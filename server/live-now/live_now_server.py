#!/usr/bin/env python3
"""24h 服务器侧的 /api/life/now 发布器。

为什么需要它：

    笔记本上的 `scripts/publish_live_now.py` 每分钟的链路依赖本机 ——
    本机一关，站点就停在最后一份快照。但位置（Dawarich）、手机使用事件、
    实时问答、以及手机传到中继的 Gadgetbridge 导出，**在服务器上都是可用的**。
    这个脚本让这些部分在笔记本关机时继续更新；电脑窗口那一栏只能显示
    最后一次中继快照（`LAST SEEN`），因为 ActivityWatch 在本机。

数据来源：
    · 位置       Dawarich（和 collect_phone.py 同一套 fetch_points/derive_stays）
    · 手机       phone_relay 的 /events?day=
    · 实时问答   phone_relay 的 /qa?day=
    · 电脑       phone_relay 的 /desktop（笔记本每 10 分钟推一次）
    · 手表       phone_relay 上的 gadgetbridge.db（手机上传的那份）——
                 直接解析今日步数/心率；中继 /watch 只作为没有今日数据时的回退
    · 英文       发布前用 translate.Translator 翻问答/窗口标题/地名

部署：
    server/live-now/README.md。它由 lifelogging-live-now.timer 每 2 分钟拉起，
    和笔记本上的实时任务互不冲突：发布前会读当前 /api/life/now，电脑 / 手表
    只保留两边里更新的那一份。

用法（在服务器上，或本机调试）：
    /opt/lifelogging/venv/bin/python server/live-now/live_now_server.py --dry-run --print-json
    /opt/lifelogging/venv/bin/python server/live-now/live_now_server.py
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

from common import Day, load_config  # noqa: E402
import publish_live_now as base  # noqa: E402
from collect_phone import derive_stays, fetch_points  # noqa: E402
from live_questions import relay_phone_events  # noqa: E402
from translate import Translator  # noqa: E402
from middleware.ingest_gadgetbridge import (  # noqa: E402
    find_candidate_tables,
    read_table,
    to_events,
)

CFG = load_config()
TZ = ZoneInfo(CFG.get("timezone", "Asia/Shanghai"))
STATE_DIR = Path(CFG.get("state_dir") or "/var/lib/lifelogging")
SITE = CFG.get("life_site") or {}


def relay_conn() -> tuple[str, str] | None:
    cfg = (CFG.get("sources") or {}).get("phone_relay") or {}
    if not cfg.get("enabled"):
        return None
    url = str(cfg.get("api_base") or "").rstrip("/")
    key = str(cfg.get("api_key") or "")
    if not url or not key or "REPLACE" in (url + key).upper():
        return None
    return url, key


def location_server(day: str) -> dict | None:
    """Dawarich → 最新坐标 + 最后一个停留。"""
    try:
        d = Day.parse(day, TZ)
        points = fetch_points(d)
    except Exception as exc:  # noqa: BLE001 — 位置服务不是发布链路的硬依赖
        print(f"  ⚠️  location fetch failed: {exc}", file=sys.stderr)
        return None
    if not points:
        return None

    try:
        stays = derive_stays(d, points)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  stay derivation failed: {exc}", file=sys.stderr)
        stays = []

    def stay_end(ev):
        return base.to_utc((ev.meta or {}).get("end_utc")) or base.to_utc(ev.ts_utc)

    latest_stay = max(stays, key=lambda ev: stay_end(ev) or datetime.min.replace(tzinfo=timezone.utc)) if stays else None
    latest_point = points[-1]
    lat, lon = latest_point[1], latest_point[2]
    point_dt = datetime.fromtimestamp(latest_point[0], tz=timezone.utc)

    place = None
    area = district = city = None
    mode = "stationary"
    stay_minutes = None
    last_seen = point_dt
    if latest_stay is not None:
        end = stay_end(latest_stay) or point_dt
        last_seen = max(last_seen, end)
        minutes = float(latest_stay.value_num or 0)
        if minutes > 0:
            stay_minutes = round(minutes, 0)
        raw_place = str(latest_stay.value_text or "")
        if raw_place and not raw_place.startswith("("):
            parsed = base.parse_place(raw_place)
            place = base.clip(raw_place, 120)
            area, district, city = parsed["area"], parsed["district"], parsed["city"]
        else:
            mode = "moving"

    return {
        "place": place,
        "area": area,
        "district": district,
        "city": city,
        "lat": round(float(lat), 5),
        "lon": round(float(lon), 5),
        "mode": mode,
        "stayMinutes": stay_minutes,
        "lastSeen": base.iso(last_seen),
        "day": day,
        "stale": False,
    }


def desktop_server() -> dict | None:
    snap = base.relay_snapshot("/desktop")
    if not snap:
        return None
    current = snap.get("current") or None
    pushed = base.to_utc(snap.get("pushed_at"))
    last = base.to_utc(snap.get("last_event"))
    afk = snap.get("afk")
    last_seen = last or pushed
    running = bool(
        current
        and afk is not True
        and last_seen
        and (base.now_utc() - last_seen).total_seconds() < 15 * 60
    )
    return {
        "source": "relay",
        "running": running,
        "afk": afk,
        "app": base.friendly_desktop(current.get("app")) if current else None,
        "title": base.clip(current.get("title"), 160) if current else None,
        "category": current.get("category") if current else None,
        "since": current.get("since") if current else None,
        "minutes": current.get("minutes") if current else None,
        "todayHours": ((snap.get("today") or {}).get("total_hours")),
        "lastSeen": base.iso(last_seen),
        "pushedAt": base.iso(pushed),
    }


def phone_server(day: str) -> dict | None:
    conn = relay_conn()
    if not conn:
        return None
    url, key = conn
    try:
        events = relay_phone_events(day, url, key)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  relay phone events failed: {exc}", file=sys.stderr)
        return None
    if not events:
        return None

    def ts(e):
        return str(e.get("ts_utc") or "")

    latest = max(events, key=ts)
    app_row = next(
        (e for e in sorted(events, key=ts, reverse=True)
         if e.get("metric") == "app_switch" and e.get("value_text")),
        None,
    )
    screen_row = next(
        (e for e in sorted(events, key=ts, reverse=True)
         if e.get("metric") in ("screen_on", "screen_off")),
        None,
    )
    unlocks = sum(1 for e in events if e.get("metric") == "unlock")
    return {
        "app": base.friendly_phone(app_row.get("value_text")) if app_row else None,
        "screenOn": (screen_row.get("metric") == "screen_on") if screen_row else None,
        "unlocks": int(unlocks),
        "lastSeen": base.iso(base.to_utc(latest.get("ts_utc"))),
        "lastEvent": latest.get("metric"),
    }


def gadgetbridge_db() -> Path | None:
    gb = (CFG.get("sources") or {}).get("gadgetbridge") or {}
    candidates = []
    if gb.get("db_path"):
        candidates.append(Path(str(gb["db_path"])))
    # 中继的上传目录；server/README.md 里 DATA_DIR 就是这里
    candidates.append(Path("/var/lib/lifelogging/gadgetbridge.db"))
    candidates.append(STATE_DIR / "gadgetbridge.db")
    for path in candidates:
        try:
            if path and path.is_file():
                return path
        except OSError:
            continue
    return None


def watch_from_db(day: str) -> dict | None:
    """直接解析手机上传到中继的 Gadgetbridge 数据库；不依赖笔记本。

    今日部分数据也是有效的：read_table 的时间窗是整天，采样点有多少算多少。
    """
    db = gadgetbridge_db()
    if not db:
        return None
    try:
        d = Day.parse(day, TZ)
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10.0)) as conn:
            conn.row_factory = sqlite3.Row
            candidates = find_candidate_tables(conn)
            if not candidates:
                return None
            table, _cols, mapping, _rows = candidates[0]
            grouped = read_table(conn, table, mapping, d)
            events = to_events(grouped, d)
        metrics = {
            e.metric: e.value_num
            for e in events
            if e.metric and e.value_num is not None
        }
        if not metrics:
            return None
        pushed = datetime.fromtimestamp(db.stat().st_mtime, tz=timezone.utc)
        return {
            "day": day,
            "synced": True,
            "steps": metrics.get("steps"),
            "hrAvg": metrics.get("heart_rate_avg"),
            "hrMin": metrics.get("heart_rate_min"),
            "pushedAt": base.iso(pushed),
            "source": "gadgetbridge-db",
        }
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  gadgetbridge parse failed: {exc}", file=sys.stderr)
        return None


def watch_server(day: str) -> dict | None:
    local = watch_from_db(day)
    if local:
        return local
    snap = base.relay_snapshot("/watch")
    if not snap:
        return None
    health = snap.get("health") or {}
    return {
        "day": snap.get("day"),
        "synced": snap.get("day") == day,
        "steps": health.get("steps"),
        "hrAvg": health.get("heart_rate_avg"),
        "hrMin": health.get("heart_rate_min"),
        "pushedAt": base.iso(base.to_utc(snap.get("pushed_at"))),
        "source": "relay",
    }


def fetch_current(url: str) -> dict | None:
    try:
        res = httpx.get(url, timeout=10.0)
        if res.status_code != 200:
            return None
        body = res.json()
        return body if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001
        return None


def newer_ts(a, b) -> int:
    da, db = base.to_utc(a), base.to_utc(b)
    if da and db:
        return 1 if da > db else -1 if da < db else 0
    if da and not db:
        return 1
    if db and not da:
        return -1
    return 0


def merge_with_current(payload: dict, current: dict | None) -> dict:
    if not current:
        return payload

    # 电脑 / 手表只保留更新的那一份：笔记本开着时它本机那份通常比中继新；
    # 笔记本关掉后服务器这份才会胜出。
    cur_desk = current.get("desktop") or None
    new_desk = payload.get("desktop") or None
    if cur_desk and (not new_desk or newer_ts(cur_desk.get("lastSeen"), new_desk.get("lastSeen")) > 0):
        payload["desktop"] = cur_desk

    cur_watch = current.get("watch") or None
    new_watch = payload.get("watch") or None

    def watch_key(w):
        if not w:
            return ("", "")
        return (str(w.get("day") or ""), str(w.get("pushedAt") or ""))

    if cur_watch and (not new_watch or watch_key(cur_watch) > watch_key(new_watch)):
        payload["watch"] = cur_watch

    # 服务器拿不到的段，用当前快照兜底
    for key in ("location", "phone", "qa", "desktop", "watch"):
        if payload.get(key) is None and current.get(key) is not None:
            payload[key] = current[key]
    return payload


def resolve_now_url(override: str | None) -> str:
    return base.resolve_now_url(override)


def main() -> int:
    ap = argparse.ArgumentParser(description="24h server-side /api/life/now publisher")
    ap.add_argument("--api-base", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print-json", action="store_true")
    ap.add_argument("--no-translate", action="store_true")
    args = ap.parse_args()

    day = datetime.now(TZ).date().isoformat()
    now_url = resolve_now_url(args.api_base)
    current = fetch_current(now_url)

    payload = {
        "v": 1,
        "ts_utc": base.iso(base.now_utc()),
        "location": location_server(day),
        "desktop": desktop_server(),
        "phone": phone_server(day),
        "watch": watch_server(day),
        "qa": base.qa_now(day),
    }
    payload = merge_with_current(payload, current)

    if not args.no_translate:
        try:
            translator = Translator(CFG, STATE_DIR, max_calls=6)
            payload = translator.translate_object(payload)
            if translator.failed:
                print(f"  ⚠️  {translator.failed} string(s) not translated", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️  translation failed, publishing original: {exc}", file=sys.stderr)

    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("· dry-run: not sent", file=sys.stderr)
        return 0

    token_env = SITE.get("token_env") or "LIFE_INGEST_TOKEN"
    token = os.environ.get(token_env, "").strip()
    if not token:
        print(f"✗ environment variable {token_env} is not set", file=sys.stderr)
        return 2

    try:
        res = httpx.post(
            now_url,
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        print(f"✗ cannot reach {now_url}: {exc}", file=sys.stderr)
        return 1
    if res.status_code != 200:
        print(f"✗ publish failed HTTP {res.status_code}: {res.text[:300]}", file=sys.stderr)
        return 1

    loc = payload.get("location") or {}
    desk = payload.get("desktop") or {}
    watch = payload.get("watch") or {}
    qa = payload.get("qa") or {}
    print(
        "✓ now "
        f"{loc.get('district') or loc.get('city') or '—'} "
        f"{loc.get('lat')},{loc.get('lon')} · "
        f"{desk.get('app') or 'desktop idle'} · "
        f"watch {watch.get('steps')} ({watch.get('day') or '—'}) · "
        f"{(qa.get('answer') or 'no qa')[:40]}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
