#!/usr/bin/env python3
"""把日特征向量两两做相关，找出「什么和什么一起变」—— 带 n、效应量、置信区间。

**为什么要有它**

    这套系统花了很多力气做标注：每天问七维、把自由文本打成 1–5 分。
    但标签进来之后，除了喂给日记提示词，**没有任何东西在找规律**。
    那就变成：人付了摩擦成本，换回一堆没人读的数字。

    这一步是缺的那一环。它只做**一件事**：在已有的日特征上算相关性，
    然后诚实地把不确定性一起印出来。

**它故意不做什么**

    · 不下因果结论。相关就是相关，`r` 再大也只说明「一起变」。
    · 不挑好看的。所有候选对都测，再用 Benjamini–Hochberg 做多重比较校正，
      没通过 FDR 的**不报**。35 天 × 300 个对，不做校正必然能挑出一堆
      「显著」的假发现 —— 那正是这类系统最容易骗自己的地方。
    · 不补数据。样本不够就明说不够，不给一个看起来像结论的数字。

**一个必须写在输出里的统计缺陷**

    日频数据是**自相关**的：今天的心情和昨天的心情相关，所以「35 个独立样本」
    这个前提并不成立，真实有效自由度更小。这里的 p/q 是在**独立样本**假设下算的，
    因此**偏乐观**。

    所以 q 值只用来**排序**（先看哪个），不用来**证实**。要真正确认一个关系，
    得看它在不同时间窗口里是否稳定 —— 那是 `--split` 做的事。

用法：
  python scripts/patterns.py                  # 全窗口
  python scripts/patterns.py --min-n 21       # 至少三周才报
  python scripts/patterns.py --split          # 前后两半分别算，看结论稳不稳
  python scripts/patterns.py --json           # 只输出 JSON
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import header, load_config  # noqa: E402

CFG = load_config()
STATE = Path(CFG["state_dir"])
FEATURES = STATE / "features.jsonl"
OUT_JSON = STATE / "patterns.json"

# 少于两周不报。十个点算出来的相关系数，置信区间宽到没有信息量。
MIN_N_DEFAULT = 14

# FDR 的报出门槛。0.10 已经比较宽松 —— 日频数据本来就噪声大。
FDR_ALPHA = 0.10


# ── 特征表 ──────────────────────────────────────────────────────────
#
# 只收**数值**。`top_app`、`main_place` 这类字符串不进相关分析 ——
# 它们是分类变量，硬编码成数字会造出一个不存在的顺序。

def _get(path: tuple[str, ...]) -> Callable[[dict], Any]:
    def pick(row: dict) -> Any:
        cur: Any = row
        for k in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
            if cur is None:
                return None
        return cur
    return pick


FEATURE_SPECS: list[tuple[str, str, Callable[[dict], Any]]] = [
    # 桌面
    ("desktop_hours",        "电脑时长",        _get(("desktop", "hours"))),
    ("desktop_blocks",       "时间块数",        _get(("desktop", "blocks"))),
    ("desktop_longest_h",    "最长连续块",      _get(("desktop", "longest_block_hours"))),
    ("desktop_late_h",       "深夜时长",        _get(("desktop", "late_hours"))),
    ("cat_work",             "Work 时长",       _get(("desktop", "by_category", "Work"))),
    ("cat_study",            "Study 时长",      _get(("desktop", "by_category", "Study"))),
    ("cat_leisure",          "Leisure 时长",    _get(("desktop", "by_category", "Leisure"))),
    ("cat_life",             "Life 时长",       _get(("desktop", "by_category", "Life"))),
    # 手机
    ("phone_screen_h",       "手机亮屏时长",     _get(("phone", "screen_hours"))),
    ("phone_fg_h",           "手机前台时长",     _get(("phone", "foreground_hours"))),
    ("phone_unlocks",        "解锁次数",        _get(("phone", "unlocks"))),
    ("phone_switches",       "应用切换",        _get(("phone", "switches"))),
    ("phone_leisure_min",    "手机娱乐时长",     _get(("phone", "leisure_top10_minutes"))),
    # 位置
    ("loc_stays",            "停留点数",        _get(("location", "stays"))),
    ("loc_distinct",         "去过的区数",       _get(("location", "distinct_places"))),
    ("loc_stayed_h",         "在外时长",        _get(("location", "stayed_hours"))),
    # 健康
    ("steps",                "步数",            _get(("health", "steps"))),
    ("hr_avg",               "平均心率",        _get(("health", "heart_rate_avg"))),
    ("hr_min",               "最低心率",        _get(("health", "heart_rate_min"))),
    ("hr_max",               "最高心率",        _get(("health", "heart_rate_max"))),
    ("sleep_h",              "睡眠时长",        _get(("health", "sleep_window"))),
    ("sleep_onset",          "入睡时刻",        _get(("health", "sleep_onset_min"))),
    ("sleep_mid",            "睡眠中点",        _get(("health", "sleep_midpoint_min"))),
    # 标签（自评 + LLM 补分）
    ("mood",                 "心情",            _get(("labels", "mood"))),
    ("energy",               "体力",            _get(("labels", "energy"))),
    ("self_sleep",           "睡眠(自评)",      _get(("labels", "sleep"))),
    ("social",               "人际",            _get(("labels_llm", "social"))),
    ("rest",                 "休息",            _get(("labels_llm", "rest"))),
    ("food",                 "吃饭",            _get(("labels_llm", "food"))),
    ("learn",                "学习",            _get(("labels_llm", "learn"))),
]

LABEL = {k: name for k, name, _ in FEATURE_SPECS}

# 每个特征属于哪个**数据来源**。同一个来源里的两个数字几乎必然是
# 同一件事的两种写法，测出来 r=0.8 只是定义使然，不是行为发现：
#
#   电脑时长 ↔ 时间块数        同一次使用，块多时长自然长
#   停留点数 ↔ 去过的区数      停留点本身就是区数的原料
#   平均心率 ↔ 最高心率        同一天同一台设备的两个分位数
#
# 所以**只测跨来源**的配对。真正的行为问题几乎都长这样：
# 睡得少 → 第二天手机用得多？ 出门多 → 心情好？ 工作长 → 睡得晚？
DOMAIN = {
    "desktop_hours": "desktop",
    "desktop_blocks": "desktop",
    "desktop_longest_h": "desktop",
    "desktop_late_h": "desktop",
    "cat_work": "desktop",
    "cat_study": "desktop",
    "cat_leisure": "desktop",
    "cat_life": "desktop",
    "phone_screen_h": "phone",
    "phone_fg_h": "phone",
    "phone_unlocks": "phone",
    "phone_switches": "phone",
    "phone_leisure_min": "phone",
    "loc_stays": "location",
    "loc_distinct": "location",
    "loc_stayed_h": "location",
    "steps": "health",
    "hr_avg": "health",
    "hr_min": "health",
    "hr_max": "health",
    "sleep_h": "health",
    "sleep_onset": "health",
    "sleep_mid": "health",
    # 自评和 LLM 补分放在**同一个** domain 里：两者都来自你写的那段话，
    # 互相之间的相关有一半是文本本身的循环性，不该算行为发现。
    "mood": "labels",
    "energy": "labels",
    "self_sleep": "labels",
    "social": "labels",
    "rest": "labels",
    "food": "labels",
    "learn": "labels",
}

# 这些对**没有可解释的方向**，测了只会占 FDR 名额。
# 例：`Study 时长 ↔ 手机娱乐时长` 可以看，但 `入睡时刻 ↔ 睡眠中点`
# 是同一件事的两种写法 —— 它们必然强相关，属于**定义性重复**，不是发现。
TRIVIAL_PAIRS = {
    frozenset(("sleep_h", "sleep_mid")),
    frozenset(("sleep_onset", "sleep_mid")),
    frozenset(("phone_screen_h", "phone_fg_h")),
}


# ── 统计 ────────────────────────────────────────────────────────────


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 1e-12 or syy <= 1e-12:
        return None                    # 常数序列，没有相关性可言
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _rank(xs: list[float]) -> list[float]:
    """平均秩（并列取平均）。Spearman 用它避开线性假设。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _fisher_ci(r: float, n: int) -> tuple[float, float] | None:
    """Fisher z 变换的 95% 置信区间。n 太小就没有意义。"""
    if n <= 3 or abs(r) >= 1.0:
        return None
    z = math.atanh(r)
    se = 1.0 / math.sqrt(n - 3)
    lo, hi = z - 1.96 * se, z + 1.96 * se
    return (math.tanh(lo), math.tanh(hi))


