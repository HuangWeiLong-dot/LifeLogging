#!/usr/bin/env python3
"""服务器侧：白天根据**当天**的位置/手机数据，实时问你几句。

**为什么要有它**

    原来只有一套提问：笔记本晚上拿「昨天日记」生成，次日早上问回来。
    它问的是**昨天**的事，而且是「拿你自己写的话堵你」——那很有用，但它
    看不见今天正在发生的事。

    这个脚本补的就是那一块：白天在服务器上，**每个小时问一次**
    「刚才这一小时你在干嘛」——在它还新鲜的时候问，而不是等明天。

    题是**固定节拍**的，不是「够阈值才问」：哪怕过去一小时很平淡，也会照实
    说「电脑上没记录、手机没解锁」，然后问你那段时间在做什么。阈值（停留
    ≥30 分钟等）只用来决定**这一小时里哪件事最值得提**，不是发问的门槛。

**两条线共同存在，互不替代**

    | | 谁生成 | 什么时候 | 问什么 | 存哪 |
    |---|---|---|---|---|
    | 问责批 | 笔记本（要 vault） | 晚上，问次日 | 昨天日记 / 挂着的事 | `questions-<day>.json` |
    | 实时批 | 这里 | 白天，问当场 | 今天的行踪 / 手机 | `questions-live-<day>.json` |

    ⚠️ **必须分开存**。中继的 `save_questions()` 是纯覆写、`load_latest_questions()`
       取「文件名排序最后一份」——实时批写进同一份文件会**静默顶掉**手机早上
       那批题。中继那边为此专门加了一条路由和一份文件，见 `phone_relay.py`。

**为什么不上 LLM**

    这几条规则是确定性的：同一个片段问出来的话每次都一样，可测、零成本、
    也不会编一个不存在的地名出来。开放式的自由问答是另一件事（那个走 LLM，
    并且受每日额度约束）。

**同一小时不重复问**

    题的 key 里带着年份日期和小时（`hour-2026-10-17T14`）；发过就记进
    `live-asked-<day>.json`，同一小时不再生成第二条。timer 每 30 分钟拉一次，
    所以一小时里会跑两次 —— 第二次看到 key 已经存在，什么都不做。

    每次运行都把**当天已生成的全部**实时题重新推一遍（中继那边是覆写），
    所以那份本地记录是权威副本，不是缓存。

用法（服务器上，由 systemd timer 每 30 分钟拉一次）：
  python scripts/live_questions.py
  python scripts/live_questions.py --dry-run     # 只打印，不推、不落盘
  python scripts/live_questions.py --date 2026-10-15
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import Day, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])

# 只在白天到睡前问。凌晨扫出来的「你 03:00 在哪」没有意义，
# 而且会吵醒人（如果推送开着）。
ASK_FROM_HOUR = 8
# ⚠️ 这是**开区间上界**（`hour < ASK_UNTIL_HOUR`）。收尾定在 23:00 之后，
#    这里必须是 24 而不是 23 —— 写 23 的话 23 点那一轮会在窗口检查处直接
#    返回，收尾永远等不到。踩过一次这个坑，别改回去。
ASK_UNTIL_HOUR = 24

# 当天最多问几条。实时题是**补充**，不是主力 —— 一天刷十几条会让人把
# 整条通道静音，那样问责批也一起收不到了。
# 停留多久才值得在那一小时的题里提一句。
# ⚠️ 这**不是**「够久才问」的门槛 —— 每小时必问，这个值只决定
#    「这一小时里哪件事最值得提」。路过、等红灯不算。
MIN_STAY_MIN = 30

# 电脑快照多久没更新，就不再当作「这一小时」的数据。
#
# ⚠️ 这是本文件里最重要的一条闸门。笔记本一天只醒约 3.4 小时，睡着的时候
#    `/desktop` 上那份快照**不会消失**，它只是停在最后一次推送的样子。
#    没有这条闸门时，07:02、08:03、10:03、11:04、12:04 五道题会复读出
#    **一模一样**的「Code.exe 22 分 · msedge 21 分」—— 而且都写在
#    「这一段」的标题下面，看起来像是每小时都在测。那是假的。
SNAP_MAX_AGE_MIN = 45

# 收尾题里的「今天电脑合计」能容忍多久的延迟。日报比小时题宽松 ——
# 一天的总量在两三个小时里不会变太多，但**跨天**绝对不行。
DAILY_SNAP_MAX_AGE_MIN = 180


def _snap_day(snap: dict | None) -> str:
    return str((snap or {}).get("day") or "")


def _snap_age_min(snap: dict | None, now: datetime) -> float | None:
    """快照是多久以前推的（分钟）。解析不出来返回 None —— **不猜**。"""
    raw = (snap or {}).get("pushed_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return (now - dt).total_seconds() / 60.0


def snapshot_state(snap: dict | None, day_iso: str, now: datetime,
                   max_age_min: float) -> tuple[bool, str]:
    """这份笔记本快照能不能代表 `day_iso` 的「现在」？

    返回 (能不能用, 一句**给人看的**原因)。不能用时那句话会直接进题面，
    所以要短 —— 「电脑：数据停在 10-22」比「没有记录到活动」诚实得多，
    前者是「不知道」，后者会被读成「你没用电脑」。
    """
    if not isinstance(snap, dict) or not snap:
        return False, "笔记本还没推过快照"
    day = _snap_day(snap)
    if day and day != day_iso:
        return False, f"数据停在 {day}"
    age = _snap_age_min(snap, now)
    if age is None:
        # 老版本推的快照没有 pushed_at。宁可照用，也不要因为缺字段就整天哑掉。
        return True, ""
    if age > max_age_min:
        return False, f"最后上报是 {_hm(age)}前"
    return True, ""

# 晚上几点开始「一天的收尾」。到点之后不再发小时的题，改发三条总结题；
# 答完之后机器人会用 LLM 写一段当天总结（见 feishu_bot）。
# 留空 = 不做收尾，只发小时题。
SUMMARY_AT = os.environ.get("LIVE_SUMMARY_AT", "23:00")

# 白天的**任务检查**时段。到这些小时，如果任务表里还有没问过的任务，
# 就用一条「到现在做到了吗？」替换掉普通的小时题（不是额外加一条）。
# 一天最多 4 条，避免刷屏；空字符串 = 关闭。
COMMITMENT_HOURS: tuple[int, ...] = tuple(
    int(x) for x in os.environ.get("LIVE_COMMITMENT_HOURS", "10,14,18,21").split(",")
    if x.strip().isdigit() and 0 <= int(x) <= 23
)


def _summary_due(now: datetime) -> bool:
    if not SUMMARY_AT:
        return False
    try:
        hh, _, mm = SUMMARY_AT.partition(":")
        due = now.replace(hour=int(hh), minute=int(mm or 0),
                          second=0, microsecond=0)
    except (TypeError, ValueError):
        return False
    return now >= due


def _summary_soon(now: datetime) -> bool:
    """离收尾不到**半小时**了吗？到了就别再发小时题。

    ⚠️ 原来写的是「不到一小时」，那是在收尾定在 21:30 的时候。收尾挪到 23:00
       之后，一小时就变成了 **22:00 起停发** —— 而使用者要的是「每小时一条」，
       于是 22 点那个小时凭空没了。改成半小时：22:04 照常发，22:34 起才停。
    """
    if not SUMMARY_AT:
        return False
    try:
        hh, _, mm = SUMMARY_AT.partition(":")
        due = now.replace(hour=int(hh), minute=int(mm or 0),
                          second=0, microsecond=0)
    except (TypeError, ValueError):
        return False
    return now >= due - timedelta(minutes=30)


def _unfilled(v: str) -> bool:
    """空、占位符、或混了非 ASCII —— 都算「还没填」。

    ⚠️ 非 ASCII 那条是重点。占位符被连着中文一起粘进去时
       （`REPLACE_ME` 写成 `你的key`），值是**有**的、非空、也不含 REPLACE，
       看着完全正常 —— 然后它会在 httpx 拼请求头时炸成一个编码错误，
       跟「你忘了填」这个事实看不出任何关系。仓库里为同一件事吃过一次亏，
       见 common.require_api_key() 的注释。

    ⚠️ 也别只查一个前缀：老脚本查的是 `REPLACE_WITH`，而配置模板里写的是
       `REPLACE_ME` —— 于是「忘了填」会以一个 401 的形式出现。这里查子串。
    """
    if not v or not v.strip():
        return True
    if "REPLACE" in v.upper():
        return True
    return any(ord(c) > 127 for c in v)


def relay() -> tuple[str, str] | None:
    cfg = (CFG.get("sources") or {}).get("phone_relay") or {}
    if not cfg.get("enabled"):
        print("-> phone relay disabled (config.yaml -> sources.phone_relay.enabled)")
        return None
    base = (cfg.get("api_base") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    if _unfilled(base) or _unfilled(key):
        print("  ⚠️  phone_relay 开着，但 config.yaml 里还是占位符（或混了非 ASCII）")
        return None
    return base, key


def asked_path(day: str) -> Path:
    return Path(CFG["state_dir"]) / f"live-asked-{day}.json"


def load_asked(day: str) -> list[dict]:
    p = asked_path(day)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        items = raw.get("questions") if isinstance(raw, dict) else raw
        return [q for q in (items or []) if isinstance(q, dict) and q.get("id")]
    except (OSError, json.JSONDecodeError):
        return []


def save_asked(day: str, questions: list[dict]) -> None:
    p = asked_path(day)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"day": day, "questions": questions},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def closed_path(day: str) -> Path:
    """「这一天已经收尾了」的标记文件。

    ⚠️ 为什么需要它。收尾（/收尾 或 23:00）之后，timer 仍然每 30 分钟跑一次，
       于是复盘刚发完，一小时后又冒出一条「07:02–08:02 这一段：主要在忙什么？」。
       那不是「还有题没答」，是**已经关掉的这一天又被重新开口了**。

       「今天结束了」只有使用者自己知道，所以这个标记只能由他发起的那次
       收尾来写，不能拿时钟推。
    """
    return Path(CFG["state_dir"]) / f"live-closed-{day}.json"


def is_closed(day: str) -> bool:
    return closed_path(day).exists()


def mark_closed(day: str, reason: str = "") -> None:
    p = closed_path(day)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"day": day, "reason": reason,
             "closed_at": datetime.now(TZ).isoformat(timespec="seconds")},
            ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"  ⚠️  封盘标记写不进去：{e}")


def qid(day: str, text: str) -> str:
    """`<day>-<sha1(text)[:8]>` —— 和 generate_questions 同一套。

    ⚠️ 日期前缀是**有意义的**：apply_answers.py 靠它筛出属于这一天的问题
       （`value_text.startswith(f"{day}-")`）。哈希那段只要唯一即可。
    """
    return f"{day}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"


def make(day: str, text: str, evidence: str, key: str,
         kind: str = "routine", fmt: str = "text",
         options: list[dict] | None = None) -> dict:
    """生成一条实时题。

    ⚠️ `ref` 必须是空的。apply_answers 只处理带 ref 的题（见它
       `read_questions()` 里的判断），所以这里的题永远不会去勾任务 ——
       包括 format="choice" 的打分题：它只是问一句，不是任务。

    ⚠️ 打分题用 choice **不是**为了好看：答案要能被下游算成数字
       （`collect_labels.py` 读的就是选项的 leading digit）。自由文本
       再有人情味，也进不了任何统计。
    """
    item = {"id": qid(day, text), "kind": kind, "format": fmt,
            "text": text, "evidence": evidence, "ref": "",
            "_key": key}
    if options:
        item["options"] = list(options)
    return item


# ── 取数 ────────────────────────────────────────────────────────────
def relay_phone_events(day_iso: str, base: str, key: str) -> list[dict]:
    r = httpx.get(f"{base}/events", params={"day": day_iso},
                  headers={"X-API-Key": key}, timeout=20.0)
    r.raise_for_status()
    return [e for e in (r.json().get("events") or []) if isinstance(e, dict)]


def today_stays(day: Day) -> list[dict]:
    """今天的位置停留。用的就是日记那套（Dawarich → 本地聚类 → 离线反查）。

    拿不到就返回空 —— **不猜**。位置服务断了是这个脚本最正常的失败，
    不该让它把整次运行弄崩（那就连手机那两条规则也一起不跑了）。
    """
    try:
        from collect_phone import derive_stays, fetch_points
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  位置模块加载失败，跳过位置规则：{type(e).__name__}: {e}")
        return []
    try:
        pts = fetch_points(day)
        if not pts:
            return []
        out = []
        for ev in derive_stays(day, pts):
            meta = ev.meta or {}
            out.append({
                "place": str(ev.value_text or ""),
                "minutes": float(ev.value_num or 0),
                "start": ev.ts_utc,
                "end": meta.get("end_utc"),
                "is_coord": str(ev.value_text or "").startswith("("),
            })
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  位置数据拿不到，跳过位置规则：{type(e).__name__}: {e}")
        return []


def _hm(minutes: float) -> str:
    total = int(round(minutes))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} 小时 {m} 分"
    return f"{h} 小时" if h else f"{m} 分"


def _stay_span(stay: dict) -> str:
    """一次停留的时间跨度：``11:04 起，已停留 3 小时 46 分``。

    和「停留 3 小时 46 分」的区别很关键：后者会被读成「这一小时里待了多久」。
    """
    start = _parse_iso(stay.get("start"))
    end = _parse_iso(stay.get("end"))
    mins = float(stay.get("minutes") or 0)
    a = start.strftime("%H:%M") if start else ""
    b = end.strftime("%H:%M") if end else ""
    if a and b and end > start:
        return f"{a}–{b}，共 {_hm(mins)}"
    if a:
        return f"{a} 起，已停留 {_hm(mins)}"
    return f"已停留 {_hm(mins)}"


# ── 规则 ────────────────────────────────────────────────────────────
def _parse_iso(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v)).astimezone(TZ)
    except (TypeError, ValueError):
        return None


def rule_hourly(day_iso: str, stays: list[dict], events: list[dict],
                snap: dict | None, now: datetime) -> dict | None:
    """**每小时一题**：从过去一小时的数据里挑最值得问的那件事。

    ⚠️ 这是**固定节拍**，不是「够阈值才问」。

        最早的版本是三条阈值规则（长停留 ≥90 分 / 手机静默 ≥90 分 / 单应用
        ≥150 分）。它们一天可能一次都不触发，于是「实时提问」实际上很少发生。
        使用者要的是「每一个小时至少问一次，根据当前的数据」—— 所以改成：
        **每小时一定有题**，阈值降级成「选材依据」（这一小时里哪件事最值得提）。

        合并成一个规则还有个好处：一天不会冒出几条互相重叠的题
        （比如同时问「你上午在哪」和「你上午手机怎么没动」）。

    选材优先级：有位置 → 电脑有活动 → 手机有活动 → 都没有就照实说很平淡。
    """
    start = now - timedelta(hours=1)
    span = f"{start.strftime('%H:%M')}–{now.strftime('%H:%M')}"

    unlocks: list[datetime] = []
    switches: list[str] = []
    for e in events:
        dt = _parse_iso(e.get("ts_utc"))
        if dt is None or dt < start:
            continue
        if e.get("metric") == "unlock":
            unlocks.append(dt)
        elif e.get("metric") == "app_switch":
            sw = str(e.get("value_text") or "").strip()
            if sw:
                switches.append(sw)

    # 电脑：笔记本每 10 分钟推一次的快照里带着「过去一小时」。
    #
    # ⚠️ 先过新鲜度闸门。笔记本睡着时这份快照**还在**，只是停住了 ——
    #    不过闸门的话每一小时的题都会复读同一份数据（见 SNAP_MAX_AGE_MIN）。
    snap_ok, snap_why = snapshot_state(snap, day_iso, now, SNAP_MAX_AGE_MIN)
    lh = ((snap or {}).get("last_hour") or {}) if snap_ok else {}
    d_apps = [(str(a), float(m)) for a, m in (lh.get("top_apps") or [])]
    d_total = float(lh.get("total_minutes") or 0.0)

    # 位置：这一小时里还在持续的停留（≥30 分钟才算「在那儿」，
    # 路过不算 —— 否则每条题都会带上「你在某条路上」）。
    stay = None
    for s in stays:
        if float(s.get("minutes") or 0) < MIN_STAY_MIN:
            continue
        end = _parse_iso(s.get("end"))
        if end is not None and end < start:
            continue          # 一小时前就结束了
        if stay is None or s["minutes"] > stay["minutes"]:
            stay = s

    bits: list[str] = []
    if stay:
        where = str(stay.get("place") or "")
        tag = "坐标 " if stay.get("is_coord") else ""
        # ⚠️ 写**这段停留的时间跨度**，不写累计分钟数。
        #    原来写「停留 3 小时 46 分」，然后把它放在「13:04–14:04 这一段」
        #    的标题下面 —— 读者当然会以为「这一段待了 3 小时 46 分」，
        #    而那个数是**从进门到现在**的累计值。同一份数据两小时前是
        #    「46 分」，一小时前是「2 小时 46 分」，全是同一个停留。
        bits.append(f"位置：{tag}{where}（{_stay_span(stay)}）")
    if d_apps:
        bits.append("电脑：" + " · ".join(f"{a} {_hm(m)}" for a, m in d_apps[:3]))
    elif not snap_ok:
        # ⚠️ 不能说「没有记录到活动」—— 那是在拿「没有数据」冒充「没有干活」，
        #    而且这句假话会一路进到日记里。
        bits.append(f"电脑：{snap_why}")
    elif d_total <= 0:
        # 快照是新的、这一小时确实是空的 —— 这一句是**真**的。
        bits.append("电脑：这一小时没有活动")

    if unlocks:
        line = f"手机：解锁 {len(unlocks)} 次"
        # ⚠️ 桌面 / 系统界面不是「在用的应用」。不滤掉的话「切换最多」永远是
        #    启动器（实测第一次跑就报了个 `com.miui.home`）——那这条信息就废了。
        #    名单直接复用 normalize 里现成的那张，不另立一份。
        try:
            from normalize import PHONE_NOT_AN_APP, phone_app_name
        except Exception:  # noqa: BLE001
            PHONE_NOT_AN_APP = set()
            phone_app_name = lambda x: x      # noqa: E731
        real = [s for s in switches if s not in PHONE_NOT_AN_APP]
        if real:
            top = Counter(real).most_common(1)[0][0]
            line += f"，{phone_app_name(top)} 切换最多"
        bits.append(line)
    elif events:
        # 今天**有**事件，只是这一小时没有 —— 那才是「真的没解锁」。
        bits.append("手机：这一小时没解锁")
    else:
        # ⚠️ 一整天一条事件都没有，更可能是 Tasker / 中继断了，而不是你没碰手机。
        #    这两种情况在数据上长得一模一样，所以话必须说成「不知道」。
        bits.append("手机：今天还没有任何上报")

    if not bits:
        bits.append("这一小时没有任何记录")

    # ⚠️ 正文不写「今天」（这题可能隔天才被读到，见 feishu_bot.render_question）。
    #    这里写的是**具体的小时区间**，本身就没有歧义。
    text = (f"{span} 这一段：\n"
            + "\n".join(f"· {b}" for b in bits)
            + "\n\n主要在忙什么？")
    evidence = ("位置来自 Dawarich；电脑来自笔记本每 10 分钟推的快照；"
                f"手机来自中继。（{day_iso}）")
    return make(day_iso, text, evidence,
                f"hour-{day_iso}T{now.strftime('%H')}")


# ── 任务检查（穿插在小时题里）──────────────────────────────────────

_TIME_RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,2})[:：](\d{2})\s*(?:-|–|—|~|到|至)\s*"
    r"(\d{1,2})[:：](\d{2})(?!\d)"
)


def _time_window(task: str) -> tuple[int, int] | None:
    """从任务文本里抽 `HH:MM-HH:MM`。抽不到返回 None。"""
    m = _TIME_RANGE_RE.search(task)
    if not m:
        return None
    try:
        start, end = int(m.group(1)), int(m.group(3))
    except ValueError:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return None
    return start, end


def load_commitment_tasks(day_iso: str) -> list[str]:
    """读当天的 `journal-input-<day>.json`，取 `carryover` + `tasks`。

    ⚠️ 笔记本还没推当天输入时，往前找最多 2 天。宁可拿一份稍旧的任务表，
       也不要因为「今天还没推送」就一条任务检查都不发。
    """
    try:
        d = date.fromisoformat(day_iso)
    except ValueError:
        return []

    state = Path(CFG["state_dir"])
    payload = None
    for i in range(0, 3):
        p = state / f"journal-input-{(d - timedelta(days=i)).isoformat()}.json"
        if not p.exists():
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            break
        except (OSError, json.JSONDecodeError):
            continue
    if not isinstance(payload, dict):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for key in ("carryover", "tasks"):
        for t in (payload.get(key) or []):
            t = " ".join(str(t).split())
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _commitment_keys(day_iso: str, known: list[dict]) -> list[str]:
    """今天已经发过的问责题 key。"""
    prefix = f"task-{day_iso}T"
    return [str(q.get("_key") or "") for q in known
            if str(q.get("_key") or "").startswith(prefix)]


def commitment_question(day_iso: str, tasks: list[str],
                        now: datetime, known: list[dict]) -> dict | None:
    """到了检查时段，挑一条没问过的任务生成问责题。

    ⚠️ 它**替换**普通小时题，不是追加 —— 一天 4 条已经够多了，再加会让人
       把整条通道静音，连带收尾题一起收不到。

    ⚠️ **一个检查时段只发一条。** 原来 `_key` 里带着任务 hash，于是
       `COMMITMENT_HOURS` 里的 10 点这一轮、每 30 分钟拉一次 timer 就跑一次，
       每次都因为「这一条任务没问过」而再发一条 —— 10:03 发一条、10:33 又发
       一条，和上面那句「一天最多 4 条」直接矛盾。key 去掉 task hash 之后，
       同一个小时天然只有一条。
    """
    if now.hour not in COMMITMENT_HOURS or not tasks:
        return None

    fired = _commitment_keys(day_iso, known)
    # 这个时段已经问过一条了
    if any(k.startswith(f"task-{day_iso}T{now:%H}") for k in fired):
        return None
    # 当天上限（COMMITMENT_HOURS 有 4 个时段，正常情况下到不了这里，
    # 但如果环境变量被改成了更密的时段，这行是最后一道闸）
    if len(fired) >= max(1, len(COMMITMENT_HOURS)):
        return None

    asked_refs = {str(q.get("ref") or "") for q in known if q.get("ref")}
    candidates = [t for t in tasks if t not in asked_refs]
    if not candidates:
        return None

    def score(t: str) -> tuple[int, int]:
        idx = tasks.index(t)
        w = _time_window(t)
        if w is None:
            return (2, idx)
        start, end = w
        if start <= now.hour < end:
            return (0, idx)                  # 时段正在进行
        if end == now.hour:
            return (0, idx)                  # 时段刚结束
        if end == (now.hour - 1) % 24:
            return (1, idx)                  # 上一小时结束
        return (3, idx)

    task = min(candidates, key=score)
    display = task if len(task) <= 90 else task[:87] + "…"
    text = f"现在 {now:%H:%M}。任务表里还挂着「{display}」——到现在做到了吗？"
    return {
        "id": qid(day_iso, text),
        "kind": "commitment",
        "format": "choice",
        "text": text,
        "evidence": "来自任务表 Open 项 / 昨天日记里未完成的任务。",
        "ref": task,
        "options": [
            {"label": "做了", "done": True},
            {"label": "做了一部分", "done": False},
            {"label": "没做", "done": False},
        ],
        # ⚠️ key 里**不带**任务 hash：见上面「一个检查时段只发一条」。
        "_key": f"task-{day_iso}T{now:%H}",
    }


def relay_desktop(base: str, key: str) -> dict | None:
    """笔记本每 10 分钟推上来的快照。

    拿不到**不是错误** —— 笔记本一天只醒几小时，睡着时就是没有新快照。
    位置和手机数据仍然能用，所以不该因为电脑这块缺席就整题不发。
    """
    try:
        r = httpx.get(f"{base}/desktop", headers={"X-API-Key": key}, timeout=20.0)
        r.raise_for_status()
        snap = r.json().get("snapshot")
        return snap if isinstance(snap, dict) else None
    except Exception:  # noqa: BLE001
        return None


# 收尾里的「常规题」：**数据看不见的七个维度，每天全问**。
#
# 每个维度准备几套**意思相同、措辞不同**的问法，按天轮换：
#
#   · 每天七条都在 —— 这些维度是「精准」的来源，漏一天就断一天。
#   · 但每天的**问法**不同 —— 同一句话连问一周，回答会退化成「还行」
#     「正常」，那就等于没问。
#   · 各套问法问的是同一件事的不同侧面，所以跨天**仍然可比**
#     （比如「体力」永远是状态 + 时间点，只是从不同角度切入）。
#
# 元组结构：(维度名, 几套问法, 依据)。依据说明「为什么这件事数据看不到」。
ROUTINE_DIMENSIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("体力", (
        "今天体力上是什么状态？什么时候开始明显下降的？",
        "今天精力最好的是哪一段，最差的是哪一段？",
        "今天大概几点开始觉得撑不住了？之前的状态怎么样？",
        "今天身体有没有哪里不舒服，或者反而比平时好？",
    ), "身体的感觉数据看不到 —— 步数和心率只能说明你动过。"),
    ("心情", (
        "今天心情怎么样？有没有哪一段明显好起来或者沉下去？",
        "今天什么时候情绪最好，什么时候最糟？",
        "今天有没有一件事让你明显高兴，或者明显堵得慌？",
        "今天整体情绪更像哪一种：平静、起伏，还是绷着？",
    ), "情绪数据看不到。"),
    ("人际", (
        "今天和谁待在一起的时间比较长？聊了什么？",
        "今天有没有一次聊天让你印象比较深？和谁？",
        "今天有没有想联系谁但没联系？",
        "今天独处的时间和跟人相处的时间，哪个更多？",
    ), "人际数据看不到 —— 位置只说明你们在同一个地方。"),
    ("睡眠", (
        "昨晚睡得怎么样？今天有没有因此受影响？",
        "昨晚大概几点睡着、几点醒的？中间醒过吗？",
        "今天的困意主要在什么时候？靠什么撑过去的？",
        "昨晚的睡眠和前一晚比，好一些还是差一些？",
    ), "睡眠质量只有你知道，手环只能测时长。"),
    ("吃饭", (
        "今天的饭是按点吃的吗？有没有哪一餐跳过了？",
        "今天吃了什么？有没有一顿是认真吃的？",
        "今天有没有饿过头或者吃撑的时候？",
        "今天的饮食和平时比，正常吗？",
    ), "吃饭不产生事件。"),
    ("休息", (
        "今天有没有一段是真正休息的（不是刷手机）？",
        "今天有没有离开屏幕超过半小时？在做什么？",
        "今天有没有觉得「缓过来了」的时刻？",
        "今天忙完之后是怎么收的尾？",
    ), "「休息」和「娱乐」在数据上分不开。"),
    ("学习", (
        "今天学到了什么以前不知道的？",
        "今天有没有哪件事让你改变了对某个东西的看法？",
        "今天看的东西里，哪一条最值得留下来？",
        "今天有没有哪个问题现在比早上更清楚了？",
    ), "学到的东西不会出现在窗口标题里。"),
)


# 哪几个维度除了自由文本，**每天还给一个 1–5 的分数**。
#
# ⚠️ 这是给「以后要学的模型」攒燃料，不是给今天看的。情绪/体力是预测里
#    最可能当目标变量的一维，而它现在只存在于自由文本里 —— 自由文本再
#    有人情味也进不了任何统计。分数和备注**都要**：分数量化，备注保留
#    「为什么」，两者缺一都会把这一天压平。
#
# ⚠️ 只给两个维度打分。七个全改成选择会把这个晚上变成填表，回答质量
#    反而会掉 —— 那正是 `ROUTINE_DIMENSIONS` 注释里说的「退化成还行」。
#
# ⚠️ 打分题**不轮换问法**，用固定题干。轮换是为了防止自由文本退化成
#    「还行」；而 1–5 的量表每天一样，才是可比的。题干里留一句邀请备注，
#    把原来那种「什么时候最好/最差」的细节留在备注里。
RATED_DIMENSIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "心情": (
        "今天整体心情打几分？想补一句的话：今天什么时候情绪最好、什么时候最糟？",
        ("1 很差", "2 偏低", "3 一般", "4 不错", "5 很好"),
    ),
    "体力": (
        "今天整体体力打几分？想补一句的话：今天什么时候精力最好、什么时候最差？",
        ("1 没电", "2 偏低", "3 一般", "4 不错", "5 充沛"),
    ),
    "睡眠": (
        "昨晚整体睡得怎么样？想补一句的话：几点睡、几点醒、中间醒过吗？",
        ("1 很差", "2 偏低", "3 一般", "4 不错", "5 很好"),
    ),
}


def _rating_options(dim: str) -> list[dict]:
    """把维度名变成 choice 题要的 options 形状。

    ⚠️ **不写 `done`**。写了也不会被用，但会让后来的人以为这条答案在
       判定某个任务完成与否 —— 它不是，routine 题的 ref 永远是空的。
    """
    return [{"label": label} for label in RATED_DIMENSIONS[dim][1]]


def summary_questions(day_iso: str, stays: list[dict],
                      events: list[dict], snap: dict | None,
                      now: datetime | None = None) -> list[dict]:
    """晚上收尾的几道题。答完之后机器人会用 LLM 写一段当天总结。

    ⚠️ 题目是**按当天真实数据拼出来的**，不是每天问同样三句。

       原来只有三条固定模板，其中两条（「最值得记住的一件事」「没做成什么」）
       完全是空问 —— 换成任何一天都成立，也就等于没有信息。现在每条都带上
       当天的具体数字或地点，让你能对着答。

    题目的顺序有讲究：
      1. 最值得记住的一件事 —— 数据里永远没有，只能问你
      2. 电脑分布 —— 对照体感：统计说的和你感觉的一样吗
      3. 手机节奏 —— 解锁/切换次数，问「哪一段是真投入」
      4. 位置 —— 去过几个地方、最久的是哪，问计划外的那次
      5. 常规题 ×7 —— **数据看不见的七个维度**（体力/心情/人际/睡眠/吃饭/
         休息/学习），**每天全问**，但每天的措辞不同，见 ROUTINE_DIMENSIONS。
         其中心情/体力是 **1–5 打分 + 可选备注**（见 RATED_DIMENSIONS），
         其余五个仍是自由文本
      6. 没做成、想留到明天 —— 明天的问责线索

    ⚠️ **某一块没有数据就不问那一题**（比如位置数据缺失的那天）。
       硬问一句「今天去了哪」只会让你没法答，还会污染总结。
       所以题数在 2–5 之间浮动，不是固定三条。
    """
    now = now or datetime.now(TZ)
    # ⚠️ 电脑快照必须**是今天的、而且是新的**，否则那句「今天电脑合计 3.52 小时」
    #    会是昨天的数。实测就出过这个：复盘里写「今天 3.52 小时」，而已知的
    #    快照停在 10-22 —— 同一份复盘里模型自己都说了「快照还停在 10-22」。
    snap_ok, snap_why = snapshot_state(snap, day_iso, now, DAILY_SNAP_MAX_AGE_MIN)
    if not snap_ok:
        print(f"  . 收尾不用电脑数据：{snap_why}")
    today = ((snap or {}).get("today") or {}) if snap_ok else {}
    total = float(today.get("total_hours") or 0)
    cats = today.get("by_category") or []
    d_apps = [(str(a), float(h)) for a, h in (today.get("top_apps") or [])]

    unlocks = sum(1 for e in events if e.get("metric") == "unlock")
    switches = sum(1 for e in events if e.get("metric") == "app_switch")
    places = [s for s in stays if float(s.get("minutes") or 0) >= 20]

    qs: list[dict] = []

    def add(text: str, evidence: str, routine: bool = False,
            dim: str = "") -> None:
        options = _rating_options(dim) if dim in RATED_DIMENSIONS else None
        q = make(day_iso, text, evidence,
                 f"summary-{day_iso}-{len(qs) + 1}", kind="summary",
                 fmt="choice" if options else "text", options=options)
        if routine:
            # kind 仍是 summary（收尾流程靠它判断「答完了没有」），
            # 只是打个标记，让机器人把它显示成「常规」。
            q["routine"] = True
            # 维度名留着做审计：**每个维度每天都该有一条**，
            # 少了哪一维要能一眼看出来，而不是靠人肉比对七句话。
            q["dim"] = dim
        qs.append(q)

    # 1. 只能问你的那一件
    add("今天最值得记住的一件事是什么？",
        "数据里永远没有这个 —— 只有你知道。一句话就行，会进日记。")

    # 2. 电脑分布：和他自己的体感对一下
    if total:
        dist = " / ".join(f"{k} {v}h" for k, v in cats[:4]) or "无分类"
        top = f"；最久的是 {d_apps[0][0]}（{d_apps[0][1]}h）" if d_apps else ""
        add(f"今天电脑合计 {total} 小时（{dist}）{top}。"
            "这个分布和你的体感一致吗？",
            "来自笔记本每 10 分钟推的快照：today.total_hours / by_category / top_apps。")

    # 3. 手机节奏：次数是实测的
    if unlocks or switches:
        add(f"今天手机解锁 {unlocks} 次、切换应用 {switches} 次。"
            "哪一段时间是真正投入的？",
            "来自中继上的手机事件（Tasker 上报），次数是实测的，不是估的。")

    # 4. 位置：只有 ≥20 分钟的停留才算
    if places:
        best = max(places, key=lambda s: float(s.get("minutes") or 0))
        tag = "坐标 " if best.get("is_coord") else ""
        add(f"今天去过 {len(places)} 个地方，最久的是{tag}{best.get('place')}"
            f"（{_hm(float(best.get('minutes') or 0))}）。"
            "这些停留里有哪一次是计划外的？",
            "位置来自 Dawarich 的停留推导；只有 ≥20 分钟的停留才算。")

    # 常规题：七个维度**每天全问**，但每天的措辞不同。
    try:
        ord_ = date.fromisoformat(day_iso).toordinal()
    except ValueError:
        ord_ = 0
    for i, (dim, variants, evidence) in enumerate(ROUTINE_DIMENSIONS):
        rated = RATED_DIMENSIONS.get(dim)
        if rated:
            text = rated[0]
        elif variants:
            # 每个维度用不同的偏移 —— 这样七条题的组合逐日变化，
            # 而不是整组一起平移（那样三天的组合会一模一样）。
            text = variants[(ord_ + i * 3) % len(variants)]
        else:
            continue
        add(text, evidence, routine=True, dim=dim)

    # 最后一道：留给明天的问责线索
    add("今天有什么没做成、想留到明天的？",
        "这条会成为明天问责的线索（也会进周回顾的素材）。")

    return qs


def build(day: Day, base: str, key: str) -> list[dict]:
    events = relay_phone_events(day.iso, base, key)
    stays = today_stays(day)
    snap = relay_desktop(base, key)
    now = datetime.now(TZ)

    # 已经发过的（按 key）—— 同一小时只问一次，收尾一天一次
    known = load_asked(day.iso)
    fired = {str(q.get("_key") or "") for q in known}

    # ── 已经收尾的一天不再开口 ──
    # 见 closed_path()：收尾之后再出「这一段你在干嘛」是纯噪音。
    if is_closed(day.iso):
        return known

    # ── 晚上收尾 ──
    if _summary_due(now):
        prefix = f"summary-{day.iso}"
        if any(k.startswith(prefix) for k in fired):
            return known                  # 今天已经收过尾了
        try:
            # ⚠️ 这里**不封盘**。封盘要等 push 成功（见 main()）—— 先封的话，
            #    一次推送失败会让这一天再也生不出收尾，而服务器上看不出异常。
            return known + summary_questions(day.iso, stays, events, snap, now=now)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  收尾题生成失败：{type(e).__name__}: {e}")
            return known

    # 离收尾不到一小时就别再发小时题 —— 否则晚上连着两条。
    if _summary_soon(now):
        return known

    # 这一小时刚刚发过问责题？那就不再发小时题。问责题是**替换**，不是加塞。
    if any(k.startswith(f"task-{day.iso}T{now:%H}") for k in fired):
        return known

    # ── 固定时段的任务检查（替换这一小时的普通题）──
    if now.hour in COMMITMENT_HOURS:
        try:
            cq = commitment_question(day.iso, load_commitment_tasks(day.iso),
                                     now, known)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  任务检查生成失败：{type(e).__name__}: {e}")
            cq = None
        if cq and cq["_key"] not in fired:
            return known + [cq]

    # ── 每小时一题 ──
    try:
        q = rule_hourly(day.iso, stays, events, snap, now)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  生成失败：{type(e).__name__}: {e}")
        q = None

    if q and q["_key"] not in fired:
        return known + [q]
    return known


def push(day_iso: str, base: str, key: str,
         questions: list[dict]) -> tuple[bool, str]:
    """把当天全部实时题推到中继，**成功之后**才落本地。返回 (成功?, 说明)。

    ⚠️ 顺序不能反：先落本地的话，一次推送失败会让这条规则当天再也不发
       （本地记着「已发」），而服务器上看不出任何异常 —— 这个项目一路在打的
       正是这类静默失效。
    """
    payload = {"day": day_iso,
               "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
               "questions": [{k: v for k, v in q.items() if k != "_key"}
                             for q in questions]}
    try:
        r = httpx.post(f"{base}/questions/live", json=payload,
                       headers={"X-API-Key": key}, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        return False, f"推不到中继 {base}：{e}"
    if r.status_code == 404:
        # 404 几乎只有这一个原因：服务器上跑的还是旧版中继。
        return False, "中继没有 /questions/live —— 服务器上跑的还是旧版代码"
    if r.status_code != 200:
        return False, f"中继返回 {r.status_code}：{r.text[:200]}"
    save_asked(day_iso, questions)
    return True, str(r.json().get("count"))


def fetch_pending(base: str, key: str) -> list[dict]:
    r = httpx.get(f"{base}/questions/pending",
                  headers={"X-API-Key": key}, timeout=20.0)
    r.raise_for_status()
    return [q for q in (r.json().get("questions") or []) if isinstance(q, dict)]


def force_summary(day_iso: str, base: str, key: str) -> list[dict]:
    """**立刻**生成当天的收尾三题（不等 SUMMARY_AT），返回还没答的那些。

    ⚠️ 为什么需要它：「一天什么时候结束」只有你自己知道。原来收尾由**时钟**
       决定（默认 23:00），而你在飞书发一句命令，就应该是现在。

    ⚠️ 已经生成过就不再生成，直接返回**还挂着**的那几道 —— 否则重复按会
       生成第二套，题号不同、内容几乎一样。
    """
    day = Day.parse(day_iso, TZ)
    known = load_asked(day_iso)
    prefix = f"summary-{day_iso}"

    if not any(str(q.get("_key") or "").startswith(prefix) for q in known):
        events = relay_phone_events(day_iso, base, key)
        stays = today_stays(day)
        snap = relay_desktop(base, key)
        qs = summary_questions(day_iso, stays, events, snap,
                               now=datetime.now(TZ))
        ok, detail = push(day_iso, base, key, known + qs)
        if not ok:
            raise RuntimeError(detail)
        # ⚠️ 收尾题**推成功之后**才封盘。先封的话，推送失败会让这一天再也
        #    生不出收尾 —— 而使用者以为自己已经收工了。
        mark_closed(day_iso, "manual-summary")

    return [q for q in fetch_pending(base, key)
            if str(q.get("kind") or "") == "summary"]


def main() -> int:
    ap = argparse.ArgumentParser(description="服务器侧：根据当天数据生成实时提问")
    ap.add_argument("--date", default="today")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    day = Day.parse(args.date, TZ)
    header(f"Live questions · {day.iso}")

    now = datetime.now(TZ)
    if args.date == "today" and not (ASK_FROM_HOUR <= now.hour < ASK_UNTIL_HOUR):
        # 静默退出，**不算失败** —— timer 每 30 分钟拉一次，夜里那次本来就该没事做。
        print(f"  . {now.strftime('%H:%M')} 不在 {ASK_FROM_HOUR}:00–{ASK_UNTIL_HOUR}:00 之间，不问")
        return 0

    endpoint = relay()
    if not endpoint:
        return 1
    base, key = endpoint

    try:
        questions = build(day, base, key)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  生成失败：{type(e).__name__}: {e}")
        return 1

    existing = load_asked(day.iso)
    new = [q for q in questions if q not in existing]
    if not questions:
        print("  . 今天还没有值得问的片段")
        return 0

    for q in questions:
        mark = "新" if q in new else "已有"
        print(f"  [{mark}] {q['text']}")
        print(f"        {q['evidence']}")

    if args.dry_run:
        print("\n--dry-run，不推、不落盘。")
        return 0

    if not new:
        print("  . 没有新题，不重复推")
        return 0

    ok, detail = push(day.iso, base, key, questions)
    if not ok:
        print(f"  ⚠️  {detail}")
        return 1
    print(f"  OK  {detail} 条已推（本次新增 {len(new)}）")

    # ⚠️ **推成功之后**才封盘。收尾题一旦发出，这一天的小时题就不该再出现 ——
    #    但先封后推的话，一次推送失败会让这一天再也生不出收尾，
    #    而使用者以为自己已经收工了。
    if any(str(q.get("kind") or "") == "summary" for q in new):
        mark_closed(day.iso, "auto-summary")
        print(f"  ·  {day.iso} 已封盘，今天不再出小时题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
