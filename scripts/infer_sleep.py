#!/usr/bin/env python3
"""睡眠窗口 —— 从手机静默里推断出来，再用飞书的一句话校正。

**为什么不是从手表来**

    Gadgetbridge 里那台 Garmin Forerunner 235 **没有睡眠功能**：
    `GARMIN_ACTIVITY_SAMPLE` 只有 STEPS / HEART_RATE / DISTANCE_CM /
    ACTIVE_CALORIES 四列，`GARMIN_SLEEP_STAGE_SAMPLE`、
    `GARMIN_SLEEP_STATS_SAMPLE`、`GARMIN_NAP_SAMPLE` 全是 0 行。
    所以「几点睡几点起」在这个项目里不可能是手表给的，只能从别处来。

**手机静默为什么可信**

    人睡着时手机不动。一整晚没有任何解锁 / 切应用 / 亮屏，那一段空白
    就是睡眠窗口。它不如手环的睡眠识别细（没有深浅睡分期），但
    「几点睡、几点起、睡了多久」这三件事上的误差通常在十几分钟内，
    而且不需要任何新硬件。

    但它是**推断**，不是观测：手机没电、忘在客厅、半夜起来刷十分钟，
    都会让它偏。所以每一行都带 `basis` 和 `confidence` ——
    日记提示词里不能把它当成事实来写。

**手动覆盖**

    在飞书发 `睡：23:40-08:10`，机器人把这句话原样存成 `manual/sleep`。
    本脚本**优先用它**，标成 `basis="manual"`；没有手动记录才做推断。
    手动的永远盖过推断的 —— 和 `labels.jsonl` 里「本人打分优先」同一条原则。

**归属哪一天**

    和 Oura / WHOOP 一致：**一夜归属于它醒来的那天**。
    23:40 睡、08:10 起的那一夜算「今天早上这次睡眠」，不是昨天。

    所以取数窗口是 [D-1 18:00, D 12:00)，只取**醒在 D 日上午**的那一段。
    这和 `day_local` 按自然日切分是两回事 —— 那个切法会把一夜劈成两半，
    睡前那一小时记到前一天去。

**只写自己这个 source**

    全部行用 `source="sleep"`。重跑时先删这个 source 当天的旧行再写，
    所以推断值改了会跟着改，不会留下两条。

用法：
  python scripts/infer_sleep.py --date today
  python scripts/infer_sleep.py --date 2026-10-20 --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Day, Event, EventsClient, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])

# 一个「睡眠日」从中午 12 点开始。这和 Oura / WHOOP 的切法一致：
# 中午之前醒来的都算这一夜，午睡不会把整夜挤到前一天去。
WRAP_HOUR = 12

# 短于这个时长的静默不算睡眠 —— 上厕所、开会、手机放桌上都会有好几小时。
MIN_GAP = timedelta(hours=3)

# 候选静默段必须开始于前一天的这个钟点之后。晚上七八点不碰手机
# （人在电脑前）是常态，那段时间不能当入睡。
NIGHT_START_HOUR = 20

# 起床时间落在这个区间里才算「这一夜醒了」。凌晨 3 点醒的多半是起夜，
# 下午醒的多半是午睡 —— 两个都不该算成当天的睡眠。
WAKE_EARLIEST_HOUR = 4

# 两段静默之间只有这么短的「活动」，算同一次起夜，合并成一夜。
# 半夜看眼手机 2 分钟不该把一夜切成两半。
MERGE_ACTIVITY = timedelta(minutes=30)

# 超过这个就不写。宁可缺一天，也不要往数据里塞一条假的睡眠。
#
# 14 小时是上限的原因：病后补觉能到 12–13 小时，再长就不是睡眠了。
# 而「08:10-23:40」这种写反的记录会解出 15.5 小时 —— 靠这个上限挡住。
MAX_GAP = timedelta(hours=14)

SOURCE = "sleep"

# `23:40-08:10` / `23:40~08:10` / `23:40 08:10` / `0:40-8:10` 都认
_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
_PAIR_RE = re.compile(r"^\s*(\d{1,2}\s*[:：]\s*\d{2})\s*[-~—–至到\s]\s*(\d{1,2}\s*[:：]\s*\d{2})\s*$")


# ── 手动那一条 ──────────────────────────────────────────────────────


def parse_manual(text: str, day: Day) -> tuple[datetime, datetime] | None:
    """把 `23:40-08:10` 解析成两个 tz-aware 时刻。

    起床时间一律算在 day 当天；入睡时间**早于中午**就算当天凌晨
    （02:00-08:00 那种），**晚于中午**就算前一天（23:40-08:10）。
    这条规则只在中午前后各错一次，而人几乎不会在中午入睡。
    """
    m = _PAIR_RE.match(text or "")
    if not m:
        return None
    parts = []
    for raw in m.groups():
        t = _TIME_RE.search(raw)
        if not t:
            return None
        hour, minute = int(t.group(1)), int(t.group(2))
        if hour > 23 or minute > 59:
            return None
        parts.append(time(hour, minute))

    onset_t, wake_t = parts
    wake = datetime.combine(day.d, wake_t, tzinfo=TZ)
    onset_day = day.d - timedelta(days=1) if onset_t.hour >= WRAP_HOUR else day.d
    onset = datetime.combine(onset_day, onset_t, tzinfo=TZ)
    if wake <= onset:
        # 23:40-08:10 被写反了方向，或者起床时间早于入睡时间
        return None
    if wake - onset > MAX_GAP:
        # 例子：「08:10-23:40」按上面的规则会解成同一天的 08:10 到 23:40，
        # 15 个半小时。这不是睡眠，是写错了 —— 宁可丢掉也不要写进数据。
        return None
    return onset, wake


def manual_window(events: list[dict], day: Day) -> tuple[datetime, datetime] | None:
    """找当天有没有飞书发来的手动睡眠记录。有就返回它（取最后一条）。"""
    found = None
    for e in events:
        if e.get("domain") != "manual" or e.get("metric") != "sleep":
            continue
        parsed = parse_manual(str(e.get("value_text") or ""), day)
        if parsed:
            found = parsed
    return found


# ── 手机静默 ────────────────────────────────────────────────────────


def phone_activity(events: list[dict], lo: datetime, hi: datetime) -> list[datetime]:
    """窗口内所有手机活动的时刻。

    不看 metric，任何一条 phone 事件都算「在用手机」：解锁、切应用、
    亮屏、息屏都行。息屏也是活动 —— 它恰好说明人还醒着。
    """
    out: list[datetime] = []
    for e in events:
        if e.get("domain") != "phone":
            continue
        ts = _parse_ts(e.get("ts_utc"))
        if ts and lo <= ts < hi:
            out.append(ts)
    out.sort()
    return out


def desktop_activity(lo: datetime, hi: datetime) -> list[datetime]:
    """ActivityWatch 的窗口切片 → 活动时刻（段的开始和结束各算一个）。

    **为什么要把电脑也算进来**

        只靠手机的话，「晚上在电脑前没碰手机」和「睡着了」长得一模一样。
        实测最常见的情形就是：21 点开始写代码，手机扔一边，
        到凌晨 1 点才去睡 —— 只看手机，这一夜会从 21 点算起，多出 4 小时。

        电脑的窗口切片正好补上这一段。它拿不到就算了（AW 没开、
        笔记本睡着了），退化成只看手机，不会报错。
    """
    base = ((CFG.get("sources") or {}).get("activitywatch") or {}).get("api_base") or ""
    if not base:
        return []
    base = base.rstrip("/")
    try:
        buckets = httpx.get(f"{base}/api/0/buckets/", timeout=10.0).json()
    except Exception:  # noqa: BLE001
        return []

    out: list[datetime] = []
    for bucket in [b for b in buckets if b.startswith("aw-watcher-window_")]:
        try:
            evs = httpx.get(
                f"{base}/api/0/buckets/{bucket}/events",
                params={
                    "start": lo.astimezone(timezone.utc).isoformat(),
                    "end": hi.astimezone(timezone.utc).isoformat(),
                    "limit": -1,
                },
                timeout=60.0,
            ).json()
        except Exception:  # noqa: BLE001
            continue
        for ev in evs:
            data = ev.get("data") or {}
            if not str(data.get("app") or "").strip():
                continue
            dur = float(ev.get("duration") or 0)
            if dur <= 0 or dur > 4 * 3600:
                continue
            ts = _parse_ts(ev.get("timestamp"))
            if ts is None or not (lo <= ts < hi):
                continue
            out.append(ts)
            end = ts + timedelta(seconds=dur)
            if end < hi:
                out.append(end)
    out.sort()
    return out


def night_gaps(activity: list[datetime], day: Day) -> list[list[datetime]]:
    """活动时刻之间的空白 → 候选的「夜间静默段」。

    这里有三道过滤，每一道都是为了排除一类误判 —— 单看「静默够长」
    会把「晚上在电脑前没碰手机」当成睡觉：

      1. **必须够长**（MIN_GAP）。三小时以内的空白是日常，不是睡眠。
      2. **必须落在夜里**（开始于前一天 NIGHT_START_HOUR 之后）。
         晚上 18:00–21:00 不碰手机是常事，那段时间不能当入睡。
      3. **只有跨过午夜的那一段才能往后合并**。第二天凌晨 3 点看了眼
         手机，活动只有 10 分钟 —— 该并进同一夜；但前一晚 20:00–23:40
         那段空白虽然挨着，它结束在前一天，不能并进来，否则入睡时间
         会被拽到晚饭后。
    """
    gaps: list[list[datetime]] = []
    for a, b in zip(activity, activity[1:]):
        if b - a >= MIN_GAP:
            gaps.append([a, b])

    night_start = datetime.combine(day.d - timedelta(days=1), time(NIGHT_START_HOUR), tzinfo=TZ)
    gaps = [g for g in gaps if g[0] >= night_start]

    merged: list[list[datetime]] = []
    for g in gaps:
        prev = merged[-1] if merged else None
        burst = (g[0] - prev[1]) if prev is not None else None
        if (burst is not None
                # `burst > 0` 是必要的：两段空白紧贴在一起时，中间只有
                # **一个**活动点，那既可能是「起夜看了一眼」也可能是
                # 「醒了又赖床」—— 合并会让 08:00 醒来的人被算到 14:00。
                # 真正能确认是起夜的，是中间有 0–30 分钟的活动。
                and timedelta(0) < burst <= MERGE_ACTIVITY
                and prev[1].astimezone(TZ).date() == day.d):
            prev[1] = g[1]                  # 起夜，并回同一夜
        else:
            merged.append(g)
    return merged


def pick_night(gaps: list[list[datetime]], day: Day) -> tuple[datetime, datetime] | None:
    """在候选段里挑出「醒在今天早上」的那一夜。"""
    best = None
    for onset, wake in gaps:
        local_wake = wake.astimezone(TZ)
        if local_wake.date() != day.d:
            continue                        # 醒的不是这一天
        if not (WAKE_EARLIEST_HOUR <= local_wake.hour < WRAP_HOUR):
            continue                        # 凌晨 3 点醒的、下午才醒的，都不是这一夜
        if wake - onset > MAX_GAP:
            continue                        # 手机没电 / 没带，宁可缺一天
        if best is None or (wake - onset) > (best[1] - best[0]):
            best = (onset, wake)
    return best


# ── 写事件 ──────────────────────────────────────────────────────────


def _parse_ts(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _minutes(delta: timedelta) -> float:
    return round(delta.total_seconds() / 60.0, 1)


def build_events(onset: datetime, wake: datetime, basis: str, confidence: str,
                 day: Day, note: str = "") -> list[Event]:
    """一夜 → 一组日汇总事件。

    `sleep_onset_min` 用「距前一天中午的分钟数」而不是「距午夜的分钟数」，
    因为 23:40 和 00:10 在午夜口径下是一个在末尾一个在开头，
    算入睡时刻的标准差（也就是睡眠规律性）会得出一个荒唐的数。
    从中午 12:00 起算之后，两者是 700 和 730，是一条连续的数列。
    """
    ts = day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    anchor = datetime.combine(day.d - timedelta(days=1), time(WRAP_HOUR), tzinfo=TZ)

    # ⚠️ 传进来的 onset / wake 多半是 **UTC**（事件表和 ActivityWatch 都给 UTC）。
    #    所有给人看的钟点都必须先转成本地时区，否则日志和 value_text 里
    #    会出现「15:29→00:40」这种明显对不上的时刻 —— 实测踩过一次：
    #    分钟数算对了，只有显示是 UTC，看着像推断错了。
    onset_l = onset.astimezone(TZ)
    wake_l = wake.astimezone(TZ)

    onset_min = _minutes(onset - anchor)
    wake_min = _minutes(wake - datetime.combine(day.d, time(0), tzinfo=TZ))
    mid = onset + (wake - onset) / 2
    mid_min = _minutes(mid - anchor)
    hours = round((wake - onset).total_seconds() / 3600.0, 2)

    meta = {
        "onset_utc": onset.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wake_utc": wake.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "onset_local": onset_l.strftime("%H:%M"),
        "wake_local": wake_l.strftime("%H:%M"),
        "basis": basis,
        "confidence": confidence,
        "window_hours": hours,
    }
    if note:
        meta["note"] = note

    def ev(metric: str, value: float, text: str = "", extra: dict | None = None) -> Event:
        m = dict(meta)
        if extra:
            m.update(extra)
        return Event(ts_utc=ts, domain="health", metric=metric,
                     value_num=value, value_text=text, source=SOURCE, meta=m)

    return [
        ev("sleep_window", hours, f"{onset_l:%H:%M}→{wake_l:%H:%M}"),
        ev("sleep_hours", hours),
        ev("sleep_onset_min", onset_min, onset_l.strftime("%H:%M")),
        ev("sleep_wake_min", wake_min, wake_l.strftime("%H:%M")),
        ev("sleep_midpoint_min", mid_min, mid.astimezone(TZ).strftime("%H:%M")),
    ]


def main() -> int:
    from common import cli_day

    ap = argparse.ArgumentParser(description="推断睡眠窗口并写入事件表")
    ap.add_argument("--date", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    day = Day.parse(args.date or cli_day(), TZ)
    header(f"Infer sleep · {day.iso}")

    client = EventsClient(CFG)
    if not client.ping():
        print(f"⚠️  events API 没在跑（{client.base}），无法推断睡眠")
        return 1

    lo = datetime.combine(day.d - timedelta(days=1), time(18, 0), tzinfo=TZ)
    hi = datetime.combine(day.d, time(WRAP_HOUR), tzinfo=TZ)

    try:
        events = client.query(start=lo, end=hi, limit=200_000)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  查事件失败：{e}")
        return 1

    # 1. 手动优先
    manual = manual_window(events, day)
    if manual:
        onset, wake = manual
        basis, confidence = "manual", "high"
        print(f"  ✓ 用飞书手动记录：{onset.astimezone(TZ):%m-%d %H:%M} → "
              f"{wake.astimezone(TZ):%m-%d %H:%M}")
    else:
        phone = phone_activity(events, lo, hi)
        desktop = desktop_activity(lo, hi)
        activity = sorted(phone + desktop)
        if not activity:
            print("  . 夜里没有任何活动记录 —— 手机没带/没电，电脑也没开，或者中继没在跑")
            _clear(client, day, args.dry_run)
            return 0
        print(f"  · 活动点：手机 {len(phone)}，电脑 {len(desktop)}"
              + ("（ActivityWatch 没开，只用手机判断）" if not desktop else ""))
        night = pick_night(night_gaps(activity, day), day)
        if not night:
            print(f"  . 没找到够长的静默段（阈值 {MIN_GAP}），今天按缺数据算")
            _clear(client, day, args.dry_run)
            return 0
        onset, wake = night
        hours = (wake - onset).total_seconds() / 3600.0
        # 3–5 小时可能只是手机没电/忘在客厅；6 小时以上更像真的睡了
        confidence = "high" if hours >= 6 else "medium"
        basis = "phone_silence"
        print(f"  ✓ 手机静默推断：{onset.astimezone(TZ):%m-%d %H:%M} → "
              f"{wake.astimezone(TZ):%m-%d %H:%M}"
              f"（{hours:.1f}h，置信度 {confidence}）")

    events_out = build_events(onset, wake, basis, confidence, day)
    for e in events_out:
        print(f"    {e.metric:20s} {e.value_num}  {e.value_text}")

    if args.dry_run:
        print("\n(--dry-run，不写库)")
        return 0

    _clear(client, day, dry_run=False)
    n = client.post_batch(events_out)
    print(f"\nOK  写入 {n} 行（source={SOURCE}）")
    return 0


def _clear(client: EventsClient, day: Day, dry_run: bool) -> None:
    """删掉当天该 source 的旧行。

    ⚠️ 和 collect_phone.py / ingest_gadgetbridge.py 同一个理由：唯一索引
       (source, domain, metric, ts_utc, value_text) **不含 value_num**，
       不先删的话，睡得晚了一小时重跑也写不进去，那天会永远停在第一次的推断上。
    """
    if dry_run:
        return
    try:
        httpx.delete(f"{client.base}/day", headers=client.headers,
                     params={"day": day.iso, "source": SOURCE}, timeout=30.0)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())
