#!/usr/bin/env python3
"""个人基线：今天和你自己的常态差多少 —— 不用模型，只比中位数。

**为什么不是预测**

    预测要标签、要样本、要验证，现在都还不够。但「今天亮屏 9.9 小时，
    而过去 14 天的中位数是 6.1 小时」这句话，**不需要任何模型**就已经
    能让人停下来看一眼。它也不会说「因为你熬夜了」—— 只给偏离，不给原因。

**为什么是中位数 / MAD，不是均值 / 标准差**

    一天熬到凌晨 4 点就能把均值拉歪。中位数和 MAD（中位数绝对偏差）
    对离群点不敏感，而个人数据里**每天都是离群点**。

**样本不足就闭嘴**

    少于 `--min-days` 个历史值，直接说「样本不足」，不硬算。
    过去几天完全没波动（MAD=0）时也不比 —— 没有离散度就没有「反常」。
    这和机器人里「答不了比答错好」是同一条原则。

用法：
  python scripts/baseline.py                  # 比最近一天
  python scripts/baseline.py --date 2026-10-20
  python scripts/baseline.py --all            # 连没到阈值的也列出来
  python scripts/baseline.py --json           # 给别的脚本吃
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import load_config  # noqa: E402

CFG = load_config()
STATE = Path(CFG["state_dir"])
FEATURES = STATE / "features.jsonl"

DEFAULT_WINDOW = 14
DEFAULT_MIN_DAYS = 5
DEFAULT_Z = 2.0


def _get(row: dict, *path: str) -> Any:
    cur: Any = row
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# (key, 显示名, 单位, 取值函数)
METRICS: list[tuple[str, str, str, Callable[[dict], Any]]] = [
    ("phone_screen_hours", "手机亮屏", "h", lambda r: _get(r, "phone", "screen_hours")),
    ("phone_unlocks", "手机解锁", "次", lambda r: _get(r, "phone", "unlocks")),
    ("phone_switches", "应用切换", "次", lambda r: _get(r, "phone", "switches")),
    ("phone_leisure_top10", "娱乐应用（top10）", "min",
     lambda r: _get(r, "phone", "leisure_top10_minutes")),
    ("desktop_hours", "电脑合计", "h", lambda r: _get(r, "desktop", "hours")),
    ("desktop_late_hours", "深夜电脑", "h", lambda r: _get(r, "desktop", "late_hours")),
    ("desktop_longest_block", "最长连续块", "h",
     lambda r: _get(r, "desktop", "longest_block_hours")),
    ("location_stays", "位置停留", "处", lambda r: _get(r, "location", "stays")),
    ("location_stayed_hours", "停留合计", "h",
     lambda r: _get(r, "location", "stayed_hours")),
    ("web_minutes", "网页", "min", lambda r: _get(r, "web", "minutes")),
    ("health_steps", "步数", "步", lambda r: _get(r, "health", "steps")),
    ("health_hr_avg", "平均心率", "bpm", lambda r: _get(r, "health", "heart_rate_avg")),
    ("track_span_km", "移动距离", "km", lambda r: _get(r, "track", "span_km")),
    ("label_mood", "心情", "分", lambda r: _get(r, "labels", "mood")),
    ("label_energy", "体力", "分", lambda r: _get(r, "labels", "energy")),
]


def median_mad(values: list[float]) -> tuple[float, float]:
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return med, mad


def robust_z(x: float, med: float, mad: float) -> float:
    """中位数绝对偏差 → 标准分的常用换算（0.6745 是正态下的比例）。"""
    return 0.6745 * (x - med) / mad


def deviations(rows: list[dict], target_day: str, *, window: int = DEFAULT_WINDOW,
               min_days: int = DEFAULT_MIN_DAYS,
               z_threshold: float = DEFAULT_Z) -> dict:
    """返回 {target, baseline_days, checked, items, flagged, insufficient, no_variance}。

    ⚠️ 纯函数：不读文件、不打印。smoke_test 直接喂假行。
    """
    ordered = sorted((r for r in rows if isinstance(r, dict) and r.get("day")),
                     key=lambda r: r["day"])
    idx = next((i for i, r in enumerate(ordered) if r["day"] == target_day), None)
    if idx is None:
        return {"target": target_day, "baseline_days": 0, "checked": 0,
                "items": [], "flagged": [], "insufficient": [], "no_variance": [],
                "error": f"features.jsonl 里没有 {target_day}"}

    today = ordered[idx]
    prior = ordered[max(0, idx - window):idx]
    items: list[dict] = []
    insufficient: list[str] = []
    no_variance: list[str] = []

    for key, label, unit, getter in METRICS:
        x = _num(getter(today))
        if x is None:
            continue
        vals = [v for v in (_num(getter(r)) for r in prior) if v is not None]
        if len(vals) < min_days:
            insufficient.append(label)
            continue
        med, mad = median_mad(vals)
        if mad == 0:
            # 每天都一样 → 没有离散度，「反常」无从谈起。
            no_variance.append(label)
            continue
        z = robust_z(x, med, mad)
        items.append({
            "key": key, "label": label, "unit": unit,
            "value": round(x, 2), "median": round(med, 2),
            "mad": round(mad, 2), "z": round(z, 2),
            "n": len(vals),
        })

    flagged = [d for d in items if abs(d["z"]) >= z_threshold]
    flagged.sort(key=lambda d: -abs(d["z"]))
    return {
        "target": target_day,
        "baseline_days": len(prior),
        "checked": len(items),
        "items": items,
        "flagged": flagged,
        "insufficient": insufficient,
        "no_variance": no_variance,
    }


def load_features() -> list[dict]:
    try:
        lines = FEATURES.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def render(result: dict, *, show_all: bool) -> str:
    if result.get("error"):
        return f"  ⚠️  {result['error']}"
    lines = [f"  {result['target']} vs 之前 {result['baseline_days']} 天"
             f"（可比指标 {result['checked']} 项）"]

    if result["flagged"]:
        for d in result["flagged"]:
            direction = "高于" if d["z"] > 0 else "低于"
            lines.append(
                f"  · {d['label']:<14} {d['value']} {d['unit']}，"
                f"{direction}过去 {d['n']} 天的中位数 {d['median']} {d['unit']}"
                f"（偏离 {d['z']:+.1f} MAD）")
    elif not show_all:
        lines.append("  . 今天没有明显偏离")
    else:
        lines.append("  . 没有超过阈值的项")

    if show_all:
        # 把没到阈值的也列出来，人自己判断 —— 「没超阈值」不等于「正常」。
        shown = {d["key"] for d in result["flagged"]}
        for d in result.get("items") or []:
            if d["key"] in shown:
                continue
            lines.append(f"    - {d['label']}：{d['value']} {d['unit']}，"
                         f"偏离 {d['z']:+.1f} MAD")
        if result["insufficient"]:
            lines.append("  . 样本不足（不硬算）：" + "、".join(result["insufficient"]))
        if result["no_variance"]:
            lines.append("  . 过去没有波动（MAD=0）：" + "、".join(result["no_variance"]))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="今天和你自己的常态比")
    ap.add_argument("--date", default="", help="YYYY-MM-DD；默认 features 里最新一天")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS)
    ap.add_argument("--z", type=float, default=DEFAULT_Z)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load_features()
    if not rows:
        print("  还没有 features.jsonl —— 先跑 scripts/features.py")
        return 0

    target = args.date or max(r["day"] for r in rows)
    result = deviations(rows, target, window=args.window,
                        min_days=args.min_days, z_threshold=args.z)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(render(result, show_all=args.all))
    return 0


if __name__ == "__main__":
    sys.exit(main())
