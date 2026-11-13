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
import time
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

# ── 歌词（LRCLIB）────────────────────────────────────────────────
# lrclib.net 是公开的同步歌词库，不需要 key。查不到很正常（纯音乐、
# 冷门曲目），缓存成 null 之后 24 小时内不再重试，避免每 2 分钟打一次。
LYRICS_CACHE = STATE_DIR / "lyrics-cache.json"
LYRICS_TTL_OK = 30 * 86400
LYRICS_TTL_MISS = 86400


def _load_lyrics_cache() -> dict:
    try:
        return json.loads(LYRICS_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_lyrics_cache(cache: dict) -> None:
    try:
        LYRICS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        LYRICS_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def lyrics_for(artist: str | None, title: str | None,
               album: str | None = None, duration: float | None = None) -> str | None:
    """返回带时间戳的 LRC 文本；查不到返回 None。"""
    artist = (artist or "").strip()
    title = (title or "").strip()
    # 播放器用中文顿号/全角逗号分隔多个歌手，LRCLIB 只认英文逗号。
    # 例：`Daniel Caesar、Bon Iver` 会 404，换成 `Daniel Caesar, Bon Iver` 才命中。
    artist = artist.replace("、", ", ").replace("，", ", ").replace("＆", " & ")
    # 标题里的弯引号和 (Explicit) 后缀也会让精确匹配失败：
    # `Emily’s Song (Explicit)` → `Emily's Song`
    title = title.replace("’", "'").replace("‘", "'")
    for suffix in (" (Explicit)", " [Explicit]", " (explicit)", " [explicit]"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
            break
    if not artist or not title:
        return None

    key = f"{artist}|{title}|{album or ''}|{int(duration or 0)}"
    now = time.time()
    cache = _load_lyrics_cache()
    hit = cache.get(key)
    if hit:
        ttl = LYRICS_TTL_OK if hit.get("lyrics") else LYRICS_TTL_MISS
        if now - float(hit.get("at") or 0) < ttl:
            return hit.get("lyrics")

    # LRCLIB 的 /get 是**精确匹配**：专辑名带 (Explicit) 之类的后缀就 404。
    # 所以逐级放宽：带专辑 → 不带专辑 → 连时长也不带。
    attempts = []
    if album:
        attempts.append({"artist_name": artist, "track_name": title, "album_name": album})
    attempts.append({"artist_name": artist, "track_name": title})
    if duration:
        attempts[0]["duration"] = int(duration)

    text = None
    for params in attempts:
        try:
            res = httpx.get("https://lrclib.net/api/get", params=params, timeout=6.0)
            if res.status_code == 200:
                text = (res.json().get("syncedLyrics") or "").strip() or None
                if text:
                    break
        except Exception:  # noqa: BLE001 — 歌词是锦上添花，失败不能影响 /now
            continue

    cache[key] = {"lyrics": text, "at": now}
    _save_lyrics_cache(cache)
    return text


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
    # 单独查最新的屏幕事件：最后 60 条里可能全是 app_switch / music_position。
    # unlock 也是「屏幕亮着」的证据 —— 只认 screen_off 会把刚解锁的手机显示成 OFF。
    screen_row = conn.execute(
        """SELECT metric FROM events
           WHERE day_local = ? AND domain = 'phone'
             AND metric IN ('screen_on', 'screen_off', 'unlock')
           ORDER BY ts_utc DESC LIMIT 1""",
        (day,),
    ).fetchone()
    unlocks = conn.execute(
        """SELECT COUNT(*) FROM events WHERE day_local = ? AND domain = 'phone' AND metric = 'unlock'""",
        (day,),
    ).fetchone()[0]
    # 电量：手机每 15 分钟上报一条，取当天最新的一条。
    # 注意 Worker 只校验 /now 的**顶层**字段（location/desktop/phone/watch/qa），
    # section 内部是自由形状 —— 往 phone 里加 battery 不需要改站点。
    batt = conn.execute(
        """SELECT value_num, meta_json, ts_utc FROM events
           WHERE day_local = ? AND domain = 'phone'
             AND metric = 'battery' AND value_num IS NOT NULL
           ORDER BY ts_utc DESC LIMIT 1""",
        (day,),
    ).fetchone()
    battery = None
    if batt is not None:
        try:
            meta = json.loads(batt["meta_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        # Tasker 变量没定义时会原样发 `%CHARGING`，不能当成 True
        chg = meta.get("charging")
        charging = chg is True or (
            isinstance(chg, str)
            and not chg.startswith("%")
            and chg.strip().lower() in ("true", "1", "yes", "on")
        )
        battery = {
            "level": round(float(batt["value_num"]), 0),
            "charging": charging,
            "at": iso(to_utc(batt["ts_utc"])),
        }

    # 正在听什么：取当天最新一条 music 事件。
    music_rows = conn.execute(
        """SELECT value_text, meta_json, ts_utc FROM events
           WHERE day_local = ? AND domain = 'phone' AND metric = 'music'
             AND value_text IS NOT NULL AND substr(value_text, 1, 1) != '%'
           ORDER BY ts_utc DESC LIMIT 10""",
        (day,),
    ).fetchall()
    music_row = music_rows[0] if music_rows else None
    music = None
    if music_row is not None and music_row["value_text"]:
        try:
            mmeta = json.loads(music_row["meta_json"] or "{}")
        except (TypeError, ValueError):
            mmeta = {}
        # App 和 Tasker 可能各发一条：Tasker 那条没有 meta，但时间更晚。
        # 回看同名的最近几条，把歌手/专辑/时长补上。
        if not mmeta.get("artist"):
            for r in music_rows[1:]:
                try:
                    alt = json.loads(r["meta_json"] or "{}")
                except (TypeError, ValueError):
                    continue
                # App 的 value_text 是「歌手 - 歌名」，Tasker 的是纯歌名，
                # 所以按 meta.title 比对，不按 value_text。
                if not alt.get("artist"):
                    continue
                if alt.get("title") != music_row["value_text"]:
                    continue
                for k, v in alt.items():
                    mmeta.setdefault(k, v)
                break
        # at：前端靠它判断「现在还在放」还是「今天最后放的一首」
        music_plays = conn.execute(
            """SELECT COUNT(*) FROM events
               WHERE day_local = ? AND domain = 'phone' AND metric = 'music'
                 AND value_text IS NOT NULL AND substr(value_text, 1, 1) != '%'""",
            (day,),
        ).fetchone()[0]
        track_title = mmeta.get("title") or music_row["value_text"]
        # App 每 30 秒发的播放位置（value_text 用纯歌名），用于歌词高亮
        pos_row = conn.execute(
            """SELECT value_num, ts_utc, meta_json FROM events
               WHERE day_local = ? AND domain = 'phone' AND metric = 'music_position'
                 AND value_text = ?
               ORDER BY ts_utc DESC LIMIT 1""",
            (day, track_title),
        ).fetchone()
        # 新版 App 在 meta.playing 里直接给状态（播放/暂停/拖动都会立即推）；
        # 旧版没有这个字段时退回「90 秒内还有心跳就算在播」。
        pos_at = to_utc(pos_row["ts_utc"]) if pos_row is not None else None
        playing = None
        if pos_row is not None:
            try:
                pmeta = json.loads(pos_row["meta_json"] or "{}")
            except (TypeError, ValueError):
                pmeta = {}
            if "playing" in pmeta:
                playing = bool(pmeta.get("playing"))
            elif pos_at is not None:
                playing = (now_utc() - pos_at).total_seconds() < 90
        music = {
            # 手机 App 会单独给 title/artist/album；Tasker 只有整行 value_text。
            "title": track_title,
            "artist": mmeta.get("artist"),
            "album": mmeta.get("album"),
            "duration": mmeta.get("duration"),
            # 96x96 JPEG 的 base64，站点直接渲染成 data URI
            "cover": mmeta.get("cover"),
            "raw": music_row["value_text"],
            "app": mmeta.get("app"),
            "at": iso(to_utc(music_row["ts_utc"])),
            "plays": int(music_plays or 0),
            "playing": playing,
            "position": pos_row["value_num"] if pos_row is not None else None,
            "positionAt": iso(pos_at) if pos_at else None,
            "lyrics": lyrics_for(mmeta.get("artist"), track_title,
                                 mmeta.get("album"), mmeta.get("duration")),
        }

    net_row = conn.execute(
        """SELECT value_text FROM events
           WHERE day_local = ? AND domain = 'phone' AND metric = 'network'
             AND value_text IS NOT NULL
           ORDER BY ts_utc DESC LIMIT 1""",
        (day,),
    ).fetchone()
    network = None
    if net_row is not None and net_row["value_text"]:
        kind = str(net_row["value_text"]).split(":", 1)[0]
        network = {"mode": kind or None}

    notif_today = conn.execute(
        """SELECT COUNT(*) FROM events
           WHERE day_local = ? AND domain = 'phone' AND metric = 'notification'""",
        (day,),
    ).fetchone()[0]
    notif_top_rows = conn.execute(
        """SELECT value_text AS app, COUNT(*) AS n FROM events
           WHERE day_local = ? AND domain = 'phone' AND metric = 'notification'
             AND value_text IS NOT NULL AND value_text != ''
           GROUP BY value_text ORDER BY n DESC LIMIT 30""",
        (day,),
    ).fetchall()
    notif_top = [[friendly_phone(r["app"]), int(r["n"])] for r in notif_top_rows]

    return {
        "app": friendly_phone(app_row["value_text"]) if app_row else None,
        "screenOn": (screen_row["metric"] != "screen_off") if screen_row else None,
        "unlocks": int(unlocks or 0),
        "battery": battery,
        "music": music,
        "network": network,
        "notifications": int(notif_today or 0),
        "notificationsTop": notif_top,
        "lastSeen": iso(to_utc(latest["ts_utc"])),
        "lastEvent": latest["metric"],
    }


def watch_now(conn, day: str) -> dict | None:
    rows = conn.execute(
        """SELECT day_local, metric, value_num, ts_utc, ingested_at FROM events
           WHERE domain = 'health'
             AND metric IN ('steps', 'heart_rate_avg', 'heart_rate_min')
           ORDER BY day_local DESC, ts_utc DESC LIMIT 24""",
    ).fetchall()

    local = None
    if rows:
        latest_day = rows[0]["day_local"]
        metrics = {
            r["metric"]: r["value_num"]
            for r in rows if r["day_local"] == latest_day
        }
        stamps = [
            r["ingested_at"] or r["ts_utc"]
            for r in rows if r["day_local"] == latest_day
        ]
        local = {
            "day": latest_day,
            "synced": latest_day == day,
            "steps": metrics.get("steps"),
            "hrAvg": metrics.get("heart_rate_avg"),
            "hrMin": metrics.get("heart_rate_min"),
            "pushedAt": iso(to_utc(max(stamps))) if stamps else None,
            "source": "local",
        }

    snap = relay_snapshot("/watch")
    relay = None
    if snap:
        health = snap.get("health") or {}
        relay = {
            "day": snap.get("day"),
            "synced": snap.get("day") == day,
            "steps": health.get("steps"),
            "hrAvg": health.get("heart_rate_avg"),
            "hrMin": health.get("heart_rate_min"),
            "pushedAt": iso(to_utc(snap.get("pushed_at"))),
            "source": "relay",
        }

    # 本机 events.db 才是源数据，中继只是给飞书机器人留的最后一份。
    # 中继的 /watch 可能还是昨天的（push_health 只在重采集里跑），
    # 所以取更新的那一天；同一天时优先本机（ingested_at 更新）。
    if local and relay:
        return local if str(local.get("day") or "") >= str(relay.get("day") or "") else relay
    return local or relay


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
