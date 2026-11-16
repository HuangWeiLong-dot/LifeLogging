#!/usr/bin/env python3
"""把中继上的手机事件直接发布到站点，不再等笔记本醒来。

**它解决什么**

    手机（Tasker / LifeBridge）24 小时往中继发事件，但「中继 → 站点」原来
    只有笔记本上的 `collect_phone_events.py → events.db → publish_life_*`
    这一条路。笔记本一关，LIFE 页面的手机卡片、归档里的 EVENTS 都停在那里。

    这里绕开 events.db，直接读中继落盘的 `phone-<day>.jsonl`：

      1. 原始文档 `life/raw/phone/<day>.json` —— 和笔记本发布的 `events`
         是同一批事件（手机 / 手动笔记 / 问答），由 Worker 在
         `GET /api/life/raw/events/<day>` 时合并、按
         `(ts_utc, domain, metric, value_text)` 去重。笔记本那份仍然是主，
         这份只在它缺席/落后时补齐。
      2. 当日手机聚合 —— POST 到站点的 `/api/life/phone/ingest`，只合并
         `phone` 字段（及手机-only 日子的七维/并集），不碰桌面数据。

**它不做什么**

    · 不写 events.db，不碰笔记本的 `state/`。
    · 不发布桌面 / 位置 / 健康 / 日记 —— 那些仍然需要笔记本。
    · 不翻译。归档里的应用名可能是中文；笔记本那份到了之后会覆盖成英文副本。

**隐私**

    和 `publish_life_raw.build_events` 同一套边界：支付整条丢弃、
    通知正文丢掉（只留哪个 App / 几条 / `body=redacted`）、音乐封面去掉。
    凭证遮盖复用 `redact_text` / `redact_obj`，不另写一份。

**配置**（/etc/lifelogging/watch.env 也可以直接用，令牌是同一个）

    LIFE_INGEST_TOKEN   必填，与 Worker secret 相同
    LIFE_API_BASE       默认 https://cms.liveinpassion.me
    RELAY_DATA_DIR      默认 /var/lib/lifelogging（与中继一致）
    PHONE_WINDOW_DAYS   默认 30，只发布最近 N 天
    PHONE_TZ            默认 Asia/Shanghai（分天口径）

用法：

    python3 server/phone-publisher/phone_publisher.py --dry-run
    python3 server/phone-publisher/phone_publisher.py --days 7
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from normalize import phone_category_intervals  # noqa: E402
from publish_life_raw import (  # noqa: E402
    NOTIFICATION_DROP_META,
    redact_obj,
    redact_text,
)
from publish_life_site import (  # noqa: E402
    _clip_intervals,
    _interval_minutes,
    build_phone,
)

DATA_DIR = Path(os.environ.get("RELAY_DATA_DIR", "/var/lib/lifelogging")).resolve()
API_BASE = (os.environ.get("LIFE_API_BASE") or "https://cms.liveinpassion.me").rstrip("/")
TOKEN = os.environ.get("LIFE_INGEST_TOKEN", "").strip()
WINDOW_DAYS = max(1, int(os.environ.get("PHONE_WINDOW_DAYS", "30")))
TZ = ZoneInfo(os.environ.get("PHONE_TZ", "Asia/Shanghai"))
STATE_PATH = DATA_DIR / "phone-published.json"

RAW_URL = f"{API_BASE}/api/life/raw/ingest"
AGG_URL = f"{API_BASE}/api/life/phone/ingest"

# 单个原始文档的上限。Worker 的 MAX_DOC_BYTES 是 4 MiB，留一点余量。
MAX_RAW_BYTES = 3_500_000
# 一批原始文档的总字节数。Worker 的 MAX_BODY_BYTES 是 12 MiB。
MAX_BATCH_BYTES = 8_000_000

DIMENSIONS = ("Work", "Study", "Transport", "Leisure", "Health", "Life", "Social")


def load_state() -> dict:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def read_day(day: str) -> list[dict] | None:
    """读中继当天那份 JSONL。文件不存在返回 None（不是错误）。"""
    path = DATA_DIR / f"phone-{day}.jsonl"
    if not path.is_file():
        return None
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue      # 断电写坏的半行：跳过，不让一行拖垮整天
                if isinstance(e, dict):
                    out.append(e)
    except OSError as exc:
        print(f"  ⚠️  读取 {path} 失败：{exc}", file=sys.stderr)
        return None
    return out


def clean_event(e: dict) -> dict | None:
    """中继的一行 → 可以公开的归档事件。

    ⚠️ 这里的每一条过滤都必须是**明确**的。归档是公开的：
       支付金额 / 通知正文 / 音乐封面都不能因为「忘了处理」而漏出去。
    """
    ts = str(e.get("ts_utc") or "").strip()
    metric = str(e.get("metric") or "").strip()
    if not ts or not metric:
        return None
    domain = str(e.get("domain") or "phone").strip() or "phone"

    if domain == "phone" and metric == "payment":
        return None
    meta = e.get("meta") if isinstance(e.get("meta"), dict) else {}
    meta = dict(meta)
    if domain == "phone" and metric == "notification":
        meta = {k: v for k, v in meta.items() if k not in NOTIFICATION_DROP_META}
        meta["body"] = "redacted"
    if domain == "phone" and metric == "music":
        meta.pop("cover", None)          # 96x96 base64，归档不需要

    out = {
        "ts_utc": ts,
        "domain": domain,
        "metric": metric,
        "value_text": redact_text(e.get("value_text")),
        "source": str(e.get("source") or ""),
        "meta": redact_obj(meta),
    }
    if e.get("value_num") is not None:
        out["value_num"] = e["value_num"]
    return out


def phone_only_aggregate(phone_events: list[dict], day: str) -> tuple[dict | None, dict, float | None]:
    """手机自己的聚合 + 手机-only 日子的七维与并集。

    ⚠️ 只用于**没有笔记本完整快照**的日子。站点那边只有在
        `phoneOnly=true` 时才会用这份七维覆盖，已有桌面数据的日子只合并
       `phone` 字段 —— 否则会把桌面的读数抹掉。
    """
    rows = [
        {
            "ts_utc": e["ts_utc"],
            "metric": e["metric"],
            "value_text": e.get("value_text"),
            "value_num": e.get("value_num"),
        }
        for e in phone_events
    ]
    phone = build_phone(rows)

    day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    day_end = day_start + timedelta(days=1)
    intervals: list[tuple[datetime, datetime]] = []
    dims = {name: 0.0 for name in DIMENSIONS}
    for cat, spans in phone_category_intervals(phone_events).items():
        for lo, hi in _clip_intervals(spans, day_start, day_end):
            if hi <= lo:
                continue
            intervals.append((lo, hi))
            if cat in dims:
                dims[cat] += (hi - lo).total_seconds() / 3600.0
    collapsed = round(_interval_minutes(intervals) / 60.0, 2) if intervals else None
    return phone, {k: round(v, 2) for k, v in dims.items()}, collapsed


def build_day(day: str, raw: list[dict]) -> dict | None:
    """清洗 + 聚合一天。没有可用事件返回 None。"""
    cleaned = [c for c in (clean_event(e) for e in raw) if c]
    if not cleaned:
        return None
    cleaned.sort(key=lambda e: e["ts_utc"])

    phone_events = [e for e in cleaned if e["domain"] == "phone"]
    phone, dims, collapsed = phone_only_aggregate(phone_events, day)

    content = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
    if len(content.encode("utf-8")) > MAX_RAW_BYTES:
        # music_position 是每 30 秒一条的心跳，占了文件的大头；归档里没有
        # 它也能看出听了什么。先去掉它，仍然超限就只发聚合、不发原始文档。
        trimmed = [e for e in cleaned if e["metric"] != "music_position"]
        content = json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"))
        print(f"  · {day}: 原始文档过大，去掉 music_position 后 "
              f"{len(content.encode('utf-8')) / 1e6:.2f} MB")
    raw_ok = len(content.encode("utf-8")) <= MAX_RAW_BYTES
    if not raw_ok:
        print(f"  ⚠️  {day}: 原始文档仍然超过 {MAX_RAW_BYTES} 字节，本轮只发聚合",
              file=sys.stderr)

    return {
        "day": day,
        "content": content if raw_ok else None,
        "phone": phone,
        "dimensions": dims,
        "collapsedHours": collapsed,
    }


def digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def post_raw(docs: list[dict]) -> None:
    """按字节分批发原始文档。失败抛异常，调用方不更新状态。"""
    batch: list[dict] = []
    size = 0
    for doc in docs:
        n = len(doc["content"].encode("utf-8"))
        if batch and size + n > MAX_BATCH_BYTES:
            _post_raw_batch(batch)
            batch, size = [], 0
        batch.append(doc)
        size += n
    if batch:
        _post_raw_batch(batch)


def _post_raw_batch(batch: list[dict]) -> None:
    res = httpx.post(
        RAW_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"v": 1, "docs": batch},
        timeout=120.0,
    )
    res.raise_for_status()
    print(f"  ✓ 原始文档 {len(batch)} 天 → {RAW_URL}")


def post_aggregate(days: list[dict]) -> None:
    res = httpx.post(
        AGG_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"v": 1, "days": days},
        timeout=60.0,
    )
    res.raise_for_status()
    print(f"  ✓ 手机聚合 {len(days)} 天 → {AGG_URL}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish relay phone events to the site")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不 POST、不写状态")
    ap.add_argument("--days", type=int, default=WINDOW_DAYS, help="只发布最近 N 天")
    args = ap.parse_args()

    if not args.dry_run and not TOKEN:
        print("✗ LIFE_INGEST_TOKEN 未设置（与 Worker secret 相同）", file=sys.stderr)
        return 2

    today = datetime.now(TZ).date()
    state = load_state()
    days_state = state.get("days") if isinstance(state.get("days"), dict) else {}
    new_state: dict[str, dict] = {}

    raw_docs: list[dict] = []
    agg_days: list[dict] = []
    pending_state: dict[str, dict] = {}

    scanned = skipped = 0
    for i in range(max(1, args.days)):
        day = (today - timedelta(days=i)).isoformat()
        path = DATA_DIR / f"phone-{day}.jsonl"
        try:
            st = path.stat()
        except OSError:
            continue
        scanned += 1
        prev = days_state.get(day)
        if (prev and prev.get("mtime_ns") == st.st_mtime_ns
                and prev.get("size") == st.st_size):
            new_state[day] = prev
            skipped += 1
            continue

        raw = read_day(day)
        if raw is None:
            continue
        built = build_day(day, raw)
        if built is None:
            continue

        raw_hash = digest(built["content"]) if built["content"] else ""
        agg_hash = digest(json.dumps(built["phone"], sort_keys=True,
                                     ensure_ascii=False)) if built["phone"] else ""
        if prev and prev.get("raw") == raw_hash and prev.get("agg") == agg_hash:
            new_state[day] = {
                "mtime_ns": st.st_mtime_ns, "size": st.st_size,
                "raw": raw_hash, "agg": agg_hash,
            }
            skipped += 1
            continue

        if built["content"]:
            raw_docs.append({
                "kind": "phone", "id": day, "format": "json",
                "content": built["content"],
            })
        if built["phone"]:
            agg_days.append({
                "day": day,
                "phone": built["phone"],
                "dimensions": built["dimensions"],
                "collapsedHours": built["collapsedHours"],
            })
        pending_state[day] = {
            "mtime_ns": st.st_mtime_ns, "size": st.st_size,
            "raw": raw_hash, "agg": agg_hash,
        }

    print(f"  · 扫了 {scanned} 天，跳过 {skipped} 天未变化，"
          f"待发 {len(raw_docs)} 份原始文档 / {len(agg_days)} 天聚合")

    if args.dry_run:
        for doc in raw_docs:
            print(f"    {doc['id']}: {len(doc['content']) / 1024:.0f} KiB")
        for day in agg_days:
            p = day["phone"] or {}
            print(f"    {day['day']}: {p.get('unlocks', 0)} unlocks, "
                  f"{p.get('screenMinutes', 0)} min screen")
        print("· dry-run，没有写入")
        return 0

    if not raw_docs and not agg_days:
        print("· 没有变化")
        return 0

    try:
        if raw_docs:
            post_raw(raw_docs)
        if agg_days:
            post_aggregate(agg_days)
    except Exception as exc:  # noqa: BLE001
        detail = getattr(getattr(exc, "response", None), "text", "")[:300]
        print(f"✗ 发布失败: {type(exc).__name__}: {exc} {detail}", file=sys.stderr)
        return 1

    # 只有全部 POST 成功才推进状态；失败下一轮会重发（站点写入是幂等的）。
    new_state.update(pending_state)
    state["days"] = {
        k: v for k, v in new_state.items()
        if k >= (today - timedelta(days=max(1, args.days) - 1)).isoformat()
    }
    save_state(state)
    print(f"✓ 已发布 {len(raw_docs)} 份原始文档 / {len(agg_days)} 天聚合")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
