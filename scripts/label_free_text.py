#!/usr/bin/env python3
"""把每天的回答变成**带证据的标签** → `state/labels-llm.jsonl`。

**为什么要有它**

    七个维度每晚都问，但只有三个（心情/体力/睡眠）是 1–5 打分题。剩下四个
    （人际/吃饭/休息/学习）永远是一段话。话能进日记，进不了回归 ——
    而「这周社交少的那几天，第二天心情是不是更低」这种问题，需要的就是数字。

**但光有数字没用 —— 每个分数都要能追到证据**

    只存「体力 = 4」等于什么都没说。有用的是「体力 = 4，因为
    『地铁上直接睡着』，那天 07:02–08:02 的实时题里写着『坐地铁』」。
    所以每一行除了分数，还带四样东西：

        evidence  原文里**逐字存在**的一句话（≤30 字），是打分的依据
        cause     是什么导致的（≤25 字），必须能被 evidence 支撑
        links     这一维和**其他维度 / 当天某件事**的关系（0–3 条）
        confidence high / low —— 含糊的话标 low，别让它冒充观测值

    `evidence` 和每条 link 的 `quote` 会**逐字回查**：在（回答 + 备注 +
    当天实时问答）里找不到的，直接丢掉。模型编出来的证据进不了文件 ——
    这是这套东西唯一能防住「编一段合理的解释」的手段。

**三种关系，别混为一谈（这是本文件最该被读懂的一段）**

    1. 证据（evidence）       —— 可以**标注**。今天就能做。
    2. 关联（links）          —— 可以**记录成假设**。今天就能做，但要标成假设。
    3. 因果（causality）      —— **不可能**从这些数据里得出。

    所以 links 里存的是「他说『因为昨晚没睡好』，所以这一天的体力和睡眠
    有关系」—— **是他的说法，不是结论**。模型可以拿这些边当特征，但
    「吃饭不规律会不会让心情变差」最终要靠跨很多天的统计去检验，不能因为
    某一天他这么说过就写死成规则。

    换句话说：这里记的是**图的边**，不是**图的权重**。权重得数据自己长出来。

**它和 `labels.jsonl` 的区别（这条必须记住）**

    labels.jsonl      本人**亲口**打的 1–5。是观测值。
    labels-llm.jsonl  模型产出：本人没打分的维度由它打分；所有维度由它补证据。

    分数上，本人打的永远优先（`features.py` 只拿 AI 分去补**缺失**的维度）。
    证据上，没有「本人版」这回事 —— evidence/cause/links 全部是模型的解读，
    所以每一行都带 `score_from`（self / llm）标清楚分数的来路。

**信息不足就给 null**

    提示词里明确要求「不确定就 null，不要默认 3」。3 是一个判断，不是默认值。
    一个「被默认成 3」的样本比 null 更伤模型 —— 它看起来是观测值。

**幂等 + 花钱**

    同一个 qid 只处理一次，结果直接复用，反复跑不会重复计费。
    一天一次调用（7 个维度 + 当天实时问答打包在一个 prompt 里），
    走的还是那份成本台账。

用法：
  python scripts/label_free_text.py                # 只补没处理过的
  python scripts/label_free_text.py --dry-run      # 不调模型、不写盘
  python scripts/label_free_text.py --rebuild      # 忽略已有结果，全部重做
  python scripts/label_free_text.py --max-days 7   # 只处理最近 7 天
"""

from __future__ import annotations

import argparse
import json
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
OUT = STATE / "labels-llm.jsonl"
# 和 collect_labels 共用同一份「归属日纠正表」—— 两个脚本必须看到同一个日期，
# 否则同一道题的分数和证据会落在不同的天。
FIXUPS = STATE / "label-day-fixups.json"

# 提示词一改就 +1。已经处理过的行如果版本号对不上，会重做 ——
# 否则新旧两种输出会静默混在同一个文件里，而字段缺失是看不出来的。
PROMPT_VERSION = 2

