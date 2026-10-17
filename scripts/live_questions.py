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
import sys
from collections import Counter
from datetime import datetime, timedelta
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
ASK_UNTIL_HOUR = 22

# 当天最多问几条。实时题是**补充**，不是主力 —— 一天刷十几条会让人把
# 整条通道静音，那样问责批也一起收不到了。
# 停留多久才值得在那一小时的题里提一句。
# ⚠️ 这**不是**「够久才问」的门槛 —— 每小时必问，这个值只决定
#    「这一小时里哪件事最值得提」。路过、等红灯不算。
MIN_STAY_MIN = 30

# 晚上几点开始「一天的收尾」。到点之后不再发小时的题，改发三条总结题；
# 答完之后机器人会用 LLM 写一段当天总结（见 feishu_bot）。
# 留空 = 不做收尾，只发小时题。
SUMMARY_AT = os.environ.get("LIVE_SUMMARY_AT", "21:30")


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
    """离收尾不到一小时了吗？到了就别再发小时题 —— 否则晚上连着两条。"""
    if not SUMMARY_AT:
        return False
    try:
        hh, _, mm = SUMMARY_AT.partition(":")
        due = now.replace(hour=int(hh), minute=int(mm or 0),
                          second=0, microsecond=0)
    except (TypeError, ValueError):
        return False
    return now >= due - timedelta(hours=1)


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


def qid(day: str, text: str) -> str:
    """`<day>-<sha1(text)[:8]>` —— 和 generate_questions 同一套。

    ⚠️ 日期前缀是**有意义的**：apply_answers.py 靠它筛出属于这一天的问题
       （`value_text.startswith(f"{day}-")`）。哈希那段只要唯一即可。
    """
    return f"{day}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"


def make(day: str, text: str, evidence: str, key: str,
         kind: str = "routine") -> dict:
    """实时题一律是**文字题**。

    ⚠️ `format="text"` 会让 apply_answers 那边永远判成「没完成」（文字题
       没有完成这个概念），这正是我们要的：实时题不该去勾任何任务，
       它只是问你一句。所以 `ref` 必须是空的。
    """
    return {"id": qid(day, text), "kind": kind, "format": "text",
            "text": text, "evidence": evidence, "ref": "",
            "_key": key}


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
    lh = (snap or {}).get("last_hour") or {}
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
        bits.append(f"位置：{tag}{where}（停留 {_hm(float(stay['minutes']))}）")
    if d_apps:
        bits.append("电脑：" + " · ".join(f"{a} {_hm(m)}" for a, m in d_apps[:3]))
    elif d_total <= 0:
        bits.append("电脑：没有记录到活动")

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
    else:
        bits.append("手机：一次都没解锁")

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


def summary_questions(day_iso: str, stays: list[dict],
                      events: list[dict], snap: dict | None) -> list[dict]:
    """晚上收尾的三题。答完之后机器人会用 LLM 写一段当天总结。

    三题的用意各不相同：
      1. 让**你自己**挑出今天最重要的一件事 —— 数据里永远没有这个。
      2. 拿当天数据对照一下，看哪块和你的印象不一样。
      3. 收尾时把「没做成、想留到明天」的捞出来 —— 明天的问责批会用上。
    """
    facts: list[str] = []
    today = (snap or {}).get("today") or {}
    total = float(today.get("total_hours") or 0)
    if total:
        cats = today.get("by_category") or []
        top = " / ".join(f"{k} {v}h" for k, v in cats[:3])
        facts.append(f"电脑合计 {total} 小时（{top}）")
    places = [s for s in stays if float(s.get("minutes") or 0) >= 20]
    if places:
        best = max(places, key=lambda s: float(s.get("minutes") or 0))
        facts.append(f"去过 {len(places)} 个地方，最久的是{best.get('place')}"
                     f"（{_hm(float(best.get('minutes') or 0))}）")
    unlocks = sum(1 for e in events if e.get("metric") == "unlock")
    if unlocks:
        facts.append(f"手机解锁 {unlocks} 次")
    fact_line = "；".join(facts) if facts else "今天记录不多"

    return [
        make(day_iso, "今天最值得记住的一件事是什么？",
             "收尾题。随便写，会进当天的日记。",
             f"summary-{day_iso}-1", kind="summary"),
        make(day_iso, f"今天的数据：{fact_line}。\n哪一块和你想的不一样？",
             "数据来自电脑快照 / Dawarich / 中继。",
             f"summary-{day_iso}-2", kind="summary"),
        make(day_iso, "今天有什么没做成、想留到明天的？",
             "这条会成为明天问责的线索。",
             f"summary-{day_iso}-3", kind="summary"),
    ]


def build(day: Day, base: str, key: str) -> list[dict]:
    events = relay_phone_events(day.iso, base, key)
    stays = today_stays(day)
    snap = relay_desktop(base, key)
    now = datetime.now(TZ)

    # 已经发过的（按 key）—— 同一小时只问一次，收尾一天一次
    known = load_asked(day.iso)
    fired = {str(q.get("_key") or "") for q in known}

    # ── 晚上收尾 ──
    if _summary_due(now):
        prefix = f"summary-{day.iso}"
        if any(k.startswith(prefix) for k in fired):
            return known                  # 今天已经收过尾了
        try:
            return known + summary_questions(day.iso, stays, events, snap)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  收尾题生成失败：{type(e).__name__}: {e}")
            return known

    # 离收尾不到一小时就别再发小时题 —— 否则晚上连着两条。
    if _summary_soon(now):
        return known

    # ── 每小时一题 ──
    try:
        q = rule_hourly(day.iso, stays, events, snap, now)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  生成失败：{type(e).__name__}: {e}")
        q = None

    if q and q["_key"] not in fired:
        return known + [q]
    return known


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

    # ⚠️ 顺序：**先推中继、成功了再落本地**。
    #    反过来的话，一次推送失败会让这条规则当天再也不发（本地记着「已发」），
    #    而服务器上看不出任何异常 —— 正是这个项目一路在打的那类静默失效。
    payload = {"day": day.iso, "generated_at": now.isoformat(timespec="seconds"),
               "questions": [{k: v for k, v in q.items() if k != "_key"}
                             for q in questions]}
    try:
        r = httpx.post(f"{base}/questions/live", json=payload,
                       headers={"X-API-Key": key}, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  推不到中继 {base}：{e}")
        return 1
    if r.status_code == 404:
        # ⚠️ 404 几乎只有一个原因：服务器上跑的还是**旧版中继**，没有
        #    /questions/live 这条路由。实测踩到过 —— 报错本身看不出这个。
        print("  ⚠️  中继没有 /questions/live —— 服务器上跑的还是旧版代码。")
        print("     在服务器上：sudo /opt/lifelogging/repo/server/deploy.sh")
        return 1
    if r.status_code != 200:
        print(f"  ⚠️  中继返回 {r.status_code}：{r.text[:200]}")
        return 1

    save_asked(day.iso, questions)
    print(f"  OK  {r.json().get('count')} 条已推（本次新增 {len(new)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
