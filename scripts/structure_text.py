#!/usr/bin/env python3
"""把人写的自由文本变成**结构化事件** → `state/events.jsonl`。

**为什么要有它**

    这个系统里信息量最大的东西，一直是**人自己写的那几段话**：

        每小时回答   「在图书馆坐了一会儿之后去跑了个步，1 km warm up, 4 x 400m」
        问责备注     「不需要做」「打开 OBS 是为了照镜子」
        记：笔记      随手一句

    它们现在只进了日记正文。在结构化数据里一个字都没有 —— 也就是说，
    一天里最像「人」的那部分，对任何统计都不可见。

    而它们恰好补上了传感器看不见的另一半：笔记本合着的时候，屏幕上什么都没有，
    但人是在上课、在通勤、在睡觉、在帮人干活。
    **桌面数据是 unobserved，人写下的那句话是 observed。**

**这一次比「分个类」多抠了几层**

    只把「上英语课」归成 Study，几乎等于没做。所以每条事件都尽量带：

        activity / category  在做什么        place     在哪
        people               和谁            plan      提到的打算
        mood_signal          情绪线索        task_refs 提到的任务
        outcome / reason / reason_class     问责题的结果和原因

    ⚠️ 每一层都是**可空**的，而且空就是空。没提到人物就写空数组，
       不要根据常识补一个「同学」进去。

**逐字回查**

    每条事件必须带 `quote`，且必须是原文里**逐字存在**的一段。对不上就丢掉这条。
    没有这道闸，模型会把「在课上睡觉」写成「在宿舍睡觉」——
    看着没差别，其实是换了事实。

**为什么不处理日记正文**

    `vault/05-Journal/<day>.md` 是模型**根据这些数据写出来的**。再拿它去
    提取事件，是把模型的推断当成观察，绕一圈回来污染原始层。
    任务表同理：它已经是半结构化的 YAML/Markdown，不需要模型再猜。

    真要从日记里取东西，那属于「回顾」，不是「采集」。

**吞吐**

    一天一次调用（这一天的所有来源打包在一个 prompt 里）。
    ⚠️ 每次循环新建一个 LLM 实例：它的调用计数上限默认是 8，
    复用会处理到第 9 天就中止。

用法：
  python scripts/structure_text.py                 # 只处理没处理过的
  python scripts/structure_text.py --dry-run
  python scripts/structure_text.py --rebuild       # 全部重做
  python scripts/structure_text.py --day 2026-10-23
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    BudgetExceeded,
    CostLedger,
    TruncatedOutput,
    load_config,
    new_run_id,
)

CFG = load_config()
STATE = Path(CFG["state_dir"])
OUT = STATE / "events.jsonl"

PROMPT_VERSION = 2

# ⚠️ 前八个和 `config/config.yaml` 的 categories、`normalize.py` 一致 ——
#    不一致的话，同一个「学习」在活动和桌面里会是两个不同的字符串。
#    `Sleep` 是这里多出来的：桌面数据里没有「睡觉」（睡着了就没有窗口），
#    但它是最该被记录的活动之一。
CATEGORIES = ("Work", "Study", "Life", "Transport", "Leisure",
              "Health", "Social", "Other", "Sleep")

# 问责题「没做 / 只做了一部分」的原因分类。
#
# ⚠️ 这是**可数**的六类，不是自由文本。分类可数才能进回归；
#    自由文本还得再跑一次模型去归类，那是第二层噪音。
REASON_CLASSES = (
    "plan_changed",     # 计划本身变了：课调了、任务取消了
    "chose_not_to",     # 主动选择不做：不想做、觉得不值得
    "interrupted",      # 被外部打断：有人找、临时有事
    "body",             # 身体原因：太累、病了、睡过去了
    "environment",      # 工具/环境不配合：找不到资料、设备坏了
    "mistake",          # 判断失误：算错时间、记错、忘了
    "not_applicable",   # 本来就不需要做
)

_SPAN_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[–\-—~]\s*(\d{1,2}):(\d{2})")

SYSTEM_PROMPT = """你在把一个人自己写下的文字，变成结构化的事件记录。

输入分三种来源，每条前面有编号和来源标记：

  [hourly]      「HH:MM–HH:MM 这一段在忙什么」的回答 —— 一段时间里做了什么
  [commitment]  问责题：任务做到没有，以及他补的一句说明
  [note]        他随手记的一句话

输出**每行一个 JSON 对象**，一条输入对应一行，`i` 原样返回：

{"i": 0, "activity": "跑步", "category": "Health", "place": "图书馆",
 "people": [], "plan": "", "mood_signal": "", "task_refs": [],
 "quote": "去跑了个步", "confidence": "high"}

问责题额外带 outcome / reason / reason_class：
{"i": 3, "outcome": "not_done", "reason": "不需要做",
 "reason_class": "not_applicable", "quote": "不需要做", "confidence": "high"}

