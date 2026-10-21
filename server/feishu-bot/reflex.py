#!/usr/bin/env python3
"""反射弧：从当天数据里认出一个**现在值得打断你**的状态，推一句飞书。

**它和「问答」的区别**

    问答是你问、它答；这里没人问。它只在一条规则**确定成立**的时候开口，
    而且每条规则一天最多一次。规则是确定性的 —— 没有模型、没有概率、
    不猜「你可能累了」。理由和 live.py 开头那段一样：一个会瞎猜的提醒，
    比没有提醒更快被关掉。

**为什么只提醒，不执行**

    「连续坐了 4 小时就关掉网卡」听着很硬核，但它只要误报一次，代价就是
    你正在做的事被硬生生打断 —— 然后你会把整个系统关掉。所以这里只发一句话，
    不做任何不可逆的动作。

**为什么不用神经网络/预测**

    现在的数据是「6 天、138 条回答」，任何模型都只会学到噪声。等自评标签
    （state/labels.jsonl）攒够 60 天再谈预测。在那之前，确定性的阈值反而更准。

规则和阈值（都可以直接在下面改）：

    late_night    00:30–04:00 之间，手机 20 分钟内还有活动
    screen_heavy  18:00 之后，手机亮屏 ≥ 6 小时
    leisure_heavy 18:00 之后，单个娱乐应用 ≥ 3 小时
    long_desktop  电脑快照新鲜（≤30 分钟）且同一窗口连续 ≥ 3 小时
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

# ── 阈值 ────────────────────────────────────────────────────────────
SCREEN_MIN = int(os.environ.get("REFLEX_SCREEN_MIN", "360"))
LEISURE_MIN = int(os.environ.get("REFLEX_LEISURE_MIN", "180"))
DESKTOP_MIN = int(os.environ.get("REFLEX_DESKTOP_MIN", "180"))
# 电脑快照超过这么久没更新就不算「现在」—— 笔记本一天只醒几小时，
# 一份 5 小时前的快照不能拿来判断「你已经连续坐了 3 小时」。
DESKTOP_FRESH_MIN = int(os.environ.get("REFLEX_DESKTOP_FRESH_MIN", "30"))
# 最近一次手机活动距现在多久算「还在用」。
ACTIVE_MIN = int(os.environ.get("REFLEX_ACTIVE_MIN", "20"))
# 两条提醒之间至少隔这么久。连着响三次的提醒 = 噪音。
MIN_GAP_MIN = int(os.environ.get("REFLEX_MIN_GAP_MIN", "60"))

# 传感器哑了：白天（>=13 点）最近一次手机活动已经过去这么久。
# ⚠️ 这个阈值要能容忍一次长睡。8 小时睡眠是常态，13 小时才值得怀疑 ——
#    而 Tasker 半夜被杀的话，到下午正好就是这个数。
SENSOR_STALE_HOURS = float(os.environ.get("REFLEX_SENSOR_STALE_HOURS", "13"))
SENSOR_CHECK_FROM_HOUR = 13

# 夜里这几个小时只有「熬夜」能说话；其余规则白天才有意义。
QUIET_FROM_HOUR = 23
QUIET_TO_HOUR = 8

# 熬夜规则的窗口（左闭右开）。
LATE_FROM = (0, 30)
LATE_TO_HOUR = 4

# ⚠️ 和 `normalize.PHONE_LEISURE_APPS` **必须一致**。这里不能 import 那份：
#    normalize.py 在 import 时就 load_config()，而机器人要能在配置坏掉时
#    照样起得来（见 live.py 开头的「惰性依赖」）。smoke_test 钉住两份相等。
LEISURE_APPS = frozenset({
    "WeChat", "QQ", "Weibo", "Douyin", "Bilibili", "Xiaohongshu",
    "Taobao", "JD", "Pinduoduo", "Xianyu", "NetEase Music",
})

_ACTIVITY_METRICS = frozenset({"unlock", "screen_on", "app_switch"})

# 提醒发出后，在这些时间点各记一次「后来发生了什么」。
#
# ⚠️ 只记录，不评价。15 分钟后解锁 0 次可能表示「你听了」，也可能表示
#    「你把手机放桌上走了」—— 现在下结论就是编故事。等积累了足够的
#    「提醒 → 行为」配对，才谈得上「哪条提醒有效」。
OUTCOME_WINDOWS = (15, 60)


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else None


def _hm(minutes: float) -> str:
    total = int(round(minutes))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} 小时 {m} 分"
    if h:
        return f"{h} 小时"
    return f"{m} 分"


def _last_activity(events: list[dict]) -> datetime | None:
    """最近一次手机活动的时间。**只看活动类事件** —— 位置事件也会持续上报，
    拿它当「他还在用手机」等于一晚都在响。"""
    ts = [_dt(e.get("ts_utc")) for e in events
          if isinstance(e, dict) and e.get("metric") in _ACTIVITY_METRICS]
    ts = [t for t in ts if t is not None]
    return max(ts) if ts else None


def _quiet(now: datetime) -> bool:
    return now.hour >= QUIET_FROM_HOUR or now.hour < QUIET_TO_HOUR


def _late(now: datetime) -> bool:
    """现在是不是熬夜窗口。跨零点写成「分钟数」比较，避免 23 → 0 的日界问题。"""
    minutes = now.hour * 60 + now.minute
    return LATE_FROM[0] * 60 + LATE_FROM[1] <= minutes < LATE_TO_HOUR * 60


def _alert(key: str, title: str, lines: list[str]) -> dict:
    return {"key": key, "text": f"⚡ 反射弧 · {title}\n" + "\n".join(lines)}


def evaluate(now: datetime, day_iso: str, *, events: list[dict],
             phone: dict | None, snap: dict | None, state: dict,
             awaiting: bool = False) -> list[dict]:
    """返回**现在该发**的提醒（通常 0 或 1 条）。纯函数：不取数、不发消息。

    `state` 的形状：``{"day": "...", "fired": {"<key>": "<iso>"},
                       "last_at": "<iso>"}``

    ⚠️ `awaiting=True`（正有一道题在等答案）时**什么都不发**：
       飞书里那条提醒会被当成题目的答案，而你刚打了一半的字就没了。
       提醒晚一轮不要紧，答案丢了补不回来。

       机器人那边的判据比这个布尔更细：它看「这题是什么时候发的」，
       只拦最近一小时内的题 —— 昨晚 23:00 没答的题会一直挂在 awaiting 上，
       拿它挡的话凌晨的熬夜提醒就永远发不出去（见 feishu_bot.reflex_tick）。
    """
    if awaiting:
        return []

    fired: dict[str, str] = {}
    if state.get("day") == day_iso and isinstance(state.get("fired"), dict):
        fired = dict(state["fired"])

    # 全局最小间隔：连续响的提醒会被静音，然后连真正重要的那条也一起错过。
    last_at = _dt(state.get("last_at"))
    if last_at is not None and now - last_at < timedelta(minutes=MIN_GAP_MIN):
        return []

    alerts: list[dict] = []

    def take(key: str, build: Any) -> None:
        if key in fired:
            return
        a = build()
        if a:
            alerts.append(a)

    # 1. 熬夜 —— 唯一一条允许在夜里响的规则。
    if _late(now):
        def _late_alert() -> dict | None:
            last = _last_activity(events)
            if last is None or now - last > timedelta(minutes=ACTIVE_MIN):
                return None
            lines = [f"现在 {now:%H:%M}，手机 {max(0, int((now - last).total_seconds() // 60))} 分钟前还在用。"]
            if phone and phone.get("screen_minutes"):
                lines.append(f"今天亮屏已经 {_hm(phone['screen_minutes'])}。")
            lines.append("要睡就现在睡 —— 再刷下去，明天的第一段专注多半就没了。")
            return _alert("late_night", "熬夜", lines)
        take("late_night", _late_alert)

    # 白天/傍晚的规则：静音时段不说话。
    if not _quiet(now):
        # 2. 手机亮屏总量
        if now.hour >= 18 and phone:
            def _screen_alert() -> dict | None:
                minutes = float(phone.get("screen_minutes") or 0)
                if minutes < SCREEN_MIN:
                    return None
                lines = [f"今天手机亮屏 {_hm(minutes)}（阈值 {_hm(SCREEN_MIN)}）。"]
                top = phone.get("top_apps") or []
                if top:
                    lines.append(f"最多的是 {top[0][0]} {_hm(top[0][1])}。")
                return _alert("screen_heavy", "屏幕时间", lines)
            take("screen_heavy", _screen_alert)

            # 3. 单个娱乐应用
            def _leisure_alert() -> dict | None:
                top = [(str(a), float(m)) for a, m in
                       (phone.get("top_apps") or [])
                       if str(a) in LEISURE_APPS]
                if not top:
                    return None
                app, minutes = max(top, key=lambda x: x[1])
                if minutes < LEISURE_MIN:
                    return None
                return _alert("leisure_heavy", "娱乐时长", [
                    f"{app} 今天 {_hm(minutes)}（阈值 {_hm(LEISURE_MIN)}）。",
                ])
            take("leisure_heavy", _leisure_alert)

        # 4. 电脑前连续太久了
        if snap:
            def _desktop_alert() -> dict | None:
                pushed = _dt(snap.get("pushed_at"))
                if pushed is None or now - pushed > timedelta(minutes=DESKTOP_FRESH_MIN):
                    return None
                cur = snap.get("current")
                if not isinstance(cur, dict):
                    return None
                minutes = float(cur.get("minutes") or 0)
                if minutes < DESKTOP_MIN:
                    return None
                app = str(cur.get("app") or "").strip() or "这个窗口"
                ago = max(0, int((now - pushed).total_seconds() // 60))
                return _alert("long_desktop", "久坐", [
                    f"{app} 已经连续 {_hm(minutes)}，快照 {ago} 分钟前。",
                    "站起来走五分钟再继续。",
                ])
            take("long_desktop", _desktop_alert)

        # 5. 传感器哑了 —— 先让系统感知自己的感官。
        #
        # ⚠️ 这是唯一一条「关于系统自己」的规则。没有它，Tasker 半夜被杀
        #    这种事只会表现为「今天数据很少」，然后被当成「今天很安静」。
        if now.hour >= SENSOR_CHECK_FROM_HOUR:
            def _sensor_alert() -> dict | None:
                if not events:
                    return None       # 一条都没有：多半是刚部署，不吓人
                last = _last_activity(events)
                if last is None:
                    return None
                gap = now - last
                if gap < timedelta(hours=SENSOR_STALE_HOURS):
                    return None
                return _alert("sensor_silent", "传感器", [
                    f"手机事件已经 {_hm(gap.total_seconds() / 60)} 没更新"
                    f"（最后一条 {last:%H:%M}）。",
                    "要么手机没电，要么 Tasker 被系统杀了 —— 查一下。",
                ])
            take("sensor_silent", _sensor_alert)

    # 一次只发一条：同一轮堆三条会像系统坏了。其余的下一轮再看。
    return alerts[:1]


# ── 提醒之后的观察（反馈回路）────────────────────────────────────────
def outcome_row(alert: dict, window_min: int, now: datetime,
                events: list[dict]) -> dict | None:
    """一条已发出的提醒，在 +N 分钟时观察到的手机行为。

    窗口没到就返回 None（**不提前记**）；提醒本身没有可解析的时间也返回
    None（不猜）。窗口是左闭右闭：[发出, 发出+N 分钟]。
    """
    start = _dt(alert.get("ts"))
    if start is None:
        return None
    end = start + timedelta(minutes=window_min)
    if now < end:
        return None

    unlocks = switches = screen_off = activity = 0
    for e in events:
        if not isinstance(e, dict):
            continue
        t = _dt(e.get("ts_utc"))
        if t is None or not (start <= t <= end):
            continue
        metric = e.get("metric")
        if metric == "unlock":
            unlocks += 1
        elif metric == "app_switch":
            switches += 1
        if metric == "screen_off":
            screen_off += 1
        if metric in _ACTIVITY_METRICS or metric == "screen_off":
            activity += 1

    return {
        "kind": "outcome",
        "id": str(alert.get("id") or ""),
        "rule": str(alert.get("rule") or ""),
        "window_min": window_min,
        "window_start": start.isoformat(timespec="seconds"),
        "window_end": end.isoformat(timespec="seconds"),
        "recorded_at": now.isoformat(timespec="seconds"),
        "unlocks": unlocks,
        "switches": switches,
        "screen_off": screen_off,
        "activity": activity,
    }


def pending_outcomes(log_rows: list[dict], now: datetime,
                     events: list[dict]) -> list[dict]:
    """从日志里找出「该观察但还没观察」的提醒 → 要追加的 outcome 行。

    已经记过的 (id, window) 不重复记。日志坏了、id 缺了都跳过 ——
    这是观测数据，宁可少一行，也不要在同一窗口上写两次。
    """
    done: set[tuple[str, int]] = set()
    sent: list[dict] = []
    for r in log_rows:
        if not isinstance(r, dict):
            continue
        if r.get("kind") == "outcome":
            try:
                w = int(r.get("window_min"))
            except (TypeError, ValueError):
                continue
            done.add((str(r.get("id") or ""), w))
        elif r.get("kind") == "sent" and r.get("id"):
            sent.append(r)

    out: list[dict] = []
    for s in sent:
        for w in OUTCOME_WINDOWS:
            if (str(s["id"]), w) in done:
                continue
            row = outcome_row(s, w, now, events)
            if row:
                out.append(row)
    return out
