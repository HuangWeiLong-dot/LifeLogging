#!/usr/bin/env python3
"""把「我现在在干嘛」的实时快照发到站点 /api/life/now。

和每天晚上的按天聚合是两回事：
  · publish_life_site.py  一天的脱敏汇总（不含坐标）
  · publish_live_now.py   此刻的一份小快照（**含坐标、窗口标题、问答原文**）

数据来源全部是本机或本机可访问的中继：
  · 位置       events.db 里今天的 location 事件（Dawarich 拉回来的 visit/transport）
  · 电脑       phone_relay 的 /desktop（push_desktop.py 每 10 分钟推一次）
  · 手机       events.db 里今天的 phone 事件（中继拉回来的解锁/切换）
  · 手表       phone_relay 的 /watch（手机传上来的 Gadgetbridge 汇总）
  · 实时问答    phone_relay 的 /qa?day=today（live_questions 的问答）

只发一份，整份覆写 R2 的 life/now.json。公开接口，所以窗口标题等文本先过
publish_life_raw.redact_text，凭证形状的东西不会出现在上面。

用法：
  python scripts/publish_live_now.py --dry-run --print-json
  python scripts/publish_live_now.py
  python scripts/publish_live_now.py --api-base http://127.0.0.1:8787
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config  # noqa: E402
from publish_life_site import (  # noqa: E402
    CITY_NAMES,
    DESKTOP_APP_NAMES,
    DISTRICT_NAMES,
    open_db,
)
from publish_life_raw import redact_text  # noqa: E402
from normalize import PHONE_APPS  # noqa: E402
from translate import Translator  # noqa: E402
import qa_client  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG.get("timezone", "Asia/Shanghai"))
SITE = CFG.get("life_site") or {}
STATE_DIR = Path(CFG.get("state_dir") or (Path(__file__).resolve().parent.parent / "state"))

MAX_TEXT = 180


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def clip(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\n", " ").strip()
    if not text:
        return None
    text = str(redact_text(text))
    return text[:limit]


def friendly_desktop(app: Any) -> str | None:
    if not app:
        return None
    text = str(app)
    mapped = DESKTOP_APP_NAMES.get(text.lower())
    if mapped:
        return mapped
    return re.sub(r"\.exe$", "", text, flags=re.I)


def friendly_phone(pkg: Any) -> str | None:
    if not pkg:
        return None
    text = str(pkg)
    if text in PHONE_APPS:
        return PHONE_APPS[text]
    if text.startswith("com.") or "." in text:
        tail = text.split(".")[-1]
        return tail[:1].upper() + tail[1:]
    return text


def parse_place(place: str | None) -> dict[str, str | None]:
    """'Yingpu, Qingpu, Shanghai, CN' → area/district/city。"""
    out = {"area": None, "district": None, "city": None}
    if not place:
        return out
    parts = [p.strip() for p in str(place).split(",") if p.strip()]
    if not parts:
        return out
    if parts[-1].upper() in {"CN", "CHINA", "中国"}:
        parts = parts[:-1]
    if parts:
        out["city"] = CITY_NAMES.get(parts[-1], parts[-1])
    if len(parts) >= 2:
        out["district"] = DISTRICT_NAMES.get(parts[-1], parts[-2])
    if len(parts) >= 3:
        out["area"] = parts[-3]
    elif len(parts) == 2:
        out["area"] = parts[0]
    return out


def row_meta(row: Any) -> dict:
    raw = row["meta_json"] if "meta_json" in row.keys() else None
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def location_now(conn, day: str) -> dict | None:
    rows = conn.execute(
        """SELECT ts_utc, day_local, metric, value_text, meta_json
           FROM events WHERE domain = 'location'
           ORDER BY ts_utc DESC LIMIT 80""",
    ).fetchall()
    if not rows:
        return None

    def end_of(r) -> datetime | None:
        meta = row_meta(r)
        return to_utc(meta.get("end_utc")) or to_utc(r["ts_utc"])

    todays = [r for r in rows if r["day_local"] == day]
    scope = todays or rows
    latest = max(scope, key=lambda r: (end_of(r) or datetime.min.replace(tzinfo=timezone.utc)))
    meta = row_meta(latest)
    end = end_of(latest)
    start = to_utc(latest["ts_utc"])
    source_day = latest["day_local"]

    # 坐标取最近一条带 lat/lon 的 visit；transport 事件本身没有坐标
    coord_row = latest
    coord_meta = meta
    if coord_meta.get("lat") is None or coord_meta.get("lon") is None:
        for r in scope:
            m = row_meta(r)
            if m.get("lat") is not None and m.get("lon") is not None:
                coord_row, coord_meta = r, m
                break

    place = None
    for r in scope:
        if r["metric"] == "visit" and r["value_text"]:
            place = r["value_text"]
            break
    place = place or latest["value_text"]
    parsed = parse_place(place)

    mode = "stationary"
    if latest["metric"] == "transport" and latest["value_text"]:
        mode = str(latest["value_text"])

    stay = None
    if latest["metric"] == "visit" and start and end:
        minutes = (end - start).total_seconds() / 60.0
        if minutes > 0:
            stay = round(minutes, 0)

    lat = coord_meta.get("lat")
    lon = coord_meta.get("lon")
    return {
        "place": clip(place, 120),
        "area": parsed["area"],
        "district": parsed["district"],
        "city": parsed["city"],
        "lat": round(float(lat), 5) if isinstance(lat, (int, float)) else None,
        "lon": round(float(lon), 5) if isinstance(lon, (int, float)) else None,
        "mode": mode,
        "stayMinutes": stay,
        "lastSeen": iso(end),
        "day": source_day,
        "stale": source_day != day,
    }


def relay_snapshot(path: str) -> dict | None:
    cfg = (CFG.get("sources") or {}).get("phone_relay") or {}
    if not cfg.get("enabled"):
        return None
    base = str(cfg.get("api_base") or "").rstrip("/")
    key = str(cfg.get("api_key") or "")
    if not base or not key:
        return None
    try:
        res = httpx.get(f"{base}{path}", headers={"X-API-Key": key}, timeout=6.0)
        if res.status_code != 200:
            return None
        body = res.json()
        snap = body.get("snapshot") if isinstance(body, dict) else None
        return snap if isinstance(snap, dict) else None
    except Exception:  # noqa: BLE001 — 中继不可达时降级到本地
        return None


def desktop_now(conn, day: str) -> dict | None:
    snap = relay_snapshot("/desktop")
    current = (snap or {}).get("current") or None
    pushed = to_utc((snap or {}).get("pushed_at"))
    last = to_utc((snap or {}).get("last_event"))
    afk = (snap or {}).get("afk")

    if snap:
        last_seen = last or pushed
        running = bool(
            current
            and afk is not True
            and last_seen
            and (now_utc() - last_seen).total_seconds() < 15 * 60
        )
        app = friendly_desktop(current.get("app")) if current else None
        title = clip(current.get("title"), 160) if current else None
        # 锁屏 / 屏保这类 current 已经在 push_desktop 里被过滤成 None
        return {
            "source": "relay",
            "running": running,
            "afk": afk,
            "app": app,
            "title": title,
            "category": current.get("category") if current else None,
            "since": current.get("since") if current else None,
            "minutes": current.get("minutes") if current else None,
            "todayHours": ((snap.get("today") or {}).get("total_hours")),
            "lastSeen": iso(last_seen),
            "pushedAt": iso(pushed),
        }

    # 中继拿不到：退回本地已经被 normalize 写好的最后一个块
    block = conn.execute(
        """SELECT start_utc, end_utc, category, app, title, blackhole
           FROM blocks WHERE day_local = ?
           ORDER BY end_utc DESC LIMIT 1""",
        (day,),
    ).fetchone()
    if not block:
        return None
    end = to_utc(block["end_utc"])
    start = to_utc(block["start_utc"])
    return {
        "source": "local-block",
        "running": bool(end and (now_utc() - end).total_seconds() < 10 * 60 and not block["blackhole"]),
        "afk": None,
        "app": friendly_desktop(block["app"]),
        "title": clip(block["title"], 160),
        "category": block["category"],
        "since": start.astimezone(TZ).strftime("%H:%M") if start else None,
        "minutes": round((end - start).total_seconds() / 60.0, 1) if start and end else None,
        "todayHours": None,
        "lastSeen": iso(end),
        "pushedAt": None,
    }


def phone_now(conn, day: str) -> dict | None:
    rows = conn.execute(
        """SELECT ts_utc, metric, value_text FROM events
           WHERE day_local = ? AND domain = 'phone'
           ORDER BY ts_utc DESC LIMIT 60""",
        (day,),
    ).fetchall()
    if not rows:
        return None
    latest = rows[0]
    app_row = next((r for r in rows if r["metric"] == "app_switch" and r["value_text"]), None)
    screen_row = next((r for r in rows if r["metric"] in ("screen_on", "screen_off")), None)
    unlocks = conn.execute(
        """SELECT COUNT(*) FROM events WHERE day_local = ? AND domain = 'phone' AND metric = 'unlock'""",
        (day,),
    ).fetchone()[0]
    return {
        "app": friendly_phone(app_row["value_text"]) if app_row else None,
        "screenOn": (screen_row["metric"] == "screen_on") if screen_row else None,
        "unlocks": int(unlocks or 0),
        "lastSeen": iso(to_utc(latest["ts_utc"])),
        "lastEvent": latest["metric"],
    }


def watch_now(conn, day: str) -> dict | None:
    snap = relay_snapshot("/watch")
    if snap:
        health = snap.get("health") or {}
        return {
            "day": snap.get("day"),
            "synced": snap.get("day") == day,
            "steps": health.get("steps"),
            "hrAvg": health.get("heart_rate_avg"),
            "hrMin": health.get("heart_rate_min"),
            "pushedAt": iso(to_utc(snap.get("pushed_at"))),
            "source": "relay",
        }

    rows = conn.execute(
        """SELECT day_local, metric, value_num FROM events
           WHERE domain = 'health' AND metric IN ('steps', 'heart_rate_avg', 'heart_rate_min')
           ORDER BY day_local DESC LIMIT 12""",
    ).fetchall()
    if not rows:
        return None
    latest_day = rows[0]["day_local"]
    metrics = {r["metric"]: r["value_num"] for r in rows if r["day_local"] == latest_day}
    return {
        "day": latest_day,
        "synced": latest_day == day,
        "steps": metrics.get("steps"),
        "hrAvg": metrics.get("heart_rate_avg"),
        "hrMin": metrics.get("heart_rate_min"),
        "pushedAt": None,
        "source": "local",
    }


def qa_now(day: str) -> dict | None:
    data = qa_client.fetch(day)
    if data:
        answers = [a for a in (data.get("answers") or []) if a.get("value")]
        questions = {q.get("id"): q for q in (data.get("questions") or [])}
        if answers:
            latest = max(answers, key=lambda a: str(a.get("ts_utc") or ""))
            q = questions.get(latest.get("id")) or {}
            return {
                "answer": clip(latest.get("value"), 400),
                "note": clip(latest.get("note"), 160),
                "question": clip(q.get("text"), 300),
                "kind": q.get("kind"),
                "ts": iso(to_utc(latest.get("ts_utc"))),
            }
    # 中继不可用：退回 events.db 里最后的答案（没有题面）
    return None


def qa_local(conn, day: str) -> dict | None:
    row = conn.execute(
        """SELECT ts_utc, value_text FROM events
           WHERE day_local = ? AND domain = 'question' AND metric = 'answer'
           ORDER BY ts_utc DESC LIMIT 1""",
        (day,),
    ).fetchone()
    if not row or not row["value_text"]:
        return None
    parts = str(row["value_text"]).split("|", 2)
    answer = parts[1] if len(parts) > 1 else parts[0]
    note = parts[2] if len(parts) > 2 else ""
    return {
        "answer": clip(answer, 400),
        "note": clip(note, 160),
        "question": None,
        "kind": None,
        "ts": iso(to_utc(row["ts_utc"])),
    }


def resolve_now_url(override: str | None) -> str:
    base = (override or SITE.get("ingest_url")
            or "https://cms.liveinpassion.me/api/life/ingest").strip()
    if base.endswith("/api/life/ingest"):
        return base[: -len("/ingest")] + "/now"
    if base.endswith("/api/life/now"):
        return base
    if base.endswith("/api/life"):
        return base + "/now"
    return base.rstrip("/") + "/api/life/now"


def main() -> int:
    ap = argparse.ArgumentParser(description="发布「我现在在干嘛」的实时快照")
    ap.add_argument("--api-base", default=None, help="覆盖站点地址（可给 ingest URL 或域名）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print-json", action="store_true")
    ap.add_argument("--no-translate", action="store_true", help="调试用：保留原文")
    args = ap.parse_args()

    day = datetime.now(TZ).date().isoformat()
    conn = open_db(STATE_DIR)
    try:
        payload = {
            "v": 1,
            "ts_utc": iso(now_utc()),
            "location": location_now(conn, day),
            "desktop": desktop_now(conn, day),
            "phone": phone_now(conn, day),
            "watch": watch_now(conn, day),
            "qa": qa_now(day) or qa_local(conn, day),
        }
    finally:
        conn.close()

    # 站点是英文界面：问答原文、窗口标题、中文地名都在这里翻成英文。
    # Translator 有本地缓存，只有新出现的句子才会真的调模型；上限压到 6 条/轮，
    # 避免每 2 分钟一次的快照把预算吃掉。
    if not args.no_translate:
        try:
            translator = Translator(CFG, STATE_DIR, max_calls=6)
            payload = translator.translate_object(payload)
            if translator.failed:
                print(f"  ⚠️  {translator.failed} 段未翻成英文（下一轮重试）", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — 翻译不可用时至少把快照发出去
            print(f"  ⚠️  翻译失败，保留原文：{exc}", file=sys.stderr)

    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("· dry-run：未发送", file=sys.stderr)
        return 0

    token_env = SITE.get("token_env") or "LIFE_INGEST_TOKEN"
    token = os.environ.get(token_env, "").strip()
    if not token:
        print(f"✗ 环境变量 {token_env} 未设置（应与 Worker secret 相同）", file=sys.stderr)
        return 2

    url = resolve_now_url(args.api_base)
    try:
        res = httpx.post(
            url,
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        print(f"✗ 连不上 {url}: {exc}", file=sys.stderr)
        return 1
    if res.status_code != 200:
        print(f"✗ 发布失败 HTTP {res.status_code}: {res.text[:300]}", file=sys.stderr)
        return 1

    loc = payload.get("location") or {}
    desk = payload.get("desktop") or {}
    qa = payload.get("qa") or {}
    print(
        f"✓ now  {loc.get('district') or loc.get('city') or '—'} "
        f"{loc.get('lat')},{loc.get('lon')} · "
        f"{(desk.get('app') or 'desktop idle')} · "
        f"{(qa.get('answer') or 'no qa')[:40]}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
