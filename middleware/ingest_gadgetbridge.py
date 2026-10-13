"""Gadgetbridge 中间件 —— 手环数据的唯一入口。

**为什么必须写这个东西**：Gadgetbridge 完全没有 REST API。
它只有两条导出路径：
  1. Settings → Automations → Auto export database（可设每小时自动导出）
  2. 发 intent `nodomain.freeyourgadget.gadgetbridge.command.TRIGGER_EXPORT`，
     完成后广播 `...action.DATABASE_EXPORT_SUCCESS`

所以流程是：手机自动导出 SQLite → 同步到本机（Syncthing / Tasker 上传）→ 本脚本读库 → 归一进事件表。

**坑**：导出的 SQLite 表名和列名**随设备不同而不同**。
  小米手环: MI_BAND_ACTIVITY_SAMPLES(TIMESTAMP, STEPS, HEART_RATE, SLEEP, DEEP_SLEEP, REM_SLEEP, ...)
  其它设备: HuamiExtendedActivitySample / GarminActivitySample / PEBBLE_ACTIVITY_SAMPLES ...
本脚本用「按列名而非表名」的策略匹配：扫描所有表，谁有 TIMESTAMP + 相关列就用谁。

用法：
  python -m middleware.ingest_gadgetbridge --date today
  python -m middleware.ingest_gadgetbridge --date today --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import Day, Event, EventsClient, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])

# 想从手环拿到的指标 → 候选列名（按优先级）
METRIC_COLUMNS: dict[str, list[str]] = {
    "steps": ["STEPS", "steps", "STEP_COUNT"],
    "heart_rate": ["HEART_RATE", "heart_rate", "HR", "AVG_HEART_RATE"],
    "sleep_deep": ["DEEP_SLEEP", "deep_sleep"],
    "sleep_light": ["SLEEP", "sleep", "LIGHT_SLEEP"],
    "sleep_rem": ["REM_SLEEP", "rem_sleep"],
    "sleep_awake": ["WAKEUP_COUNT", "awake"],
    "spo2": ["SPO2", "spo2", "BLOOD_OXYGEN"],
    "stress": ["STRESS", "stress"],
    "hrv": ["HRV", "hrv"],
    "respiratory_rate": ["RESPIRATORY_RATE", "respiratory_rate"],
    "body_temperature": ["TEMPERATURE", "body_temperature"],
}

# 手环原始数据是「采样点」，但采样间隔随设备不同（1 分钟 ~ 数分钟）
# 这个值只用于把"采样次数"换算成"覆盖分钟数"，误差可接受，仅供日志参考
DEFAULT_SAMPLE_MINUTES = 1.0


def find_candidate_tables(conn: sqlite3.Connection) -> list[tuple[str, list[str], dict[str, str]]]:
    """扫描所有表，返回 [(表名, 列, {metric: 列名})]，按覆盖指标数排序。"""
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    found = []
    for t in tables:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        col_lower = {c.lower(): c for c in cols}
        if "timestamp" not in col_lower:
            continue
        mapping: dict[str, str] = {}
        for metric, cands in METRIC_COLUMNS.items():
            for cand in cands:
                if cand.lower() in col_lower:
                    mapping[metric] = col_lower[cand.lower()]
                    break
        if mapping:
            found.append((t, cols, mapping))
    found.sort(key=lambda x: len(x[2]), reverse=True)
    return found


def read_table(
    conn: sqlite3.Connection, table: str, mapping: dict[str, str], day: Day
) -> dict[str, list[tuple[datetime, float]]]:
    """读出当天的数据，按 metric 分组。"""
    ts_col = None
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    for c in cols:
        if c.lower() == "timestamp":
            ts_col = c
            break
    if not ts_col:
        return {}

    sel = ", ".join([f'"{ts_col}" AS ts'] + [f'"{col}" AS "{m}"' for m, col in mapping.items()])

    # Gadgetbridge 的 TIMESTAMP 是 unix 秒（UTC）
    lo = int(day.start_utc.timestamp())
    hi = int(day.end_utc.timestamp())

    try:
        rows = conn.execute(
            f'SELECT {sel} FROM "{table}" WHERE "{ts_col}" >= ? AND "{ts_col}" < ?',
            (lo, hi),
        ).fetchall()
    except sqlite3.Error as e:
        print(f"  ⚠️  Failed reading {table}: {e}")
        return {}

    grouped: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for r in rows:
        ts = datetime.fromtimestamp(r["ts"], tz=timezone.utc)
        for metric in mapping:
            v = r[metric]
            if v is None:
                continue
            try:
                grouped[metric].append((ts, float(v)))
            except (TypeError, ValueError):
                continue
    return grouped


def to_events(grouped: dict[str, list[tuple[datetime, float]]], day: Day) -> list[Event]:
    out: list[Event] = []

    for metric, pts in grouped.items():
        if not pts:
            continue

        if metric == "steps":
            # 步数取最大值（很多设备是累计值）
            out.append(
                Event(
                    ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    domain="health",
                    metric="steps",
                    value_num=max(v for _, v in pts),
                    value_text="",
                    source="gadgetbridge",
                    meta={"samples": len(pts)},
                )
            )

        elif metric.startswith("sleep_"):
            # 睡眠各阶段单位不统一（有的存分钟，有的存采样数）
            # 用非零采样数 × 默认间隔估算分钟数，并标注是估算
            nonzero = [v for _, v in pts if v > 0]
            minutes = len(nonzero) * DEFAULT_SAMPLE_MINUTES
            out.append(
                Event(
                    ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    domain="health",
                    metric=metric,
                    value_num=round(minutes, 1),
                    value_text="estimated_minutes",
                    source="gadgetbridge",
                    meta={"nonzero_samples": len(nonzero)},
                )
            )

        elif metric in ("heart_rate", "spo2", "stress", "hrv", "respiratory_rate",
                        "body_temperature"):
            vals = [v for _, v in pts if v > 0]
            if not vals:
                continue
            out.append(
                Event(
                    ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    domain="health",
                    metric=f"{metric}_avg",
                    value_num=round(sum(vals) / len(vals), 2),
                    value_text="",
                    source="gadgetbridge",
                    meta={"min": min(vals), "max": max(vals), "n": len(vals)},
                )
            )
            out.append(
                Event(
                    ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    domain="health",
                    metric=f"{metric}_min",
                    value_num=min(vals),
                    source="gadgetbridge",
                )
            )

    # 总睡眠时长
    sleep_parts = {k: v for k, v in grouped.items() if k.startswith("sleep_")}
    if sleep_parts:
        total = 0.0
        for _, pts in sleep_parts.items():
            total += len([v for _, v in pts if v > 0]) * DEFAULT_SAMPLE_MINUTES
        if total > 0:
            out.append(
                Event(
                    ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    domain="health",
                    metric="sleep_hours",
                    value_num=round(total / 60.0, 2),
                    value_text="estimated",
                    source="gadgetbridge",
                )
            )

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="today")
    ap.add_argument("--db", default=None, help="override gadgetbridge.db_path from config")
    ap.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    args = ap.parse_args()

    day = Day.parse(args.date, TZ)
    header(f"Ingest Gadgetbridge · {day.iso}")

    gb = CFG["sources"]["gadgetbridge"]
    if not gb["enabled"] and not args.db:
        print("-> gadgetbridge disabled (config.yaml -> sources.gadgetbridge.enabled)")
        return

    db_path = Path(args.db or gb["db_path"])
    if not db_path.exists():
        print(f"FAIL Gadgetbridge database not found: {db_path}")
        print("   On the phone, enable Settings -> Automations -> Auto export database,")
        print("   then sync the export to this machine (Syncthing is easiest).")
        return

    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)) as conn:
        conn.row_factory = sqlite3.Row
        candidates = find_candidate_tables(conn)

        if not candidates:
            print("FAIL No activity table containing a TIMESTAMP column.")
            print("   Likely causes: the export file is empty, or the device model is not in the known list.")
            print("   Inspect it yourself: sqlite3 <db> \".tables\"")
            return

        print(f"-> Candidate tables (sorted by metric coverage):")
        for t, cols, mapping in candidates[:5]:
            print(f"    {t:40s}  matched {len(mapping)} metrics")

        table, cols, mapping = candidates[0]
        print(f"\n-> Using table: {table}")
        print(f"   Metric mapping: {mapping}")

        grouped = read_table(conn, table, mapping, day)

    if not grouped:
        print(f"\n⚠️  No data for this day. The band may not have been worn, or data has not synced yet.")
        return

    events = to_events(grouped, day)

    print(f"\n-> Normalised:")
    for e in events:
        unit = {"steps": "steps", "sleep_hours": "h"}.get(e.metric, "")
        print(f"    {e.metric:24s} {e.value_num}{unit}")

    if args.dry_run:
        print("\n(--dry-run, nothing written)")
        return

    client = EventsClient(CFG)
    if not client.ping():
        print(f"\nFAIL events API is not running ({client.base}）")
        return

    for e in events:
        e.source = "gadgetbridge"
    n = client.post_batch(events)
    print(f"\nOK  Written to events table: {n} rows")


if __name__ == "__main__":
    main()