字段要求：

- `activity`：2–8 个字的动宾短语，不要整句复述。
- `category`：只能取
    Work（写代码、做项目）/ Study（上课、自习、看书）
    Life（吃饭、洗漱、家务）/ Transport（通勤、坐车、走路）
    Leisure（刷手机、看视频、打游戏）/ Health（跑步、健身）
    Social（聚会、见人）/ Sleep（睡觉、午休）/ Other
- `place`：原文提到的地点，原样摘。没提就 ""。
- `people`：原文提到的**具体的人**（"我哥"、"同学"、"老师"）。没提就 []。
  ⚠️ 不要凭常识补。他说"跟人聊了会"，没说是谁 → []。
- `plan`：原文里提到的**打算/计划**（"明天要交"）。没有就 ""。
- `mood_signal`：一句情绪线索（"被老师的事耗掉"）。没有就 ""。
- `task_refs`：原文里提到的**待办/任务**（"刷学习通"）。没有就 []。
- `outcome`（只问责题）：done / partial / not_done / unclear
- `reason`（只问责题）：他没做到的原因，动宾短语（≤15 字）。没写就 ""。
- `reason_class`（只问责题）：只能取
    plan_changed（计划变了）/ chose_not_to（主动不做）/ interrupted（被打断）
    body（身体原因）/ environment（工具环境）/ mistake（记错忘了）
    not_applicable（本来就不需要做）
- `quote`：原文里**逐字复制**的一小段（≤20 字），是你判断的依据。
  ⚠️ 会被逐字回查，对不上这条就作废。
- `confidence`：明确写 high；只有一两个词、看不出具体内容写 low。

规则：
1. 只根据原文判断，不要补任何原文没有的事。
2. 一条输入里同时有多件事时，取**占时间最多**的那件做 activity。
3. 问责题的答案本身就是 yes/no/做了/没做 时，照它给 outcome，
   reason 只从他补的那句话里取，没有就 ""。