# 维度和它的量表。⚠️ 维度名要和 `live_questions.ROUTINE_DIMENSIONS` 逐字对得上
# —— 不然打分打的是另一回事。
#
# ⚠️ **五档全部写出来，不留空档。** 早先只有 1/3/5，指望模型在两端之间插值；
#    实际结果是 2 和 4 全凭它当天的理解，同一句话隔几天能打出差两分的值 ——
#    那样攒出来的标签，跨时间不可比，而「跨时间可比」正是这套东西存在的理由。
#    每一档都得有一句**自己的**描述，不能写成「介于 1 和 3 之间」。
SCALES: dict[str, str] = {
    "体力": ("1 完全没电或身体明显不适"
             "；2 明显没劲，做什么都要硬撑"
             "；3 一般，能撑完要做的事"
             "；4 状态不错，大部分时间有力气"
             "；5 充沛"),
    "心情": ("1 很差，一整天都沉"
             "；2 偏低，多数时候不太提得起劲"
             "；3 平静或一般，没什么起伏"
             "；4 不错，多数时候是好的"
             "；5 很好"),
    "睡眠": ("1 很差，几乎没睡好"
             "；2 偏低，睡得不踏实或明显不够"
             "；3 一般，睡了但没完全缓过来"
             "；4 不错，基本恢复了"
             "；5 很好，醒来是清醒的"),
    "人际": ("1 完全没有与人相处，感到孤立"
             "；2 很少互动，基本一个人"
             "；3 有一点交流，但不多"
             "；4 有正常的来往，感觉还不错"
             "；5 有充分而满意的连接"),
    "吃饭": ("1 严重不规律，或直接跳过整餐"
             "；2 明显不规律，随便对付过去了"
             "；3 大致按点，但吃得很随便"
             "；4 基本规律，吃得还行"
             "；5 三餐规律，吃得认真"),
    "休息": ("1 完全没有休息（刷手机不算休息）"
             "；2 几乎没停下来，一直绷着"
             "；3 有一点空档，但没真缓过来"
             "；4 有过几段真正缓过来的时间"
             "；5 得到了充分休息"),
    "学习": ("1 没有任何新知"
             "；2 只是零散看到一点，没留下什么"
             "；3 有一点，但谈不上收获"
             "；4 有明确学到的东西"
             "；5 有明显收获，能说出学到了什么"),
}

SYSTEM_PROMPT = """你在把一天的日记式回答，变成**带证据的 1–5 分标签**。

量表（每一维都是 1 = 最差，5 = 最好）：
__SCALES__

规则（必须遵守）：

1. 只根据给出的材料打分：本人的回答、备注、以及【当天的实时问答】。
   不要引入任何外部信息，不要推测他没说的事。
2. 每一档都有**它自己的**描述。按最贴近的那一档打分，**不要插值、不要取平均**。
3. 如果某一维写着「本人已打分：N」，**score 必须原样返回 N**，不要改。
   这时 score_from 写 "self"；没有本人分数的写 "llm"。
4. **信息不足就给 null，不要默认成 3。** 3 是一个判断，不是默认值。

关于证据（这一节比打分重要）：

5. `evidence` 必须从材料里**逐字复制**一段话（≤30 字），不能改写、不能概括。
   材料里找不到依据就写 ""（空字符串），**不要编一句听起来合理的**。
6. `cause` 是「是什么导致的」（≤25 字，人或事，不要复述分数）。
   它必须能被 `evidence` 支撑；支撑不住就写 ""。
7. `links` 是**这一维和其他维度 / 当天某件事**的关系，0–3 条。
   - `to` 只能是这七个之一：体力、心情、睡眠、人际、吃饭、休息、学习；
     或者 "事件"（指【当天的实时问答】里的某一段）。
   - `relation` 目前只有 "explained_by"：这件事解释了这一维的分数。
   - `quote` 同样必须逐字存在。
   - **只在他说了才写。** 他没提到的关联不要凭常识补 —— 这里的每一条边
     都会进关联图，凭空造的边比没有边糟得多。

输出格式：**每行一个 JSON 对象**，一个维度一行：
{"dim": "体力", "score": 4, "score_from": "self", "confidence": "high",
 "why": "…", "evidence": "…", "cause": "…",
 "links": [{"to": "睡眠", "relation": "explained_by", "quote": "…"}]}
每个维度都要有一行，包括 score 为 null 的。不要输出任何 JSON 之外的东西。

⚠️ 为什么是「一行一个」而不是一个大 JSON 对象：输出可能被 max_tokens 截断。
   大对象一旦在中间断掉，`json.loads` 整段失败，**一整天的结果全丢**
   （实测 2026-10-20 丢过一次）。一行一个的话，截断最多损失最后一行。"""


