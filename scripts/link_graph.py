#!/usr/bin/env python3
"""把标签和证据拼成一张**图** → `state/graph.json`。

**为什么要有它**

    `labels-llm.jsonl` 里已经有两类东西：

      · 每个「天 × 维度」的分数 + 一句逐字证据 —— 这是**节点**
      · 每条 `links`（「因为昨晚没睡好，所以今天体力差」）—— 这是**边**

    但它们躺在一个 JSONL 里，没有任何东西把它们连起来。这个脚本负责连接，
    并且把两种**性质完全不同**的边分开存：

        attributed   他说过的关联。是**证词**，不需要样本量，但不能当因果。
        co_movement  从数字里算出来的相关。是**统计**，需要足够多的天。

    ⚠️ 这两种边绝不能混成一个字段。前者可能只有一天支撑（他那天心情不好
       随口归因于没睡好），后者要求「连着几十天都这样」。一条被标成
       `attributed` 的边，权重是 0 还是 1，得等 `co_movement` 自己长出来。

**为什么现在算不出统计边**

    lag-1 相关至少要两周的连续数据才不至于全是噪音。现在只有几天，
    脚本会明说「样本不足」并只导出证词边 —— **不输出一个看起来像结论的
    数字**，那比不输出糟糕得多。

用法：
  python scripts/link_graph.py
  python scripts/link_graph.py --min-days 14
  python scripts/link_graph.py --json          # 只打图，不打印摘要
"""

from __future__ import annotations

import argparse
import json
import sys
from itertools import permutations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import load_config  # noqa: E402

CFG = load_config()
STATE = Path(CFG["state_dir"])
OUT = STATE / "graph.json"

DIMS = ("体力", "心情", "睡眠", "人际", "吃饭", "休息", "学习")


def _read(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("day") and row.get("dim"):
            out.append(row)
    return out


def collect_scores() -> dict[str, dict[str, dict]]:
    """`{day: {dim: {score, from, evidence, cause}}}`。

    ⚠️ 本人自评压过 AI 分：同一个（天，维度）两边都有时用 self 那条。
       AI 那条里的证据仍然保留 —— 分数是观测值，证据是解读，两件事。
    """
    out: dict[str, dict[str, dict]] = {}

    for r in _read(STATE / "labels-llm.jsonl"):
        day, dim = str(r["day"]), str(r["dim"])
        if dim not in DIMS:
            continue
        out.setdefault(day, {})[dim] = {
            "score": r.get("score"),
            "from": str(r.get("score_from") or "llm"),
            "evidence": str(r.get("evidence") or ""),
            "cause": str(r.get("cause") or ""),
            "confidence": str(r.get("confidence") or ""),
            "links": [lk for lk in (r.get("links") or []) if isinstance(lk, dict)],
        }

    for r in _read(STATE / "labels.jsonl"):
        day, dim = str(r["day"]), str(r["dim"])
        if dim not in DIMS or not isinstance(r.get("score"), int):
            continue
        prev = out.setdefault(day, {}).get(dim) or {}
        out[day][dim] = {**prev, "score": r["score"], "from": "self"}
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None                        # 某一维一直是同一个分，相关无定义
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    return cov / (vx ** 0.5 * vy ** 0.5)


def co_movement(days: dict[str, dict[str, dict]], min_days: int) -> tuple[list[dict], str]:
    """同期相关 + lag-1（今天 X → 明天 Y）。样本不够就返回空和一句原因。"""
    ordered = sorted(days)
    usable = [d for d in ordered
              if sum(1 for x in days[d].values()
                     if isinstance(x.get("score"), int)) >= 5]
    if len(usable) < min_days:
        return [], (f"样本不足：{len(usable)} 天有分数的记录，"
                    f"统计边至少要 {min_days} 天")

    edges: list[dict] = []
    for a, b in permutations(DIMS, 2):
        xs, ys = [], []
        for d in usable:
            x, y = days[d].get(a), days[d].get(b)
            if x and y and isinstance(x.get("score"), int) \
                    and isinstance(y.get("score"), int):
                xs.append(x["score"])
                ys.append(y["score"])
        r = pearson(xs, ys)
        if r is not None and abs(r) >= 0.5 and len(xs) >= min_days:
            edges.append({"kind": "co_movement", "lag": 0, "from": a, "to": b,
                          "r": round(r, 3), "n": len(xs)})

    # lag-1：今天的 a，明天的 b
    for a, b in permutations(DIMS, 2):
        xs, ys = [], []
        for i, d in enumerate(usable[:-1]):
            nxt = usable[i + 1]
            x, y = days[d].get(a), days[nxt].get(b)
            if x and y and isinstance(x.get("score"), int) \
                    and isinstance(y.get("score"), int):
                xs.append(x["score"])
                ys.append(y["score"])
        r = pearson(xs, ys)
        if r is not None and abs(r) >= 0.5 and len(xs) >= min_days:
            edges.append({"kind": "co_movement", "lag": 1, "from": a, "to": b,
                          "r": round(r, 3), "n": len(xs)})
    return edges, ""


def build(min_days: int) -> dict:
    days = collect_scores()

    nodes = [{"day": d, "dim": dim, **{k: v for k, v in info.items()
                                       if k != "links"}}
             for d in sorted(days) for dim, info in sorted(days[d].items())]

    attributed: list[dict] = []
    for d in sorted(days):
        for dim, info in sorted(days[d].items()):
            for lk in info.get("links") or []:
                to = str(lk.get("to") or "")
                if to not in DIMS and to != "事件":
                    continue
                attributed.append({
                    "kind": "attributed",
                    "day": d,
                    "from": dim,
                    "to": to,
                    "relation": str(lk.get("relation") or "explained_by"),
                    "quote": str(lk.get("quote") or ""),
                })

    stats, why = co_movement(days, min_days)

    return {
        "generated_by": "scripts/link_graph.py",
        "days": len(days),
        "nodes": nodes,
        # ⚠️ 两种边**分开存**，永远不合并：证词 vs 统计，性质不同。
        "attributed": attributed,
        "co_movement": stats,
        "co_movement_note": why,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="标签 + 证据 → 关联图")
    ap.add_argument("--min-days", type=int, default=14,
                    help="算统计边至少需要多少天（默认 14）")
    ap.add_argument("--json", action="store_true", help="只打 JSON，不打摘要")
    args = ap.parse_args()

    graph = build(args.min_days)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(graph, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    if args.json:
        print(json.dumps(graph, ensure_ascii=False, indent=1))
        return 0

    print(f"  节点 {len(graph['nodes'])} 个（{graph['days']} 天）")
    print(f"  证词边 {len(graph['attributed'])} 条")
    for e in graph["attributed"][:12]:
        print(f"    {e['day']}  {e['from']} → {e['to']}  「{e['quote'][:34]}」")
    if len(graph["attributed"]) > 12:
        print(f"    …还有 {len(graph['attributed']) - 12} 条")

    if graph["co_movement"]:
        print(f"  统计边 {len(graph['co_movement'])} 条")
        for e in graph["co_movement"][:10]:
            print(f"    {e['from']} → {e['to']}  lag{e['lag']}  r={e['r']}  n={e['n']}")
    else:
        print(f"  统计边：{graph['co_movement_note']}")

    print(f"  → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