def _p_value(r: float, n: int) -> float | None:
    """Fisher z 检验的双尾 p。"""
    if n <= 3 or abs(r) >= 1.0:
        return None
    z = abs(math.atanh(r)) * math.sqrt(n - 3)
    # 正态尾概率：P(Z > z) = 0.5 * erfc(z / sqrt(2))
    return math.erfc(z / math.sqrt(2.0))


def _benjamini_hochberg(pvals: list[float]) -> list[float]:
    """返回与输入同序的 q 值。控制的是**假发现率**，不是单次犯错率。"""
    m = len(pvals)
    if not m:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        val = min(prev, pvals[i] * m / rank)
        q[i] = val
        prev = val
    return q


# ── 数据 ────────────────────────────────────────────────────────────


def load_table() -> list[dict[str, Any]]:
    """features.jsonl → 按日期排序的扁平表。"""
    if not FEATURES.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in FEATURES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict) or not raw.get("day"):
            continue
        flat: dict[str, Any] = {"day": raw["day"]}
        for key, _name, pick in FEATURE_SPECS:
            v = pick(raw)
            flat[key] = float(v) if isinstance(v, (int, float)) else None
        rows.append(flat)
    rows.sort(key=lambda r: r["day"])
    return rows


def pairs_for(rows: list[dict], a: str, b: str, lag: int) -> tuple[list[float], list[float], int]:
    """取 (a 第 d 天, b 第 d+lag 天) 的成对样本。缺任一端就丢那天。"""
    xs: list[float] = []
    ys: list[float] = []
    for i in range(len(rows) - lag):
        x = rows[i].get(a)
        y = rows[i + lag].get(b)
        if x is None or y is None:
            continue
        xs.append(float(x))
        ys.append(float(y))
    return xs, ys, len(xs)


