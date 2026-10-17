#!/usr/bin/env python3
"""服务器侧：白天根据**当天**的位置/手机数据，实时问你几句。

**为什么要有它**

    原来只有一套提问：笔记本晚上拿「昨天日记」生成，次日早上问回来。
    它问的是**昨天**的事，而且是「拿你自己写的话堵你」——那很有用，但它
    看不见今天正在发生的事。

    这个脚本补的就是那一块：白天在服务器上扫今天的数据，发现值得一嘴的
    片段就问。比如「你 14:05–16:30 在某个地方待了两小时半，那是在做什么？」
    ——**在它还新鲜的时候问**，而不是等明天。

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

**同一件事不重复问**

    每条规则有个稳定的 key；当天发过就记进 `live-asked-<day>.json`，不再发。
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
import sys
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
MAX_PER_DAY = 3

MIN_STAY_MIN = 90        # 停留多久才值得问
MIN_SILENCE_MIN = 90     # 手机静默多久才值得问
MIN_APP_MIN = 150        # 某个应用前台多久才值得问


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


def make(day: str, text: str, evidence: str, key: str) -> dict:
    """实时题一律是**文字题**。

    ⚠️ `format="text"` 会让 apply_answers 那边永远判成「没完成」（文字题
       没有完成这个概念），这正是我们要的：实时题不该去勾任何任务，
       它只是问你一句。所以 `ref` 必须是空的。
    """
    return {"id": qid(day, text), "kind": "routine", "format": "text",
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


def _hhmm(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(str(iso)).astimezone(TZ).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def _hm(minutes: float) -> str:
    total = int(round(minutes))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} 小时 {m} 分"
    return f"{h} 小时" if h else f"{m} 分"


# ── 规则 ────────────────────────────────────────────────────────────
def rule_long_stay(day_iso: str, stays: list[dict], _events: list[dict]) -> dict | None:
    """在某地待了很久 —— 那是在干什么？"""
    best = None
    for s in stays:
        if s["minutes"] >= MIN_STAY_MIN and (best is None or s["minutes"] > best["minutes"]):
            best = s
    if not best:
        return None

    where = best["place"]
    span = f"{_hhmm(best['start'])}–{_hhmm(best['end'])}"
    # ⚠️ 正文里**不要写「今天」**。这题可能隔天才被读到，而「昨天的事」用
    #    「今天」的口吻说出来，看着完全正常但它是错的。日期由渲染那一层按
    #    题目真实的 day 补上（见 feishu_bot.render_question）。
    if best["is_coord"]:
        # 反查不出地名时**别装作认识那个地方**，直接用坐标，
        # 并在证据里说明为什么是坐标。
        text = f"{span} 你在 {where} 待了 {_hm(best['minutes'])}，那是在做什么？"
        evidence = ("位置来自手机 GPS / Dawarich；服务器上没有城市数据表，"
                    f"所以只有坐标。（{day_iso}）")
    else:
        text = f"{span} 你在{where}待了 {_hm(best['minutes'])}，那是在做什么？"
        evidence = f"位置来自手机 GPS / Dawarich 的停留推导（{where}，{day_iso}）。"
    return make(day_iso, text, evidence, f"longstay-{best['start']}")


def rule_phone_silence(day_iso: str, _stays: list[dict], events: list[dict]) -> dict | None:
    """手机长时间没解锁 —— 那段时间在干嘛？"""
    unlocks = []
    for e in events:
        if e.get("metric") != "unlock":
            continue
        ts = e.get("ts_utc")
        try:
            dt = datetime.fromisoformat(str(ts)).astimezone(TZ)
        except (TypeError, ValueError):
            continue
        unlocks.append(dt)
    if len(unlocks) < 2:
        return None
    unlocks.sort()

    best = None
    for a, b in zip(unlocks, unlocks[1:]):
        gap = (b - a).total_seconds() / 60.0
        # 只看白天到睡前的空档：凌晨那八小时本来就不解锁，
        # 拿它发问是噪音（而且每天早上都会发一条）。
        if a.hour < ASK_FROM_HOUR or a.hour >= ASK_UNTIL_HOUR:
            continue
        if gap >= MIN_SILENCE_MIN and (best is None or gap > best[2]):
            best = (a, b, gap)
    if not best:
        return None

    a, b, gap = best
    # 同上：不写「今天」，日期交给渲染层。
    text = (f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')} 手机一次都没解锁"
            f"（{_hm(gap)}），那段时间在干嘛？")
    return make(day_iso, text,
                f"来自中继上的手机解锁事件（Tasker 上报），间隔是实测的。（{day_iso}）",
                f"silence-{a.isoformat()}")


def rule_heavy_app(day_iso: str, _stays: list[dict], events: list[dict]) -> dict | None:
    """某个应用今天用了很久 —— 值得一嘴。"""
    try:
        from normalize import summarize_phone
    except Exception:  # noqa: BLE001
        return None
    try:
        s = summarize_phone(events)
    except Exception:  # noqa: BLE001
        return None

    top = s.get("top_apps") or []
    if not top:
        return None
    name, minutes = top[0]
    if minutes < MIN_APP_MIN:
        return None
    # 同上：不写「今天」。
    text = f"{name} 用了 {_hm(minutes)}，是整天都开着，还是真的在用它？"
    return make(day_iso, text,
                f"来自手机前台时长（相邻两次应用切换的时间差推导，是**下界**）："
                f"{day_iso} 合计前台 {_hm(s.get('foreground_minutes') or 0)}。",
                f"heavyapp-{name}")


RULES = (rule_long_stay, rule_phone_silence, rule_heavy_app)


def build(day: Day, base: str, key: str) -> list[dict]:
    events = relay_phone_events(day.iso, base, key)
    stays = today_stays(day)

    # 已经发过的（按 key）—— 同一条规则当天只发一次
    known = load_asked(day.iso)
    fired = {str(q.get("_key") or "") for q in known}

    fresh: list[dict] = []
    for rule in RULES:
        if len(known) + len(fresh) >= MAX_PER_DAY:
            break
        try:
            q = rule(day.iso, stays, events)
        except Exception as e:  # noqa: BLE001
            # 一条规则炸了不该让别的规则也不跑。
            print(f"  ⚠️  规则 {rule.__name__} 出错：{type(e).__name__}: {e}")
            continue
        if q and q["_key"] not in fired:
            fresh.append(q)

    return known + fresh


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
