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
import json
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
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


def find_candidate_tables(
    conn: sqlite3.Connection,
) -> list[tuple[str, list[str], dict[str, str], int]]:
    """扫描所有表，返回 ``[(表名, 列, {metric: 列名}, 行数)]``。

    **排序：先看有没有数据，再看匹配到几个指标。**

    ⚠️ 顺序不能反 —— 这是一个实测踩出来的坑。
       Gadgetbridge 的库会把**所有**设备的 schema 都建出来（小米、华米、
       Garmin……），没接的设备就是一张空表。所以「匹配到几个指标」这个
       指标会**系统性地偏向空表**：实测 Garmin Forerunner 235 的库里
       `HUAMI_EXTENDED_ACTIVITY_SAMPLE` 匹配 5 个指标但 **0 行**，
       而真正有 21090 行的 `GARMIN_ACTIVITY_SAMPLE` 只匹配 2 个指标。

       只按指标数排的话会选中空表，然后打印
       「没数据，可能没戴表」—— 一个错的解释，掩盖了一个真 bug。
    """
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
        if not mapping:
            continue
        try:
            rows = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.DatabaseError:
            rows = 0
        found.append((t, cols, mapping, rows))
    # 有数据的排前面；同为有数据（或同为空）时，匹配指标多的排前面
    found.sort(key=lambda x: (x[3] > 0, len(x[2])), reverse=True)
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


# ── 锻炼会话 ────────────────────────────────────────────────────────
#
# 和上面那张「每分钟采样」是完全不同的东西：这是**一次完整的锻炼**，
# 汇总了距离、配速、心率区间、VO2max、训练效果。
#
# ⚠️ 它不会出现在 find_candidate_tables 的候选里 —— 那张表的时间列叫
#    START_TIME 而且单位是**毫秒**（不是 TIMESTAMP / 秒），所以只能单独读。
#    非 Garmin 设备没有这张表，读不到就安静跳过。
WORKOUT_TABLE = "BASE_ACTIVITY_SUMMARY"


def _summary_value(d: dict[str, Any], key: str) -> Any:
    """SUMMARY_DATA 里每个字段是 ``{unit, value}``，但也有的直接是裸值。"""
    v = d.get(key)
    return v.get("value") if isinstance(v, dict) else v


def read_workouts(conn: sqlite3.Connection, day: Day) -> list[dict[str, Any]]:
    """读当天的锻炼会话。表不存在、或没有那天的记录，都返回空列表。"""
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (WORKOUT_TABLE,)
        )
    }
    if not names:
        return []

    out: list[dict[str, Any]] = []
    for row in conn.execute(
        f'SELECT NAME, START_TIME, END_TIME, ACTIVITY_KIND, SUMMARY_DATA '
        f'FROM "{WORKOUT_TABLE}" ORDER BY START_TIME'
    ):
        start_ms = row["START_TIME"]
        if not isinstance(start_ms, (int, float)) or start_ms <= 0:
            continue
        # ⚠️ 毫秒。按秒解释会得到 1970 年，静默落到错误的那一天。
        #    这里用 1e11 做分界：秒级时间戳要到公元 5138 年才到这个量级，
        #    所以低于它的当秒、高于它的当毫秒，两边都不会误判。
        start = _ms_or_s(row["START_TIME"])
        end = _ms_or_s(row["END_TIME"])
        if start is None:
            continue
        if start.astimezone(TZ).date() != day.d:
            continue                       # 只取目标那一天

        try:
            s = json.loads(row["SUMMARY_DATA"] or "{}")
        except (json.JSONDecodeError, TypeError):
            s = {}

        secs = _summary_value(s, "activeSeconds") or 0
        meters = _summary_value(s, "distanceMeters") or 0
        try:
            secs = float(secs)
            meters = float(meters)
        except (TypeError, ValueError):
            secs, meters = 0.0, 0.0

        out.append({
            "name": (row["NAME"] or "").strip() or f"activity {row['ACTIVITY_KIND']}",
            "kind": row["ACTIVITY_KIND"],
            "start": start,
            "end": end,
            "minutes": round(secs / 60.0, 1),
            "km": round(meters / 1000.0, 2),
            "avg_hr": _summary_value(s, "averageHR"),
            "max_hr": _summary_value(s, "maxHR"),
            "pace_s_per_km": _summary_value(s, "averageKMPaceSeconds"),
            "ascent_m": _summary_value(s, "activityTotalAscent"),
            "calories": _summary_value(s, "caloriesBurnt"),
            "vo2max": _summary_value(s, "maximumOxygenUptake"),
            "training_effect": _summary_value(s, "aerobicTrainingEffect"),
            "recovery_hours": (
                round(float(_summary_value(s, "recoveryTime")) / 3600.0, 1)
                if _summary_value(s, "recoveryTime") else None
            ),
        })
    return out


def _ms_or_s(v: Any) -> datetime | None:
    """毫秒或秒都认，统一成 tz-aware datetime。认不出来返回 None。"""
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    secs = v / 1000.0 if v > 1e11 else float(v)
    try:
        return datetime.fromtimestamp(secs, tz=timezone.utc).astimezone(TZ)
    except (OSError, OverflowError, ValueError):
        return None


