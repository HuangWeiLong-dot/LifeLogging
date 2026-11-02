#!/usr/bin/env python3
"""生活活动的**时长题**：饭点 / 晚间各问一句，答案落成 Life 维度里的时间。

**为什么要有它**

    七维里的 Life 一直是「Life 类应用用了多久」——淘宝、支付宝、美团。
    也就是说：坐在食堂吃一小时饭，只要没掏手机，Life 就是 0。
    营养那一路（`manual/nutrition`）只记了吃什么，没记吃多久。

    所以这里补一条**确定性的提问 + 确定性的解析**：

      1. 服务器上 `live_questions.py` 在固定时段发一道选择题
         （午饭 / 晚饭 / 晚间杂事），选项直接是时长；饭点题同时问
         「有没有吃 / 吃了什么」，食物写在选项后的备注里。
         **它不再替换那个整点的小时题** —— 小时题照常先问，答完再问它。
      2. 笔记本侧 `parse_life_activity.py` 把答案读回来，写成
         `manual/activity` 事件（带开始时间和分钟数，备注进 meta.food）；
      3. `normalize` 把它放进 timeline，`publish_life_site` 把它并进
         Life 维度和 collapsedHours 的并集。

    **不上 LLM**：题面固定、选项固定、映射固定，可测、零成本，也不会
    把「随便写的 40 分钟」猜成一个数。

**为什么这些时段**

    · 午饭 13:00 问 11:30–13:30（题在饭后的整点发，答案才准确）
    · 晚饭 19:00 问 17:30–19:30
    · 晚间 21:00 问 19:30–21:00（洗澡 / 家务 / 买菜这类）
    22:00 不能放：`live_questions._summary_soon()` 从 22:30 起就停发小时题，
    收尾题会把它挤掉。

**答案怎么变成区间**

    选 30 min、题面是 11:30–13:30 → 区间取 **13:00–13:30**（贴着问的时刻
    往前推）。不取「11:30 开始」：那时候多半还没吃饭。超过窗口长度就裁到
    窗口起点。

用法：
  from life_slots import SLOTS, slot_for_hour, question_text, evidence_text, \
      minutes_from_label, interval, parse_evidence
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# id / evidence 里的固定标记。**解析和生成都只能从这里取** ——
# 两边各写一份字符串，改一次就静默对不上，答案会变成「没人认得的题」。
TAG = "Life activity slot"

# key      稳定标识（进题号、进 meta）
# hour     在哪个整点问（本地时间）
# start/end 这个问题的**时间窗**（本地时间，HH:MM）
# kind     粗分类：meal / chore / errand
# label    给人的说法，同时是事件里的 value_text
SLOTS: tuple[dict, ...] = (
    {"key": "lunch", "hour": 13, "start": "11:30", "end": "13:30",
     "kind": "meal", "label": "lunch"},
    {"key": "dinner", "hour": 19, "start": "17:30", "end": "19:30",
     "kind": "meal", "label": "dinner"},
    {"key": "evening", "hour": 21, "start": "19:30", "end": "21:00",
     "kind": "chore", "label": "shower / chores / errands"},
)

SLOT_BY_KEY = {s["key"]: s for s in SLOTS}

# 选项就是答案本身：value 存的是 label 原文，所以数字必须写在开头。
# ⚠️ "None" 必须在第一位：选择题的答案回写靠 label 原文匹配。
OPTIONS = (
    {"label": "None", "done": False},
    {"label": "15 min", "done": False},
    {"label": "30 min", "done": False},
    {"label": "45 min", "done": False},
    {"label": "60+ min", "done": False},
)

_EVIDENCE_RE = re.compile(
    rf"{re.escape(TAG)}\s*·\s*(?P<key>[a-z]+)\s*·\s*"
    r"(?P<start>\d{2}:\d{2})\s*[–-]\s*(?P<end>\d{2}:\d{2})"
)


def slot_for_hour(hour: int) -> dict | None:
    for slot in SLOTS:
        if slot["hour"] == hour:
            return slot
    return None


def question_text(slot: dict) -> str:
    # ⚠️ 和 live_questions 的其它题一样：只写具体的时间区间，不写
    #    today / yesterday —— 题可能隔天才被读到。
    #
    # 饭点题要一次问清三件事：有没有吃 / 吃了什么 / 花了多久。
    # 「吃了什么」写在选项后面的备注里（机器人提示 reply 1-5 + note），
    # 落到 `manual/activity` 的 meta.food，进日记；时长仍然由选项决定。
    if slot["kind"] == "meal":
        return (f"{slot['start']}–{slot['end']} · "
                f"{slot['label']}: did you eat? What did you have? "
                "How long did it take? (reply with the minutes, then the food "
                "after the number, e.g. “2 rice and chicken”)")
    return (f"{slot['start']}–{slot['end']} · how long did "
            f"{slot['label']} take?")


def evidence_text(slot: dict, day_iso: str) -> str:
    return (f"{TAG} · {slot['key']} · {slot['start']}–{slot['end']} "
            f"({day_iso})")


def parse_evidence(text: str) -> tuple[dict, str, str] | None:
    """evidence → (slot, start_hhmm, end_hhmm)。不是生活活动的题返回 None。"""
    m = _EVIDENCE_RE.search(str(text or ""))
    if not m:
        return None
    slot = SLOT_BY_KEY.get(m.group("key"))
    if not slot or slot["start"] != m.group("start") or slot["end"] != m.group("end"):
        # 时段对不上说明题面被改过 —— 不猜，交给人工看
        return None
    return slot, slot["start"], slot["end"]


def minutes_from_label(answer: str) -> int | None:
    """选项原文 → 分钟。认不出返回 None（**不猜**）。

    "None" → 0；"15 min" → 15；"60+ min" → 60。
    """
    text = str(answer or "").strip()
    if not text:
        return None
    if text.lower() in ("none", "no", "none.", "0", "0 min"):
        return 0
    m = re.search(r"\d+", text)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def interval(day_iso: str, slot: dict, minutes: int,
             tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    """答案 → 真实区间（本地时间，带时区）。

    以窗口**结束**时刻为锚点往前推；超过窗口长度就裁到窗口起点。
    分钟数 ≤ 0 返回 None（没有活动就不该造一条 0 分钟的事件）。
    """
    if minutes <= 0:
        return None
    d = date.fromisoformat(day_iso)

    def _at(hhmm: str) -> datetime:
        hh, mm = (int(x) for x in hhmm.split(":"))
        return datetime.combine(d, datetime.min.time(), tzinfo=tz) + timedelta(
            hours=hh, minutes=mm)

    win_start, win_end = _at(slot["start"]), _at(slot["end"])
    end = win_end
    start = end - timedelta(minutes=minutes)
    if start < win_start:
        start = win_start
    if end <= start:
        return None
    return start, end


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
