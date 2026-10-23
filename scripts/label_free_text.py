#!/usr/bin/env python3
"""把**自由文本回答**用 LLM 打分 → `state/labels-llm.jsonl`（1–5 / 天 / 维）。

**为什么要有它**

    七个维度每晚都问，但只有三个（心情/体力/睡眠）是 1–5 打分题。剩下四个
    （人际/吃饭/休息/学习）永远是一段话。话能进日记，进不了回归 ——
    而「这周社交少的那几天，第二天心情是不是更低」这种问题，需要的就是数字。

**它和 `labels.jsonl` 的区别（这条必须记住）**

    labels.jsonl      本人**亲口**打的 1–5。是观测值。
    labels-llm.jsonl  模型**从文本推**的 1–5。是估计值。

    两者**永远不合并**。`features.py` 把它们写成 `labels` / `labels_llm`
    两个字段，模型推出来的只补自评没有的维度，绝不覆盖本人打的分数。
    混成一列之后，没有任何办法再分清哪个数是人说的。

    这也是为什么 `collect_labels.py` 那条「不猜」的规则仍然有效：
    没人让你猜的那条路上，一个字都不猜。

**信息不足就给 null**

    提示词里明确要求「不确定就 null，不要默认 3」。3 是一个判断，不是默认值。
    一个「被默认成 3」的样本，比一个 null 更伤模型 —— 它看起来是观测值。

**幂等 + 花钱**

    同一个 qid 只打一次分，结果从 labels-llm.jsonl 里直接复用。所以反复跑
    不会重复计费。一天一次调用（7 个维度打包在一个 prompt 里），
    走的还是那份成本台账和每日额度。

用法：
  python scripts/label_free_text.py                # 只补没打过的
  python scripts/label_free_text.py --dry-run      # 不调模型、不写盘
  python scripts/label_free_text.py --rebuild      # 忽略已有结果，全部重打
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

# 维度和它的量表。⚠️ 顺序和文案要和 `live_questions.ROUTINE_DIMENSIONS` 对得上
# —— 不然打分打的是另一回事。
SCALES: dict[str, str] = {
    "体力": "1 完全没电或身体明显不适；3 一般、能撑；5 充沛",
    "心情": "1 很差；3 平静或一般；5 很好",
    "睡眠": "1 很差；3 一般；5 很好",
    "人际": "1 完全没有与人相处、感到孤立；3 有一点但不多；5 有充分而满意的连接",
    "吃饭": "1 严重不规律或跳过整餐；3 大致按点但随便；5 三餐规律、吃得认真",
    "休息": "1 完全没有休息（刷手机不算休息）；3 有一点；5 得到了充分休息",
    "学习": "1 没有任何新知；3 有一点；5 有明显收获",
}

SYSTEM_PROMPT = """你在把一段自由文本的生活回答，按给定量表打成 1–5 分。

量表（每一维都是 1 = 最差，5 = 最好）：
__SCALES__

规则（必须遵守）：

1. 只根据给出的那段回答打分。不要引入任何外部信息，不要推测他没说的事。
2. **信息不足就给 null，不要默认成 3。** 3 是一个判断，不是默认值。
   一句「还行」打不出 3 分，它是 null。
3. 回答具体、明确 → confidence 写 "high"；含糊、一句带过 → 照给分数但是
   confidence 写 "low"；完全没提到这一维 → score null + confidence "low"。
4. why 不超过 15 个字，指出你是**根据哪个词/哪件事**打的，不要复述回答。
5. 输出格式：**每行一个 JSON 对象**，一个维度一行，形如
   {"dim": "体力", "score": 3, "confidence": "high", "why": "…"}
   每个维度都要有一行，包括 score 为 null 的。
6. 不要输出任何 JSON 之外的东西，不要代码块围栏。