def workouts_to_events(workouts: list[dict[str, Any]], day: Day) -> list[Event]:
    """锻炼 → 两类事件。

    分开是有原因的：

      · **每次锻炼** 用 ``domain="fitness"`` —— 带全部细节，给可视化用的。
        它**故意不进** domain="health"，否则会被 normalize 收进日记的
        `## Health` 里，变成一行没头没尾的「workout: 20.0」。
      · **当天汇总** 用 ``domain="health"`` —— 日记要的是「今天跑了 4.2 公里」，
        不是每次的流水账。
    """
    out: list[Event] = []
    for w in workouts:
        out.append(
            Event(
                ts_utc=w["start"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                domain="fitness",
                metric="workout",
                value_num=w["minutes"],
                value_text=w["name"],
                source="gadgetbridge",
                meta={k: v for k, v in {
                    "kind": w["kind"],
                    "distance_km": w["km"],
                    "end_utc": (w["end"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                if w["end"] else None),
                    "avg_hr": w["avg_hr"],
                    "max_hr": w["max_hr"],
                    "pace_s_per_km": w["pace_s_per_km"],
                    "ascent_m": w["ascent_m"],
                    "calories": w["calories"],
                    "vo2max": w["vo2max"],
                    "training_effect": w["training_effect"],
                    "recovery_hours": w["recovery_hours"],
                }.items() if v is not None},
            )
        )

    if not workouts:
        return out

    ts = day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    total_min = round(sum(w["minutes"] for w in workouts), 1)
    total_km = round(sum(w["km"] for w in workouts), 2)

    def daily(metric: str, value: Any, text: str = "") -> Event:
        return Event(ts_utc=ts, domain="health", metric=metric,
                     value_num=value, value_text=text, source="gadgetbridge")

    out.append(daily("run_count", len(workouts)))
    out.append(daily("run_minutes", total_min))
    out.append(daily("run_km", total_km))
    if total_min > 0 and total_km > 0:
        # 全天合并配速（秒/公里）
        out.append(daily("run_pace_s_per_km", round(total_min * 60.0 / total_km, 1)))
    # VO2max 是当次测得的值，一天跑几次就取最后一次
    vo2 = [w["vo2max"] for w in workouts if w["vo2max"]]
    if vo2:
        out.append(daily("vo2max", round(float(vo2[-1]), 2)))
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

        # 先读锻炼会话：它来自另一张表（时间列是毫秒级 START_TIME，
        # 没有 TIMESTAMP，所以不会出现在候选里），而且**即使没有分钟级采样
        # 也该照样读** —— 不能因为候选表为空就把它一起跳过。
        workouts = read_workouts(conn, day)

        grouped: dict[str, list[tuple[datetime, float]]] = {}
        candidates = find_candidate_tables(conn)
        if not candidates:
            print("FAIL No activity table containing a TIMESTAMP column.")
            print("   Likely causes: the export file is empty, or the device model is not in the known list.")
            print("   Inspect it yourself: sqlite3 <db> \".tables\"")
        else:
            print("-> Candidate tables (has-data first, then metric coverage):")
            for t, _cols, mapping, rows in candidates[:5]:
                note = "" if rows else "   ← 空表，跳过"
                print(f"    {t:40s} rows={rows:<8} matched {len(mapping)} metrics{note}")

            table, cols, mapping, rows = candidates[0]
            print(f"\n-> Using table: {table}  ({rows} rows total)")
            print(f"   Metric mapping: {mapping}")
            grouped = read_table(conn, table, mapping, day)

    events = to_events(grouped, day) + workouts_to_events(workouts, day)

    if not events:
        print(f"\n⚠️  No data for this day. The band may not have been worn, or data has not synced yet.")
        return

    if workouts:
        print(f"\n-> Workouts: {len(workouts)}")
        for w in workouts:
            pace = ""
            if w["pace_s_per_km"]:
                p = float(w["pace_s_per_km"])
                pace = f"  {int(p // 60)}:{int(p % 60):02d}/km"
            hr = f"  HR {w['avg_hr']:.0f}" if w["avg_hr"] else ""
            print(f"    {w['start']:%H:%M}  {w['name']}  {w['km']} km / {w['minutes']:.0f} min{pace}{hr}")

    print(f"\n-> Normalised:")
    for e in events:
        unit = {"steps": "steps", "sleep_hours": "h", "run_km": "km",
                "run_minutes": "min", "run_count": "", "vo2max": "ml/kg/min"}.get(e.metric, "")
        print(f"    {e.metric:24s} {e.value_num}{unit}")

    if args.dry_run:
        print("\n(--dry-run, nothing written)")
        return

    client = EventsClient(CFG)
    if not client.ping():
        print(f"\nFAIL events API is not running ({client.base}）")
        return

    # ⚠️ 先删掉当天这个来源的旧行，再写。
    #
    # 事件表的唯一索引是 (source, domain, metric, ts_utc, value_text) —— **不含 value_num**。
    # 所以同一天第二次写入会被静默忽略，**值不会更新**。
    #
    # 这在手表数据上是致命的：步数、心率、跑步时长都是**当天累计**的，
    # 早上读到 6532 步、晚上读到 12000 步，第二次会被当成重复丢掉，
    # 那天就永远停在早上的数字上。实测踩到过（6532 → 6558 写不进去）。
    #
    # 删了再写就对了，代价是那几个汇总事件重写一遍 —— 很便宜。
    # collect_phone.py 用的也是这个套路。
    try:
        httpx.delete(f"{client.base}/day", headers=client.headers,
                     params={"day": day.iso, "source": "gadgetbridge"}, timeout=30.0)
    except Exception:  # noqa: BLE001
        pass          # 删不掉不至于阻断写入

    for e in events:
        e.source = "gadgetbridge"
    n = client.post_batch(events)
    print(f"\nOK  Written to events table: {n} rows")


if __name__ == "__main__":
    main()