def analyse(rows: list[dict], min_n: int) -> dict[str, Any]:
    """跑完所有候选对，返回带 FDR 的结果。"""
    tested: list[dict[str, Any]] = []

    keys = [k for k, _n, _p in FEATURE_SPECS]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if frozenset((a, b)) in TRIVIAL_PAIRS:
                continue
            # 同一来源内部的相关几乎都是构造性的，见 DOMAIN 的说明
            if DOMAIN.get(a) == DOMAIN.get(b):
                continue
            for lag, tag in ((0, "同日"), (1, "次日")):
                xs, ys, n = pairs_for(rows, a, b, lag)
                if n < min_n:
                    continue
                if len(set(xs)) < 3 or len(set(ys)) < 3:
                    continue
                r = _pearson(xs, ys)
                if r is None:
                    continue
                p = _p_value(r, n)
                if p is None:
                    continue
                tested.append({
                    "a": a, "b": b, "lag": lag, "lag_label": tag,
                    "n": n, "r": r, "p": p,
                    "spearman": _pearson(_rank(xs), _rank(ys)),
                    "ci": _fisher_ci(r, n),
                })

    qs = _benjamini_hochberg([t["p"] for t in tested])
    for t, q in zip(tested, qs):
        t["q"] = q

    # 只留过 FDR 的；按 |r| 排序（q 相同的情况下，效应量大的先看）
    hits = [t for t in tested if t["q"] <= FDR_ALPHA]
    hits.sort(key=lambda t: (-abs(t["r"]), t["q"]))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": (rows[0]["day"], rows[-1]["day"]) if rows else None,
        "days": len(rows),
        "tests": len(tested),
        "min_n": min_n,
        "fdr_alpha": FDR_ALPHA,
        "findings": hits,
        "all": tested,
    }


