#!/usr/bin/env python3
"""把飞书里的自评答案变成**机器可读**的标签 —— `state/labels.jsonl`。

**为什么要有它**

    心情和体力每天都被问，但答案一直只是自由文本（「还行」「有点累」）。
    自由文本能进日记，进不了任何统计 —— 想回答「微信超过 3 小时的那几天，
    第二天体力是不是更低」，先得有数字。

    所以 `live_questions.py` 把这两维改成 1–5 打分（备注仍然可以写），
    这个脚本把 `state/qa-<day>.json` 里的分数抽出来，落成一行一个标签。

**不猜**

    · 只认带 `dim`（心情/体力）**且**答案以 1–5 开头的题；
    · 旧数据没有 dim、或者答的是自由文本 → **跳过**，不按题面去猜是哪一维，
      更不把「还行」估成 3 分。猜出来的标签比没有标签更坏：它会被当成
      真的观测值喂进以后的模型。

**幂等**

    每次全量重扫 `qa-*.json`、整体重写 `labels.jsonl`（按 qid 去重，
    同一题重答取最后一次）。不做增量 —— 增量要维护「哪些变了」，
    那个状态一旦和现实对不上，标签就会静默缺一半。

用法：
  python scripts/collect_labels.py
  python scripts/collect_labels.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import load_config  # noqa: E402

CFG = load_config()
STATE = Path(CFG["state_dir"])
OUT = STATE / "labels.jsonl"

# ⚠️ 必须和 live_questions.RATED_DIMENSIONS 保持一致。多一个这里没认的维度
#    只是少一类标签；反过来（这里认了、那边没打分）会匹配到自由文本，然后
#    被 score_of 挡掉 —— 两种失败都**不会**产出错标签，这是有意的。
RATED_DIMS = ("心情", "体力", "睡眠")

# ⚠️ 个别题目的「归属日」需要纠正时放这里（qid → 正确的那一天）。
#
#    为什么需要有这个文件：答案是按 `question_day`（题号前缀里的日期）归类的，
#    而题号是「**生成题目的那天**」。2026-10-23 00:01 按了一次 /收尾，题是
#    给 10-23 生成的，人答的却是刚过完的 10-22 —— 那 10 条答案于是被记在
#    10-23 名下。这不是 bug 报告，是**已经发生的历史数据**：改代码不能让
#    旧数据自己走回去。
#
#    所以这里做一次显式纠正：qid → 真实归属日。文件是 `state/label-day-fixups.json`，
#    不在代码里硬编码日期 —— 这是数据修正，不是逻辑。
FIXUPS = STATE / "label-day-fixups.json"


def load_fixups() -> dict[str, str]:
    try:
        raw = json.loads(FIXUPS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

# 答案形如「4 不错」或「4分，下午开会」——取开头的数字。
# ⚠️ 不匹配「十分」「4.5」这种：它们不是这套量表产出的，宁可当没有。
_SCORE_RE = re.compile(r"^\s*([1-5])(?=\s|$|[分。，,、:：])")


def score_of(answer: str) -> int | None:
    m = _SCORE_RE.match(str(answer or ""))
    return int(m.group(1)) if m else None


def extract(payload: dict, fixups: dict[str, str] | None = None) -> list[dict]:
    """从一个 `qa-<day>.json` 里抽出可用的标签。

    ⚠️ 纯函数（不碰文件、不碰网络），smoke_test 直接喂假 payload 测。
    """
    out: list[dict] = []
    for x in payload.get("qa") or []:
        if not isinstance(x, dict):
            continue
        dim = str(x.get("dim") or "").strip()
        if dim not in RATED_DIMS:
            continue
        score = score_of(x.get("answer") or "")
        if score is None:
            # 自由文本 / 没按格式答 —— 不猜。
            continue
        qid = str(x.get("id") or "")
        day = (fixups or {}).get(qid) or str(x.get("question_day") or qid[:10])
        if not qid or not day:
            continue
        out.append({
            "day": day,
            "dim": dim,
            "score": score,
            "qid": qid,
            "ts_utc": str(x.get("ts_utc") or ""),
            "answer": str(x.get("answer") or ""),
            "note": str(x.get("note") or ""),
        })
    return out


def collect() -> list[dict]:
    """扫全部 qa 文件，先按 qid 去重，再按 (day, dim) 去重。

    ⚠️ 第二层去重**不是洁癖**。同一维度一天可能被问两次：

      · 收尾那十道里问一次，轻量打卡又问了同样的三个维度；
      · 或者收尾答完又补答了一次。

    两条 qid 不同，但它们是**同一天的同一个变量**。两条都留着的话，下游
    按 ts 挑一条纯粹是碰运气，而直接读 labels.jsonl 的东西会看到两个互相
    矛盾的值。取 ts 最大的那条：晚答的更接近事实。
    """
    fixups = load_fixups()
    by_qid: dict[str, dict] = {}
    for p in sorted(STATE.glob("qa-*.json")):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ⚠️  {p.name} 读不了，跳过：{type(e).__name__}: {e}",
                  file=sys.stderr)
            continue
        if not isinstance(payload, dict):
            continue
        for row in extract(payload, fixups):
            old = by_qid.get(row["qid"])
            if old is None or row["ts_utc"] >= old["ts_utc"]:
                by_qid[row["qid"]] = row

    by_slot: dict[tuple[str, str], dict] = {}
    for row in by_qid.values():
        k = (row["day"], row["dim"])
        old = by_slot.get(k)
        if old is None or row["ts_utc"] >= old["ts_utc"]:
            by_slot[k] = row
    return sorted(by_slot.values(), key=lambda r: (r["day"], r["dim"]))


def write(rows: list[dict]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")
    tmp.replace(OUT)


def main() -> int:
    ap = argparse.ArgumentParser(description="自评答案 → 数值标签")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = collect()
    if not rows:
        print("  还没有可用的自评标签（要么还没答过打分题，要么中继还没带 dim）。")
        return 0

    if not args.dry_run:
        write(rows)
        print(f"  OK  {len(rows)} 条标签 → {OUT}")
    else:
        print(f"  --dry-run：{len(rows)} 条，不写盘")

    by_dim: dict[str, int] = {}
    days: set[str] = set()
    for r in rows:
        by_dim[r["dim"]] = by_dim.get(r["dim"], 0) + 1
        days.add(r["day"])
    print("  " + " · ".join(f"{k} {v} 天" for k, v in sorted(by_dim.items())))

    # 有 dim 的题每天都有，缺哪一维说明那天没答 —— 但不报错：答不答是他的事。
    missing = sorted(d for d in days
                     if {r["dim"] for r in rows if r["day"] == d} != set(RATED_DIMS))
    if missing:
        print(f"  . 自评不全的日子：{', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
