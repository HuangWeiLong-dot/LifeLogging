#!/usr/bin/env python3
"""把生活活动的时长题答案变成 `manual/activity` 事件。

**数据从哪来**

    服务器侧 `live_questions.py` 在 13:00 / 19:00 / 21:00 各发一道选择题
    （午饭、晚饭、洗澡/家务/买菜），选项直接是 15/30/45/60+ 分钟。
    答案经中继落成 `question/answer` 事件，`qa_client` 再把问题原文和答案
    对齐成 `state/qa-<day>.json` —— **这里是唯一能同时看到题面和答案的地方**。

**为什么不写成「另一个 collector」**

    答案不是传感器给的，是**人自己选的**。所以落成 `manual/activity`，
    和 `manual/note` / `manual/sleep` 一样，在 normalize 里比任何推断都优先。

**幂等**

    按 (day, source) 先删后插 —— 和采集器同一套约定，随便重跑。
    每题只写一条事件；没答的时段什么都不写（**不补 0**，0 分钟是一个
    观测值，沉默不是）。

**不猜**

    选项原文里没有数字 → 跳过并打印，不按题面估一个数。
    题面标记对不上（见 `life_slots.TAG`）→ 跳过，说明生成侧被改过。

用法：
  python scripts/parse_life_activity.py --date today
  python scripts/parse_life_activity.py --date 2026-10-30 --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

import life_slots  # noqa: E402
from common import Day, Event, EventsClient, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])
STATE = Path(CFG["state_dir"])

SOURCE = "life-activity"
DOMAIN = "manual"
METRIC = "activity"

# 答案可能第二天早上才写回来（实时题在白天发出，人不一定当场答），
# 所以生成事件时要看**作答日**的 qa 文件，而事件归到**提问日**。
SCAN_DAYS = 3


def _load_qa(day_iso: str) -> dict | None:
    p = STATE / f"qa-{day_iso}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ⚠️  {p.name} 读不出来：{type(e).__name__}: {e}")
        return None


def events_for_days(days: list[str]) -> dict[str, list[Event]]:
    """扫这几天的 qa 文件 → {提问日: [事件]}。

    只认识 `life_slots` 标记的题；同一题重答取最后一次（ts_utc 最大）。
    """
    by_day: dict[str, list[Event]] = defaultdict(list)
    best: dict[str, tuple[str, dict, dict]] = {}   # qid → (ts, qa, slot)

    for day_iso in days:
        payload = _load_qa(day_iso)
        if not payload:
            continue
        for row in payload.get("qa") or []:
            parsed = life_slots.parse_evidence(row.get("evidence") or "")
            if not parsed:
                continue
            slot, _start, _end = parsed
            answer = str(row.get("answer") or "").strip()
            if not answer:
                continue
            qid = str(row.get("id") or "")
            qday = str(row.get("question_day") or (qid.split("-")[0] if qid else ""))
            if not qid or len(qday) != 10:
                continue
            ts = str(row.get("ts_utc") or "")
            if qid in best and ts <= best[qid][0]:
                continue
            best[qid] = (ts, row, {**slot, "_day": qday})

    for qid, (_ts, row, slot) in best.items():
        minutes = life_slots.minutes_from_label(row.get("answer") or "")
        if minutes is None:
            print(f"  ⚠️  认不出时长，跳过：{row.get('answer')!r}（{qid}）")
            continue
        span = life_slots.interval(slot["_day"], slot, minutes, TZ)
        if span is None:
            # None 选项 / 0 分钟：没有活动。这是**答过**，不是缺数据 ——
            # 所以也不写 0 分钟的事件，避免 Life 里出现一条 0 的伪记录。
            continue
        start, end = span
        by_day[slot["_day"]].append(Event(
            ts_utc=life_slots.to_utc_iso(start),
            domain=DOMAIN,
            metric=METRIC,
            value_num=float(minutes),
            value_text=str(slot["label"]),
            source=SOURCE,
            meta={
                "kind": slot["kind"],
                "slot": slot["key"],
                "label": slot["label"],
                "minutes": minutes,
                "start_utc": life_slots.to_utc_iso(start),
                "end_utc": life_slots.to_utc_iso(end),
                "qid": qid,
            },
        ))

    for evs in by_day.values():
        evs.sort(key=lambda e: e.ts_utc)
    return dict(by_day)


def sync(days: list[str], dry_run: bool = False) -> int:
    """重写这些天的生活活动事件。返回写入行数。"""
    by_day = events_for_days(days)
    client = EventsClient(CFG)
    if not client.ping():
        print(f"  ✗ events API 不可达（{client.base}），跳过生活活动同步")
        return 0

    total = 0
    for day_iso in sorted(by_day):
        evs = by_day[day_iso]
        print(f"  · {day_iso}: {len(evs)} 条活动"
              + ("".join(f"  {e.value_text} {e.value_num:g}min" for e in evs[:4])))
        if dry_run:
            continue
        try:
            httpx.delete(
                f"{client.base}/day",
                params={"day": day_iso, "source": SOURCE},
                headers=client.headers, timeout=30.0,
            ).raise_for_status()
            total += client.post_batch(evs)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  {day_iso} 写入失败：{type(e).__name__}: {e}")
    return total


def sync_recent(end: Day, days: int = SCAN_DAYS, dry_run: bool = False) -> int:
    """给 `collect_phone_events` 调：扫最近几天，答案迟到的也能补上。"""
    window = [(end.d - timedelta(days=i)).isoformat() for i in range(days)]
    return sync(window, dry_run=dry_run)


def main() -> int:
    ap = argparse.ArgumentParser(description="生活活动时长题答案 → manual/activity")
    ap.add_argument("--date", default="today")
    ap.add_argument("--days", type=int, default=SCAN_DAYS,
                    help=f"往前扫几天（默认 {SCAN_DAYS}，覆盖答案迟到的情况）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    end = Day.parse(args.date, TZ)
    header(f"Life activity · {end.iso}")
    n = sync_recent(end, days=max(1, args.days), dry_run=args.dry_run)
    print(f"\n{'OK ' if n else '.  '}{n} 条活动事件"
          + ("（dry-run，未写库）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