def score_of(answer: str) -> int | None:
    """答案开头的 1–5。没有就返回 None —— 说明这是自由文本。

    ⚠️ 复用 `collect_labels.score_of`，不在这里另写一个正则。两条
       「什么算已经打过分了」的规则一旦分叉，同一段话就会被重复打分。
    """
    from collect_labels import score_of as _score_of

    return _score_of(answer)


def _norm(s: str) -> str:
    """压平空白，用于逐字回查。"""
    return " ".join(str(s or "").split())


def load_fixups() -> dict[str, str]:
    try:
        raw = json.loads(FIXUPS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _qa_payload(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _all_qa() -> list[tuple[str, dict]]:
    """[(文件名日期, qa 条目)]，按 ts 排序。"""
    out: list[tuple[str, dict]] = []
    for p in sorted(STATE.glob("qa-*.json")):
        data = _qa_payload(p)
        if not data:
            continue
        for x in (data.get("qa") or []):
            if isinstance(x, dict):
                out.append((p.stem.replace("qa-", ""), x))
    out.sort(key=lambda t: str(t[1].get("ts_utc") or ""))
    return out


def day_context(day: str) -> list[str]:
    """当天【实时问答】里的那几段 —— 给模型当「那天还发生了什么」的底料。

    ⚠️ 只取**有内容的**回答。`4 不错`、`一致` 这种没有信息量，塞进去只会
       让模型拿它硬凑证据。
    """
    lines: list[str] = []
    for _, x in _all_qa():
        if str(x.get("question_day") or "") != day:
            continue
        if str(x.get("source") or "") != "live":
            continue
        ans = _norm(x.get("answer"))
        if len(ans) < 6 or score_of(ans) is not None:
            continue
        q = _norm(x.get("text")).splitlines()
        head = q[0][:24] if q else ""
        lines.append(f"{head} → {ans[:110]}")
    return lines[:12]


_FIXUPS: dict[str, str] = {}


def pending_items() -> dict[str, dict[str, dict]]:
    """`{day: {dim: item}}` —— 每一个**答过的**维度都进来。

    ⚠️ 不再只挑「没打分的」。本人打了 4 分但只回了一个数字，**照样需要
       证据**：那一天到底因为什么给了 4 分，只能从当天的实时问答里找。
    """
    by_qid: dict[str, dict] = {}
    for _, x in _all_qa():
        dim = str(x.get("dim") or "").strip()
        if dim not in SCALES:
            continue
        qid = str(x.get("id") or "")
        if not qid:
            continue
        old = by_qid.get(qid)
        if old is None or str(x.get("ts_utc") or "") >= str(old.get("ts_utc") or ""):
            by_qid[qid] = x

    out: dict[str, dict[str, dict]] = {}
    for x in by_qid.values():
        dim = str(x.get("dim") or "").strip()
        answer = str(x.get("answer") or "").strip()
        note = str(x.get("note") or "").strip()
        if len(_norm(answer)) < 1 and not note:
            continue
        day = (_FIXUPS.get(str(x.get("id") or ""))
               or str(x.get("question_day") or str(x.get("id"))[:10]))
        out.setdefault(day, {})[dim] = x
    return out


def load_done() -> dict[str, dict]:
    """已经处理过的 qid → 那一行。版本号对不上的当作没做过。"""
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
        if not isinstance(row, dict) or not row.get("qid"):
            continue
        if row.get("prompt_version") != PROMPT_VERSION:
            continue
        done[str(row["qid"])] = row
    return done


def write(rows: list[dict]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")
    tmp.replace(OUT)


def parse_rows(raw: str) -> dict[str, dict]:
    """逐行解析。坏行跳过，不因为一行坏掉整段丢失。"""
    out: dict[str, dict] = {}
    for line in str(raw or "").splitlines():
        s = line.strip().rstrip(",")
        if not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        dim = str(obj.get("dim") or "").strip()
        if dim in SCALES:
            out[dim] = obj
    return out


def build_prompt(day: str, items: dict[str, dict], context: list[str]) -> str:
    parts = [f"# 日期：{day}", ""]
    if context:
        parts += ["## 当天的实时问答（每小时问的那几段）", ""]
        parts += [f"- {c}" for c in context]
        parts += ["", "⚠️ 上面的回答是**当时**写的，比晚上回顾更具体。"]
        parts += [""]

    parts += ["## 七个维度", ""]
    for dim, x in items.items():
        parts.append(f"### {dim}")
        self_score = score_of(x.get("answer") or "")
        if self_score is not None:
            parts.append(f"本人已打分：{self_score}")
        q = _norm(x.get("text"))
        parts.append(f"问：{q[:160]}")
        parts.append(f"答：{_norm(x.get('answer'))[:400]}")
        note = _norm(x.get("note"))
        if note:
            parts.append(f"备注：{note[:200]}")
        parts.append("")
    return "\n".join(parts)


def _valid_quote(quote: str, haystack: str) -> bool:
    """这句话真的是材料里的一句吗？"""
    q = _norm(quote)
    return len(q) >= 4 and q in haystack


def main() -> int:
    ap = argparse.ArgumentParser(description="自由文本 → 带证据的 1–5 标签（LLM）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="忽略已有结果，全部重做（会重新计费）")
    ap.add_argument("--max-days", type=int, default=0,
                    help="只处理最近 N 天（0 = 全部）")
    args = ap.parse_args()

    global _FIXUPS
    _FIXUPS = load_fixups()
    pending = pending_items()
    if args.max_days > 0:
        keep = sorted(pending)[-args.max_days:]
        pending = {d: pending[d] for d in keep}
    if not pending:
        print("  没有需要处理的自评回答。")
        return 0

    done = {} if args.rebuild else load_done()
    todo: dict[str, dict[str, dict]] = {}
    for day, items in pending.items():
        # ⚠️ 缓存的行必须**同一天**才算复用。改了归属日之后，qid 没变但
        #    day 变了 —— 只看 qid 的话，纠正过的题会带着旧日期一直被复用。
        fresh = {dim: x for dim, x in items.items()
                 if (done.get(str(x.get("id"))) or {}).get("day") != day}
        if fresh:
            todo[day] = fresh

    print(f"  有回答的天：{len(pending)}；其中 {len(todo)} 天需要处理"
          f"（已有 {len(done)} 条结果复用）")
    if not todo:
        return 0

    if args.dry_run:
        for day, items in todo.items():
            ctx = day_context(day)
            print(f"  [dry-run] {day}: {', '.join(items)}"
                  f"（当天实时问答 {len(ctx)} 段可作证据）")
        return 0

    ledger = CostLedger(CFG["budget"]["ledger_path"])
    try:
        from common import LLM  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 加载 LLM 失败：{type(e).__name__}: {e}")
        return 1

    system = SYSTEM_PROMPT.replace(
        "__SCALES__",
        "\n".join(f"- {d}：{s}" for d, s in SCALES.items()))

    # ⚠️ 只保留**没有被重做**的缓存行。改了归属日之后，同一个 qid 会带着
    #    新日期重新生成一条；旧的（日期错的）那条必须丢掉，否则文件里会同时
    #    存在「qid 相同、day 不同」的两条，下游按 day 分组时就多出一个不该
    #    存在的日子。
    redone: set[str] = set()
    rows: list[dict] = []
    failed = 0
    for day in sorted(todo):
        items = todo[day]
        context = day_context(day)
        # ⚠️ 每天新建一个 LLM 实例：它的调用计数上限默认是 8，
        #    复用同一个实例处理到第 9 天就会中止。见 activities.py 里同一条注释。
        llm = LLM(cfg=CFG, ledger=ledger)
        try:
            raw = llm.complete(system=system,
                               user=build_prompt(day, items, context),
                               stage="labels", run_id=new_run_id())
        except (BudgetExceeded, TruncatedOutput) as e:
            print(f"  ❌ {day}: {e}")
            failed += 1
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {day}: {type(e).__name__}: {e}")
            failed += 1
            continue

        got_rows = parse_rows(raw)
        if not got_rows:
            print(f"  ⚠️  {day}: 模型没给可解析的行，跳过"
                  f"（前 120 字：{raw[:120]!r}）")
            failed += 1
            continue

        # 证据只能来自这些材料 —— 逐字回查用的。
        haystack = _norm("\n".join(context))
        n_score = n_ev = n_link = 0
        for dim, x in items.items():
            got = got_rows.get(dim)
            if not isinstance(got, dict):
                continue
            answer = _norm(x.get("answer"))
            note = _norm(x.get("note"))
            local = _norm(f"{answer}\n{note}\n{haystack}")

            self_score = score_of(x.get("answer") or "")
            score = got.get("score")
            if self_score is not None:
                # 本人打了分，模型不许改 —— 观测值永远压过估计值。
                score, score_from = self_score, "self"
            elif isinstance(score, int) and 1 <= score <= 5:
                score_from = "llm"
            else:
                score, score_from = None, "llm"

            evidence = _norm(got.get("evidence"))
            if evidence and not _valid_quote(evidence, local):
                evidence = ""              # 编的，丢掉
            cause = _norm(got.get("cause"))[:25]
            if not evidence:
                cause = ""                 # 没有证据就不给原因

            links: list[dict] = []
            for lk in (got.get("links") or [])[:3]:
                if not isinstance(lk, dict):
                    continue
                to = str(lk.get("to") or "").strip()
                quote = _norm(lk.get("quote"))
                if to not in SCALES and to != "事件":
                    continue
                if to == dim:
                    continue
                if not _valid_quote(quote, local):
                    continue               # 编的边，丢掉
                links.append({"to": to, "relation": "explained_by",
                              "quote": quote[:60]})

            if score is None and not evidence:
                redone.add(str(x.get("id") or ""))
                continue                   # 什么都没有，不必占一行
            redone.add(str(x.get("id") or ""))
            rows.append({
                "day": day,
                "dim": dim,
                "score": score,
                "score_from": score_from,
                "confidence": str(got.get("confidence") or ""),
                "why": _norm(got.get("why"))[:40],
                "evidence": evidence[:60],
                "cause": cause,
                "links": links,
                "qid": str(x.get("id") or ""),
                "ts_utc": str(x.get("ts_utc") or ""),
                "prompt_version": PROMPT_VERSION,
            })
            n_score += 1 if score is not None else 0
            n_ev += 1 if evidence else 0
            n_link += len(links)

        print(f"  OK  {day}: {n_score}/{len(items)} 维有分，"
              f"{n_ev} 条有证据，{n_link} 条关联")

    rows = [r for r in done.values() if str(r.get("qid")) not in redone] + rows

    by_slot: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = (str(r.get("day")), str(r.get("dim")))
        old = by_slot.get(k)
        if old is None or str(r.get("ts_utc") or "") >= str(old.get("ts_utc") or ""):
            by_slot[k] = r
    final = sorted(by_slot.values(), key=lambda r: (r["day"], r["dim"]))
    write(final)
    print(f"  OK  {len(final)} 条 → {OUT}")
    return 0 if not failed else 0     # 失败不影响夜间流水线，日志里已写清楚


if __name__ == "__main__":
    sys.exit(main())