⚠️ 为什么是「一行一个」而不是一个大 JSON 对象：模型的输出可能被 max_tokens
   截断。大对象一旦在中间断掉，`json.loads` 整段失败，**一整天的分全丢**
   （实测 2026-10-20 就丢过一次）。一行一个的话，截断最多损失最后一行。"""


def score_of(answer: str) -> int | None:
    """答案开头的 1–5。没有就返回 None —— 说明这是自由文本，需要模型。

    ⚠️ 复用 `collect_labels.score_of`，不在这里另写一个正则。两条
       「什么算已经打过分了」的规则一旦分叉，就会出现「collect_labels 认得、
       这里也认得」，或者反过来 —— 后者会让同一段话被模型再打一次分。
    """
    from collect_labels import score_of as _score_of

    return _score_of(answer)


def qa_files() -> list[Path]:
    return sorted(STATE.glob("qa-*.json"))


def pending_items() -> dict[str, dict[str, dict]]:
    """`{day: {dim: qa_item}}` —— 只有**自由文本**的那些才进来。

    ⚠️ 已经是 1–5 的答案在这里被跳过：那已经是一个数字，再让模型打一遍分
       只会引入一层噪音。自评永远优先。
    """
    # qid → 最近一次（一天可能答两次）
    by_qid: dict[str, dict] = {}
    for p in qa_files():
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for x in (payload.get("qa") or []):
            if not isinstance(x, dict):
                continue
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
        if score_of(x.get("answer") or "") is not None:
            continue                      # 已经是数字，不需要模型
        answer = str(x.get("answer") or "").strip()
        if len(answer) < 2:
            continue                      # 空/占位，问了也只会返回 null
        day = str(x.get("question_day") or str(x.get("id"))[:10])
        out.setdefault(day, {})[dim] = x
    return out


def load_done() -> dict[str, dict]:
    """已经打过分的 qid → 那一行。用于跳过重复计费。"""
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
        if isinstance(row, dict) and row.get("qid"):
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
    """逐行解析 `{"dim":…, "score":…}`。坏行跳过，不因为一行坏掉整段丢失。

    ⚠️ 这里**故意宽松**：模型偶尔会包 ```json 围栏、偶尔在中间断掉、
       偶尔在 JSON 后面多写一句话。逐行扫 + 跳过坏行，比 `json.loads` 整段
       解析稳得多 —— 后者的失败模式是「这一天一分都没有」。
    """
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


def build_prompt(day: str, items: dict[str, dict]) -> str:
    parts = [f"# 日期：{day}", ""]
    for dim, x in items.items():
        parts += [f"## {dim}", f"问：{x.get('text') or '(题干缺失)'}",
                  f"答：{x.get('answer') or ''}", ""]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="自由文本 → 1–5 分数（LLM）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="忽略已有结果，全部重打（会重新计费）")
    ap.add_argument("--max-days", type=int, default=0,
                    help="只处理最近 N 天（0 = 全部）")
    args = ap.parse_args()

    pending = pending_items()
    if args.max_days > 0:
        keep = sorted(pending)[-args.max_days:]
        pending = {d: pending[d] for d in keep}
    if not pending:
        print("  没有需要打分的自由文本。")
        return 0

    done = {} if args.rebuild else load_done()
    todo: dict[str, dict[str, dict]] = {}
    for day, items in pending.items():
        fresh = {dim: x for dim, x in items.items()
                 if str(x.get("id")) not in done}
        if fresh:
            todo[day] = fresh

    print(f"  自由文本：{len(pending)} 天；其中 {len(todo)} 天需要打分"
          f"（已有 {len(done)} 条结果复用）")
    if not todo:
        return 0

    if args.dry_run:
        for day, items in todo.items():
            print(f"  [dry-run] {day}: {', '.join(items)}")
        return 0

    ledger = CostLedger(CFG["budget"]["ledger_path"])
    try:
        from common import LLM

        llm = LLM(cfg=CFG, ledger=ledger)
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 初始化 LLM 失败：{type(e).__name__}: {e}")
        return 1

    system = SYSTEM_PROMPT.replace(
        "__SCALES__",
        "\n".join(f"- {d}：{s}" for d, s in SCALES.items()))

    rows: list[dict] = list(done.values())
    failed = 0
    for day in sorted(todo):
        items = todo[day]
        try:
            raw = llm.complete(system=system, user=build_prompt(day, items),
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
            print(f"  ⚠️  {day}: 模型没给可解析的行，跳过（前 120 字：{raw[:120]!r}）")
            failed += 1
            continue

        n = 0
        for dim, x in items.items():
            got = got_rows.get(dim)
            if not isinstance(got, dict):
                continue
            score = got.get("score")
            if not isinstance(score, int) or not (1 <= score <= 5):
                continue                  # null / 越界 —— 不猜，不写
            rows.append({
                "day": day,
                "dim": dim,
                "score": score,
                "qid": str(x.get("id") or ""),
                "ts_utc": str(x.get("ts_utc") or ""),
                "answer": str(x.get("answer") or ""),
                "note": str(x.get("note") or ""),
                "source": "llm",
                "confidence": str(got.get("confidence") or ""),
                "why": str(got.get("why") or "")[:40],
            })
            n += 1
        print(f"  OK  {day}: {n}/{len(items)} 维有分")

    # 按 (day, dim) 去重，取 ts 最大 —— 和 collect_labels 同一套规则。
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