4. 不要输出 JSON 之外的任何文字，不要代码块围栏。"""


def _norm(s: str) -> str:
    return " ".join(str(s or "").split())


def _span(text: str) -> tuple[str, str]:
    m = _SPAN_RE.search(text or "")
    if not m:
        return "", ""
    return f"{int(m.group(1)):02d}:{m.group(2)}", f"{int(m.group(3)):02d}:{m.group(4)}"


def _qa_files() -> list[Path]:
    return sorted(STATE.glob("qa-*.json"))


def _qa_rows() -> list[dict]:
    """所有 qa 条目（去重、按 ts 取最新）。"""
    by_qid: dict[str, dict] = {}
    for p in _qa_files():
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for x in (payload.get("qa") or []):
            if not isinstance(x, dict):
                continue
            qid = str(x.get("id") or "")
            if not qid:
                continue
            old = by_qid.get(qid)
            if old is None or str(x.get("ts_utc") or "") >= str(old.get("ts_utc") or ""):
                by_qid[qid] = x
    return list(by_qid.values())


def _notes(day: str) -> list[dict]:
    p = STATE / f"timeline-{day}.json"
    try:
        tl = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for i, n in enumerate(tl.get("notes") or []):
        text = _norm(n.get("text"))
        if text:
            out.append({"src": "note", "key": f"{day}-note-{i}",
                        "text": text, "ts_utc": str(n.get("ts") or "")})
    return out


def gather(day: str) -> list[dict]:
    """这一天的所有待结构化条目，按来源和顺序排好。"""
    items: list[dict] = []
    for x in _qa_rows():
        if str(x.get("question_day") or "") != day:
            continue
        qtext = str(x.get("text") or "")
        answer = _norm(x.get("answer"))
        note = _norm(x.get("note"))
        kind = str(x.get("kind") or "")
        src = str(x.get("source") or "")

        if (kind == "routine" and src == "live"
                and "这一段" in qtext and _SPAN_RE.search(qtext)):
            if not answer:
                continue
            items.append({"src": "hourly", "key": str(x.get("id")),
                          "text": answer, "question": qtext,
                          "ts_utc": str(x.get("ts_utc") or "")})
        elif kind == "commitment":
            payload = note or answer
            if not _norm(payload):
                continue
            items.append({"src": "commitment", "key": str(x.get("id")),
                          "text": payload, "question": qtext,
                          "answer": answer,
                          "ts_utc": str(x.get("ts_utc") or "")})

    items += _notes(day)
    items.sort(key=lambda it: (it["ts_utc"], it["key"]))
    return items


def load_done() -> dict[str, dict]:
    done: dict[str, dict] = {}
    try:
        lines = OUT.read_text(encoding="utf-8").splitlines()
    except OSError:
        return done
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(row, dict) and row.get("key")
                and row.get("prompt_version") == PROMPT_VERSION):
            done[str(row["key"])] = row
    return done


def build_prompt(day: str, items: list[dict]) -> str:
    parts = [f"# 日期：{day}", ""]
    for i, it in enumerate(items):
        a, b = _span(it.get("question") or "")
        head = f"## {i}　[{it['src']}]"
        if a and b:
            head += f"　{a}–{b}"
        parts.append(head)
        if it["src"] == "commitment":
            parts.append(f"答案：{it.get('answer') or '(无)'}")
            parts.append(f"补充：{it['text']}")
        else:
            parts.append(f"内容：{it['text'][:400]}")
        parts.append("")
    return "\n".join(parts)


def parse_rows(raw: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for line in str(raw or "").splitlines():
        s = line.strip().rstrip(",")
        if not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("i"), int):
            out[obj["i"]] = obj
    return out


def write(rows: list[dict]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    tmp.replace(OUT)


def main() -> int:
    ap = argparse.ArgumentParser(description="自由文本 → 结构化事件")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--day", default="")
    args = ap.parse_args()

    days = sorted({str(x.get("question_day") or "") for x in _qa_rows()})
    days = [d for d in days if d]
    all_days = set(days) | {p.stem.replace("timeline-", "")
                            for p in STATE.glob("timeline-*.json")}
    if args.day:
        all_days = {args.day}

    done = {} if args.rebuild else load_done()
    todo: dict[str, list[dict]] = {}
    for d in sorted(all_days):
        items = [it for it in gather(d) if str(it["key"]) not in done]
        if items:
            todo[d] = items

    print(f"  {len(all_days)} 天；需要处理 {len(todo)} 天"
          f"（已有 {len(done)} 条复用）")
    if not todo:
        return 0
    if args.dry_run:
        for d, items in todo.items():
            by: dict[str, int] = {}
            for it in items:
                by[it["src"]] = by.get(it["src"], 0) + 1
            print(f"  [dry-run] {d}: " +
                  " · ".join(f"{k} {v}" for k, v in sorted(by.items())))
        return 0

    ledger = CostLedger(CFG["budget"]["ledger_path"])
    try:
        from common import LLM  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 加载 LLM 失败：{type(e).__name__}: {e}")
        return 1

    rows: list[dict] = list(done.values())
    for day in sorted(todo):
        items = todo[day]
        # ⚠️ 每天新建实例：调用计数上限默认 8，复用会卡在第 9 天。
        llm = LLM(cfg=CFG, ledger=ledger)
        try:
            raw = llm.complete(system=SYSTEM_PROMPT,
                               user=build_prompt(day, items),
                               stage="structure", run_id=new_run_id())
        except (BudgetExceeded, TruncatedOutput) as e:
            print(f"  ❌ {day}: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {day}: {type(e).__name__}: {e}")
            continue

        got = parse_rows(raw)
        if not got:
            print(f"  ⚠️  {day}: 模型没给可解析的行（前 100 字：{raw[:100]!r}）")
            continue

        n = 0
        for i, it in enumerate(items):
            o = got.get(i)
            if not isinstance(o, dict):
                continue
            source = it["text"]
            quote = _norm(o.get("quote"))[:40]
            if quote and quote not in source:
                quote = ""                      # 编的，丢掉
            activity = _norm(o.get("activity"))[:16]
            reason = _norm(o.get("reason"))[:20]
            if not quote and not (activity or reason):
                continue

            cat = str(o.get("category") or "").strip()
            if cat not in CATEGORIES:
                cat = ""
            rc = str(o.get("reason_class") or "").strip()
            if rc not in REASON_CLASSES:
                rc = ""

            def _list(v, limit=6) -> list[str]:
                if not isinstance(v, list):
                    return []
                return [_norm(x)[:16] for x in v if _norm(x)][:limit]

            start, end = _span(it.get("question") or "")
            rows.append({
                "day": day,
                "src": it["src"],
                "key": it["key"],
                "start": start,
                "end": end,
                "activity": activity,
                "category": cat,
                "place": _norm(o.get("place"))[:20],
                "people": _list(o.get("people")),
                "plan": _norm(o.get("plan"))[:40],
                "mood_signal": _norm(o.get("mood_signal"))[:40],
                "task_refs": _list(o.get("task_refs")),
                "outcome": (str(o.get("outcome") or "").strip()
                            if it["src"] == "commitment" else ""),
                "reason": reason,
                "reason_class": rc,
                "quote": quote,
                "confidence": str(o.get("confidence") or ""),
                "ts_utc": it.get("ts_utc") or "",
                "prompt_version": PROMPT_VERSION,
            })
            n += 1
        print(f"  OK  {day}: {n}/{len(items)} 条")

    rows.sort(key=lambda r: (r["day"], r["start"], r["key"]))
    write(rows)
    print(f"  OK  {len(rows)} 条 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
