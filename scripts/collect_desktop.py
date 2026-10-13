"""Win11 本机采集 → 统一事件表。

**当前只启用 ActivityWatch**（窗口标题 / AFK / 浏览器 URL）。
屏幕与麦克风录制整条关闭，见 config.yaml 里 `sources.screenpipe` 的说明。

保留下来的窗口标题其实是相当强的信号 ——
「main.py - LifeLogging - Code」「Pull request #12 · GitHub」
这种字符串足以判断分类和写出「这段时间在干嘛」。
丢掉的是：音频转录、会议记录、屏幕全文 OCR。

如果将来重新开启 screenpipe，下面的 collect_screenpipe() 仍然可用。
⚠️ screenpipe 的 SQLite 表结构跨版本变动很大（旧的 `ocr_text` 列已并入 `frames.full_text`），
   所以那里全部走「探测列名」，不硬编码。表不存在就跳过，不会崩。

用法：
  python scripts/collect_desktop.py --date today
  python scripts/collect_desktop.py --date 2026-10-12
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Day, Event, EventsClient, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])


# ── 通用：SQLite 列探测 ─────────────────────────────────────────────


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
    ).fetchone()
    return row is not None


def pick_col(cols: list[str], *candidates: str) -> str | None:
    """返回第一个存在的列名。"""
    lower = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


# ── ActivityWatch ───────────────────────────────────────────────────


def aw_buckets(base: str) -> dict[str, Any]:
    r = httpx.get(f"{base}/api/0/buckets/", timeout=15.0)
    r.raise_for_status()
    return r.json()


def aw_events(base: str, bucket: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    r = httpx.get(
        f"{base}/api/0/buckets/{bucket}/events",
        params={
            "start": start.astimezone(timezone.utc).isoformat(),
            "end": end.astimezone(timezone.utc).isoformat(),
            "limit": -1,
        },
        timeout=60.0,
    )
    r.raise_for_status()
    return r.json()


def collect_activitywatch(day: Day) -> list[Event]:
    base = CFG["sources"]["activitywatch"]["api_base"].rstrip("/")
    out: list[Event] = []

    try:
        buckets = aw_buckets(base)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Cannot reach ActivityWatch ({base}）：{e}")
        print("     Check that aw-qt / aw-server is running and aw-watcher-window is enabled.")
        return out

    win_buckets = [b for b in buckets if b.startswith("aw-watcher-window_")]
    afk_buckets = [b for b in buckets if b.startswith("aw-watcher-afk_")]

    if not win_buckets:
        print("  ⚠️  No aw-watcher-window_* bucket found. The window watcher is missing or disabled.")
        return out

    for bucket in win_buckets:
        try:
            evs = aw_events(base, bucket, day.start_local, day.end_local)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  Failed reading bucket {bucket}: {e}")
            continue

        # 同名 app 合并成一块，value_num = 秒数
        agg: dict[str, float] = {}
        for ev in evs:
            data = ev.get("data") or {}
            app = (data.get("app") or "").strip()
            if not app:
                continue
            dur = float(ev.get("duration") or 0.0)
            if dur <= 0 or dur > 12 * 3600:  # 明显异常的时长丢掉
                continue
            agg[app] = agg.get(app, 0.0) + dur

        for app, secs in agg.items():
            out.append(
                Event(
                    ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    domain="screen",
                    metric="app_usage",
                    value_num=round(secs, 1),
                    value_text=app,
                    source="activitywatch",
                    meta={"bucket": bucket, "day": day.iso},
                )
            )
        print(f"  ✓ ActivityWatch {bucket}: {len(agg)} apps, "
              f"{sum(agg.values())/3600:.1f} h")

    # AFK：单独记一条，用于判断"在电脑前但没操作"
    for bucket in afk_buckets:
        try:
            evs = aw_events(base, bucket, day.start_local, day.end_local)
        except Exception:  # noqa: BLE001
            continue
        afk_secs = sum(
            float(e.get("duration") or 0)
            for e in evs
            if (e.get("data") or {}).get("status") == "afk"
        )
        if afk_secs:
            out.append(
                Event(
                    ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    domain="screen",
                    metric="afk",
                    value_num=round(afk_secs, 1),
                    value_text="",
                    source="activitywatch",
                )
            )
            print(f"  ✓ ActivityWatch AFK: {afk_secs/3600:.1f} h")

    return out


# ── screenpipe ──────────────────────────────────────────────────────


def collect_screenpipe(day: Day) -> list[Event]:
    db_path = Path(CFG["sources"]["screenpipe"]["db_path"])
    out: list[Event] = []

    if not db_path.exists():
        print(f"  ⚠️  screenpipe database not found: {db_path}")
        print("     Start it once first: npx screenpipe@latest record")
        return out

    lo = day.start_utc.strftime("%Y-%m-%dT%H:%M:%S")
    hi = day.end_utc.strftime("%Y-%m-%dT%H:%M:%S")

    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)) as conn:
        conn.row_factory = sqlite3.Row

        # —— 音频转录（对日记价值最高，取全文）——
        if has_table(conn, "audio_transcriptions"):
            cols = table_columns(conn, "audio_transcriptions")
            c_ts = pick_col(cols, "timestamp", "ts", "created_at")
            c_txt = pick_col(cols, "transcription", "text", "content")
            if c_ts and c_txt:
                rows = conn.execute(
                    f'SELECT "{c_ts}" AS ts, "{c_txt}" AS txt FROM audio_transcriptions '
                    f"WHERE {c_ts} >= ? AND {c_ts} <= ? ORDER BY {c_ts} ASC",
                    (lo, hi),
                ).fetchall()
                for r in rows:
                    txt = (r["txt"] or "").strip()
                    if len(txt) < 4:
                        continue
                    out.append(
                        Event(
                            ts_utc=_norm_ts(r["ts"]),
                            domain="audio",
                            metric="transcript",
                            value_text=txt[:2000],
                            source="screenpipe",
                        )
                    )
                print(f"  ✓ screenpipe audio transcriptions: {len(rows)} rows")

        # —— 屏幕文字（按 app+窗口 聚合成块，不要把每帧都灌进来）——
        frame_table = "frames" if has_table(conn, "frames") else (
            "ocr_text" if has_table(conn, "ocr_text") else None
        )
        if frame_table:
            cols = table_columns(conn, frame_table)
            c_ts = pick_col(cols, "timestamp", "ts", "created_at")
            c_text = pick_col(cols, "full_text", "ocr_text", "text", "accessibility_text")
            c_app = pick_col(cols, "app_name", "app", "application")
            c_win = pick_col(cols, "window_name", "window_title", "title")

            if c_ts and (c_text or c_app):
                sel = [f'"{c_ts}" AS ts']
                sel.append(f'"{c_text}" AS txt' if c_text else "'' AS txt")
                sel.append(f'"{c_app}" AS app' if c_app else "'' AS app")
                sel.append(f'"{c_win}" AS win' if c_win else "'' AS win")

                rows = conn.execute(
                    f"SELECT {', '.join(sel)} FROM {frame_table} "
                    f"WHERE {c_ts} >= ? AND {c_ts} <= ? ORDER BY {c_ts} ASC",
                    (lo, hi),
                ).fetchall()

                # 按 (app, 30分钟桶) 合并文字，控制事件数量
                buckets: dict[tuple[str, str], list[str]] = {}
                for r in rows:
                    ts = _norm_ts(r["ts"])
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    slot = dt.replace(
                        minute=(dt.minute // 30) * 30, second=0, microsecond=0
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
                    key = ((r["app"] or "unknown"), slot)
                    txt = (r["txt"] or "").strip()
                    if txt:
                        buckets.setdefault(key, []).append(txt)

                for (app, slot), texts in buckets.items():
                    merged = " ".join(texts)
                    # 去重：同一段文字被反复 OCR 到，只留一次
                    seen, uniq = set(), []
                    for t in texts:
                        if t not in seen:
                            seen.add(t)
                            uniq.append(t)
                    out.append(
                        Event(
                            ts_utc=slot,
                            domain="screen",
                            metric="screen_text",
                            value_num=len(uniq),
                            value_text=app,
                            source="screenpipe",
                            meta={"chars": len(merged), "sample": merged[:1500]},
                        )
                    )
                print(f"  ✓ screenpipe screen text: {len(rows)} frames -> {len(buckets)} (app x 30min) blocks")

        # —— 会议 ——
        if has_table(conn, "meetings"):
            cols = table_columns(conn, "meetings")
            c_ts = pick_col(cols, "meeting_start", "start_time", "created_at")
            c_name = pick_col(cols, "title", "name", "meeting_name")
            if c_ts:
                name_sel = f', "{c_name}" AS name' if c_name else ", '' AS name"
                rows = conn.execute(
                    f'SELECT "{c_ts}" AS ts{name_sel} '
                    f"FROM meetings WHERE {c_ts} >= ? AND {c_ts} <= ?",
                    (lo, hi),
                ).fetchall()
                for r in rows:
                    out.append(
                        Event(
                            ts_utc=_norm_ts(r["ts"]),
                            domain="audio",
                            metric="meeting",
                            value_text=(r["name"] or "Untitled meeting")[:200],
                            source="screenpipe",
                        )
                    )
                if rows:
                    print(f"  ✓ screenpipe meetings: {len(rows)} entries")

    return out


def collect_activitywatch_web(day: Day) -> list[Event]:
    """浏览器活动 —— 需要 aw-watcher-web 扩展。

    为什么值得装：窗口标题在浏览器里往往只有标签标题（甚至只有浏览器名），
    而这里能拿到 **URL**。`github.com/acme/pull/12` 这种信号比任何标题都明确，
    分类准确率提升主要来自这里。

    ⚠️ 扩展只上报 URL 和标题，**不截屏、不读页面内容**，也不录音。
       隐私窗口（incognito）的事件会被显式跳过。
    """
    base = CFG["sources"]["activitywatch"]["api_base"].rstrip("/")
    out: list[Event] = []

    try:
        buckets = aw_buckets(base)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Cannot reach ActivityWatch: {e}")
        return out

    web_buckets = [b for b in buckets if b.startswith("aw-watcher-web")]
    if not web_buckets:
        print("  . No aw-watcher-web-* bucket -- browser extension not installed or disabled (optional)")
        return out

    skip_prefixes = ("chrome://", "edge://", "about:", "chrome-extension://",
                     "moz-extension://", "devtools://", "brave://")

    for bucket in web_buckets:
        try:
            evs = aw_events(base, bucket, day.start_local, day.end_local)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  Failed reading bucket {bucket}: {e}")
            continue

        per_site: dict[str, float] = {}
        per_title: dict[str, float] = {}

        for ev in evs:
            data = ev.get("data") or {}
            if data.get("incognito"):
                continue
            url = (data.get("url") or "").strip()
            if not url or url.startswith(skip_prefixes):
                continue
            dur = float(ev.get("duration") or 0)
            if dur <= 0 or dur > 4 * 3600:
                continue

            host = _host_of(url)
            per_site[host] = per_site.get(host, 0.0) + dur

            title = (data.get("title") or "").strip()
            if title:
                per_title[title] = per_title.get(title, 0.0) + dur

        for host, secs in per_site.items():
            out.append(
                Event(
                    ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    domain="web",
                    metric="site_usage",
                    value_num=round(secs, 1),
                    value_text=host,
                    source="activitywatch",
                    meta={"bucket": bucket, "day": day.iso},
                )
            )

        # 停留最久的页面标题 —— 给 LLM 判断「这个站点到底在干什么」用
        for title, secs in sorted(per_title.items(), key=lambda x: -x[1])[:25]:
            out.append(
                Event(
                    ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    domain="web",
                    metric="page",
                    value_num=round(secs, 1),
                    value_text=title[:300],
                    source="activitywatch",
                )
            )

        print(f"  ✓ ActivityWatch {bucket}: {len(per_site)} sites, "
              f"{sum(per_site.values())/3600:.1f} h")

    return out


def _host_of(url: str) -> str:
    """从 URL 取主机名，取不到就退回截断的原始串。"""
    try:
        return urlparse(url).netloc.lower() or url[:80]
    except (ValueError, AttributeError):
        return url[:80]


def _norm_ts(raw: Any) -> str:
    """统一成 ISO8601 UTC。screenpipe 有时存 unix 秒、有时存 ISO 字符串。"""
    if raw is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(raw, (int, float)):
        # 秒或毫秒
        v = float(raw)
        if v > 1e11:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(raw).strip()
    if s.isdigit():
        return _norm_ts(int(s))
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        if "." in s:
            try:
                dt = datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S")
                return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pass
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── main ────────────────────────────────────────────────────────────


def main() -> None:
    from common import cli_day

    day = Day.parse(cli_day(), TZ)
    header(f"Collecting desktop · {day.iso}")

    events: list[Event] = []

    if CFG["sources"]["activitywatch"]["enabled"]:
        print("→ ActivityWatch")
        events += collect_activitywatch(day)
        events += collect_activitywatch_web(day)
    else:
        print("-> ActivityWatch disabled, skipping")

    if CFG["sources"]["screenpipe"]["enabled"]:
        print("→ screenpipe")
        events += collect_screenpipe(day)
    else:
        print("-> screenpipe disabled, skipping")

    if not events:
        print("\nFAIL No data collected. Check the warnings above.")
        return

    client = EventsClient(CFG)
    if not client.ping():
        print(f"\nFAIL events API is not running ({client.base}). Start it with:")
        print("   python -m middleware.events_api")
        return

    # 幂等：先清掉当天同源旧数据，重复跑不会翻倍
    for src in {"activitywatch", "screenpipe", "web"}:
        try:
            httpx.delete(
                f"{client.base}/day",
                headers=client.headers,
                params={"day": day.iso, "source": src},
                timeout=30.0,
            )
        except Exception:  # noqa: BLE001
            pass

    n = client.post_batch(events)
    print(f"\nOK  Written to events table: {n} rows ({len(events)} built, duplicates ignored)")


if __name__ == "__main__":
    main()