# ── 输出 ────────────────────────────────────────────────────────────


def _fmt(t: dict) -> str:
    a, b = LABEL.get(t["a"], t["a"]), LABEL.get(t["b"], t["b"])
    r = t["r"]
    ci = t.get("ci")
    ci_s = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "[—]"
    arrow = "→" if t["lag"] else "↔"
    return (
        f"  n={t['n']:<3} r={r:+.2f} {ci_s}  q={t['q']:.3f}  "
        f"{a} {arrow}{'次日' if t['lag'] else '同日'} {b}"
    )


def render(res: dict, top: int = 15) -> str:
    lines: list[str] = []
    w = res.get("window")
    lines.append(f"窗口 {w[0]} → {w[1]}（{res['days']} 天）"
                 if w else "没有可用的特征行")
    lines.append(f"测试了 {res['tests']} 对，最小样本 {res['min_n']}，"
                 f"FDR α={res['fdr_alpha']}")
    lines.append("")
    hits = res["findings"]
    if not hits:
        lines.append("没有通过 FDR 的关联。")
        lines.append("")
        lines.append("这**不是**坏消息 —— 35 天、三百个候选对，没校正地挑总能挑出")
        lines.append("几个「显著」的。一个都没过，说明现在的数据量还撑不起结论。")
    else:
        lines.append(f"通过 FDR 的关联：{len(hits)} 条（按效应量排序）")
        lines.append("")
        for t in hits[:top]:
            lines.append(_fmt(t))
    lines.append("")
    lines.append("读法：")
    lines.append("  · r 是皮尔逊相关系数，CI 是 95% 置信区间 —— 区间跨 0 就当没结论")
    lines.append("  · q 是多重比较校正后的假发现率，**只用来排序**")
    lines.append("  · 日频数据自相关，独立样本假设不成立，q 偏乐观")
    lines.append("  · 相关不是因果。要确认方向，看同一对在前后两半窗口里是否都成立")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="日特征两两相关，带 FDR 校正")
    ap.add_argument("--min-n", type=int, default=MIN_N_DEFAULT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--split", action="store_true",
                    help="前后两半分别算，标注哪些结论只在半边成立")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    rows = load_table()
    if not rows:
        print(f"没有特征数据（{FEATURES}），先跑 scripts/features.py")
        return 1

    header(f"Patterns · {len(rows)} 天")
    res = analyse(rows, args.min_n)

    if args.split and len(rows) >= 2 * max(args.min_n, 4):
        mid = len(rows) // 2
        halves = {
            "first": analyse(rows[:mid], args.min_n),
            "second": analyse(rows[mid:], args.min_n),
        }
        # 只在两半都出现的对，标成「sticky」；只在一半出现的标成「half」
        def keyset(r):
            return {(t["a"], t["b"], t["lag"]) for t in r["findings"]}
        k1, k2 = keyset(halves["first"]), keyset(halves["second"])
        res["split"] = {
            "stable": sorted(k1 & k2),
            "first_only": sorted(k1 - k2),
            "second_only": sorted(k2 - k1),
            "halves": halves,
        }

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(render(res, args.top))
        if args.split and "split" in res:
            s = res["split"]
            print()
            print(f"前后半窗稳定性：两半都成立 {len(s['stable'])} 条 · "
                  f"只在后半 {len(s['second_only'])} 条 · 只在前半 {len(s['first_only'])} 条")
            if s["stable"]:
                print("  两半都成立（更值得信）：")
                for a, b, lag in s["stable"][:10]:
                    print(f"    {LABEL.get(a,a)} {'→次日' if lag else '↔'} {LABEL.get(b,b)}")

    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    if not args.json:
        print(f"\nOK  写入 {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
