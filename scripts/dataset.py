#!/usr/bin/env python3
"""把散在各处的数据拼成**一天一行**的训练集 → `state/dataset.jsonl`。

**为什么要有它**

    现在的数据是散的：

        state/features.jsonl   行为特征（屏幕/手机/位置/健康）
        state/labels.jsonl     本人自评的 1–5
        state/labels-llm.jsonl AI 补的分 + 证据 + 关联边
        state/deviations.jsonl 课表偏离

    每一份都只讲了一件事。而「今天因为身体原因在课上睡着了」这句话要能用，
    它必须能同时看到：昨天的睡眠、昨天有没有跑步、昨天的心情、今天的偏离。
    这个拼接**只能做一次、做对、放在一个地方** —— 让每个下游各自去 join，
    迟早会出现两份对不上的数据，而且没人会发现。

**`prev` 就是「昨天」**

    每一行除了今天，还带一份**上一天**的摘要（特征 + 标签 + 偏离数）。
    这就是你说的那句「你昨天心情怎么样、昨天有没有跑步」—— 它不是一个
    需要模型去记的东西，它是数据行里本来就该有的字段。

    ⚠️ `prev_gap_days` 必须留着。隔了三天的那两个「相邻」行，在时序上
       根本不是 lag-1；做统计的人要能按 gap==1 过滤掉它们，否则
       「昨天睡太晚导致今天偏离」会变成「上礼拜睡太晚导致今天偏离」。

**它不做任何推断**

    这里只做 join，不改一个数字、不补一个空值。缺就是 null。
    相关性、因果、权重，都是后面在**这一层**上跑统计的事。
    —— 因为一旦在这里补值，后面就再也分不清哪个数是观测、哪个是填充。

用法：
  python scripts/dataset.py                # 重建
  python scripts/dataset.py --summary      # 只看覆盖率和可用的 lag-1 对数
  python scripts/dataset.py --day 2026-10-23   # 看某一行长什么样
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import load_config  # noqa: E402

CFG = load_config()
STATE = Path(CFG["state_dir"])
OUT = STATE / "dataset.jsonl"

DIMS = ("体力", "心情", "睡眠", "人际", "吃饭", "休息", "学习")
DIM_KEY = {"体力": "energy", "心情": "mood", "睡眠": "sleep",
           "人际": "social", "吃饭": "food", "休息": "rest", "学习": "learn"}


def _jsonl(path: Path) -> list[dict]:
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
        if isinstance(row, dict):
            out.append(row)
    return out


def load_features() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in _jsonl(STATE / "features.jsonl"):
        day = str(r.get("day") or "")
        if day:
            out[day] = r
    return out


def load_labels() -> tuple[dict, dict, dict]:
    """(自评, AI 分, 证据)。三个都按 day 索引。"""
    mine: dict[str, dict] = {}
    for r in _jsonl(STATE / "labels.jsonl"):
        day, dim = str(r.get("day") or ""), str(r.get("dim") or "")
        key = DIM_KEY.get(dim)
        if day and key and isinstance(r.get("score"), int):
            mine.setdefault(day, {})[key] = r["score"]

    ai: dict[str, dict] = {}
    ev: dict[str, dict] = {}
    for r in _jsonl(STATE / "labels-llm.jsonl"):
        day, dim = str(r.get("day") or ""), str(r.get("dim") or "")
        key = DIM_KEY.get(dim)
        if not day or not key:
            continue
        if str(r.get("score_from") or "") == "llm" and isinstance(r.get("score"), int):
            ai.setdefault(day, {})[key] = r["score"]
        if r.get("evidence") or r.get("cause") or r.get("links"):
            ev.setdefault(day, {})[key] = {
                "evidence": str(r.get("evidence") or ""),
                "cause": str(r.get("cause") or ""),
                "confidence": str(r.get("confidence") or ""),
                "links": [lk for lk in (r.get("links") or []) if isinstance(lk, dict)],
            }
    return mine, ai, ev


def load_deviations() -> dict[str, dict]:
    """按天汇总偏离。`by_reason` 现在一定是空的 —— 理由分类还没接进飞书，
    字段先留着，接上之后这里不用改。"""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in _jsonl(STATE / "deviations.jsonl"):
        day = str(r.get("day") or "")
        if day:
            by_day[day].append(r)

    out: dict[str, dict] = {}
    for day, rows in by_day.items():
        dev = [r for r in rows if r.get("verdict") == "deviated"]
        out[day] = {
            "slots": len(rows),
            "deviated": len(dev),
            "deviated_minutes": round(sum(float(r.get("other_minutes") or 0)
                                          for r in dev), 1),
            "unobserved": sum(1 for r in rows if r.get("verdict") == "unobserved"),
            # ⚠️ 这一格在有 reason_class 之前永远是空 dict。空 dict 表示
            #    「还没标」，不是「没有偏离原因」—— 别把两者混成一个 0。
            "by_reason": dict(Counter(
                str(r["reason_class"]) for r in dev if r.get("reason_class"))),
        }
    return out


def compact(row: dict) -> dict:
    """`prev` 里放什么：只放「昨天的状态」，不放全量特征（会把文件撑大一倍）。"""
    f = row.get("features") or {}
    desk = f.get("desktop") or {}
    phone = f.get("phone") or {}
    health = f.get("health") or {}
    track = f.get("track") or {}
    labels = {**(row.get("labels") or {}), **(row.get("labels_llm") or {})}
    dev = row.get("deviations") or {}
    return {
        "desktop_hours": desk.get("hours"),
        "phone_unlocks": phone.get("unlocks"),
        "steps": health.get("steps"),
        # ⚠️ 这是 GPS 点位之间的**跨度**，不是跑量。叫 run_km 的话，
        #    以后看数据集的人会以为那天跑了 42 公里。
        "track_span_km": track.get("span_km"),
        "labels": labels,
        "deviated": dev.get("deviated"),
        "deviated_minutes": dev.get("deviated_minutes"),
    }


def build() -> list[dict]:
    feats = load_features()
    mine, ai, ev = load_labels()
    devs = load_deviations()

    days = sorted(set(feats) | set(mine) | set(ai) | set(devs))
    rows: list[dict] = []
    for day in days:
        rows.append({
            "day": day,
            "features": feats.get(day),
            "labels": mine.get(day) or {},
            "labels_llm": ai.get(day) or {},
            "evidence": ev.get(day) or {},
            "deviations": devs.get(day) or {},
        })

    # `prev` = **上一个有记录的日子**，并记下隔了几天。
    for i, row in enumerate(rows):
        if i == 0:
            row["prev"] = None
            row["prev_day"] = None
            row["prev_gap_days"] = None
            continue
        prev = rows[i - 1]
        row["prev"] = compact(prev)
        row["prev_day"] = prev["day"]
        try:
            gap = (date.fromisoformat(row["day"])
                   - date.fromisoformat(prev["day"])).days
        except ValueError:
            gap = None
        row["prev_gap_days"] = gap
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="一天的训练行（特征 + 标签 + 偏离 + 昨天）")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--day", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = build()
    if not rows:
        print("  还没有任何数据。")
        return 0

    if args.day:
        hit = next((r for r in rows if r["day"] == args.day), None)
        if hit is None:
            print(f"  没有 {args.day} 这一行")
            return 0
        print(json.dumps(hit, ensure_ascii=False, indent=1))
        return 0

    if args.summary:
        have_feat = sum(1 for r in rows if r.get("features"))
        have_lab = sum(1 for r in rows if r.get("labels") or r.get("labels_llm"))
        have_dev = sum(1 for r in rows if (r.get("deviations") or {}).get("slots"))
        lag1 = sum(1 for r in rows
                   if r.get("prev_gap_days") == 1
                   and (r.get("labels") or r.get("labels_llm")))
        print(f"  行数 {len(rows)}（{rows[0]['day']} → {rows[-1]['day']}）")
        print(f"  有特征   {have_feat}")
        print(f"  有标签   {have_lab}")
        print(f"  有偏离   {have_dev}")
        print(f"  严格 lag-1 且有标签的行：{lag1}")
        print()
        print("  ⚠️ lag-1 少于 14 条时，任何「昨天 X 导致今天 Y」的结论都是噪音。")
        return 0

    if args.dry_run:
        print(f"  --dry-run：{len(rows)} 行，不写盘")
        return 0

    STATE.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    tmp.replace(OUT)
    print(f"  OK  {len(rows)} 行 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
