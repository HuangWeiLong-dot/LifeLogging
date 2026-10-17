#!/usr/bin/env python3
"""学着认出新应用 —— 把 `Other / default` 逐个补进分类表。

**它做什么**

    扫最近几天的 `timeline-*.json`，找出「时间花了不少、但规则表没认出来」的
    应用（`unmatched_apps`），拿**应用名 + 它出现过的窗口标题**问一次模型，
    把答案存进 `state/app_map.json`（见 `app_map.py`）。

    之后 `normalize.classify()` 会像查手写规则一样查它 —— 免费、确定、可审计。

**为什么是「学一次」而不是「每次都问」**

    见 `app_map.py` 开头的说明。一句话：一天上千个切片，逐个问模型既贵又
    不确定，而「应用 → 分类」这个映射本身是稳定的。

**三条约束**

  · **只学累积够久的**（`--min-minutes`，默认 10 分钟）。一闪而过的进程不值得
    花一次调用，而且标题样本太少容易判错。
  · **一次运行最多学 N 个**（`--max`，默认 8）—— 和 `budget.max_llm_calls_per_run`
    对齐，防止某天冒出一百个未知应用把额度打穿。
  · **模型说 Other 就不存**。存了等于「以后不再学它」，而它可能只是那天标题
    看不出线索。留在队列里，等下次有更好的标题样本再学。

用法（在笔记本上，daily 流水线里自动跑，也可以手动）：
  python scripts/learn_apps.py                # 学最近 7 天里没认出来的
  python scripts/learn_apps.py --dry-run      # 只列候选，不调模型
  python scripts/learn_apps.py --days 30 --max 3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app_map  # noqa: E402
from common import (  # noqa: E402
    BudgetExceeded,
    CostLedger,
    LLM,
    header,
    load_config,
    new_run_id,
)
from normalize import classify  # noqa: E402

CFG = load_config()

CATEGORIES = ("Work", "Study", "Leisure", "Life",
              "Health", "Transport", "Social", "Other")

_SYSTEM = """你在给一个个人时间记录系统分类。

给你一个进程名，以及它出现过的窗口标题。判断这个应用主要用于什么。
只回**一个词**，从这些里选：
Work / Study / Leisure / Life / Health / Transport / Social / Other

判据：
- Work：开发、办公、会议、写作、后台管理、数据库/终端
- Study：课程、论文、文档、阅读、刷题、语言学习
- Leisure：游戏、影视、音乐、短视频、社交娱乐
- Life：购物、银行、生活服务、文件管理、系统设置
- Health：运动、健康、医疗
- Transport：地图、导航、出行购票
- Social：即时通讯、社交网络
- Other：实在看不出来

拿不准就回 Other —— 宁可让它继续待在学习队列里，也不要猜一个错的然后
永久生效（它会一直被当成规则用，而且没人会再检查）。"""


def collect(days: int) -> dict[str, dict]:
    """扫最近几天的 timeline，汇总「没认出来的应用」。

    返回 `{app: {"seconds": float, "titles": [...]}}`。

    ⚠️ 数据源是 **timeline JSON**（笔记本自己的产物），不是事件表：
       那里已经算好了 `unmatched_apps`，不用再跑一遍分类。
    """
    state = Path(CFG["state_dir"])
    out: dict[str, dict] = {}
    for p in sorted(state.glob("timeline-*.json"))[-max(1, days):]:
        try:
            tl = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for app, secs in (tl.get("unmatched_apps") or {}).items():
            app = str(app or "").strip()
            if not app:
                continue
            e = out.setdefault(app, {"seconds": 0.0, "titles": []})
            try:
                e["seconds"] += float(secs or 0)
            except (TypeError, ValueError):
                pass
        # 标题样本：从时间块里捞（同一应用、按出现顺序）
        for b in (tl.get("blocks") or []):
            app = str(b.get("app") or "").strip()
            e = out.get(app)
            if e is None:
                continue
            for t in (b.get("titles") or []):
                t = " ".join(str(t or "").split())
                if t and t not in e["titles"]:
                    e["titles"].append(t)
    return out


def parse_category(raw: str) -> str | None:
    """从模型输出里取出一个分类词。取不到返回 None（调用方跳过）。"""
    low = (raw or "").lower()
    for c in CATEGORIES:
        if c.lower() in low:
            return c
    return None


def build_prompt(app: str, titles: list[str]) -> str:
    lines = [f"进程名: {app}", "", "窗口标题（按停留时间排序，最多 8 条）:"]
    if titles:
        for t in titles[:8]:
            lines.append(f"- {t[:100]}")
    else:
        lines.append("- （没有标题样本）")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="学着认出新应用")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-minutes", type=float, default=10.0)
    ap.add_argument("--max", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    header("Learn apps")
    found = collect(args.days)
    print(f"  最近 {args.days} 天里没认出来的应用：{len(found)} 个")

    todo: list[tuple[str, dict, float]] = []
    for app, e in found.items():
        mins = e["seconds"] / 60.0
        if mins < args.min_minutes:
            continue
        if app_map.lookup(app):
            continue                     # 已经学过
        # 手写规则要是能认出来（比如标题里带了关键词），就不该学它 ——
        # 那会把它钉死成一个分类，反而盖掉按标题细分的可能。
        if classify(app, " ".join(e["titles"]))[1] != "default":
            continue
        todo.append((app, e, mins))

    todo.sort(key=lambda x: -x[2])
    todo = todo[:max(0, args.max)]

    if not todo:
        print("  . 没有值得学的（要么都认出来了，要么都不到 "
              f"{args.min_minutes:g} 分钟）")
        return 0

    print(f"  候选（≥{args.min_minutes:g} 分钟，最多学 {args.max} 个）：")
    for app, e, mins in todo:
        print(f"    {app}  {mins:.0f} 分钟  标题样本 {len(e['titles'])} 条")

    if args.dry_run:
        print("\n--dry-run，不调模型、不落盘。")
        return 0

    ledger = CostLedger(CFG["budget"]["ledger_path"])
    # ⚠️ 整个运行只用**一个** LLM 实例：它的 `max_llm_calls_per_run` 才有意义。
    #    （常驻机器人那边相反 —— 那个要每次新建，见 feishu-bot/live.py。）
    llm = LLM(cfg=CFG, ledger=ledger)
    run_id = new_run_id()
    learned = 0

    for app, e, mins in todo:
        try:
            out = llm.complete(system=_SYSTEM,
                               user=build_prompt(app, e["titles"]),
                               stage="learn_app", run_id=run_id)
        except BudgetExceeded as e2:
            print(f"  ⚠️  预算到顶，停止学习：{e2}")
            break
        except Exception as e2:  # noqa: BLE001
            print(f"  ⚠️  {app} 判定失败：{type(e2).__name__}: {e2}")
            continue

        cat = parse_category(out)
        if cat is None:
            print(f"  ?  {app}: 模型没给出可识别的分类（{out.strip()[:40]!r}），跳过")
            continue
        if cat == "Other":
            # 不存 Other —— 存了就再也不会学它了，而它可能只是这次标题没线索。
            print(f"  .  {app}: 判为 Other，留在队列里等下次")
            continue

        if app_map.remember(app, cat, sample_titles=e["titles"],
                            minutes_seen=mins):
            learned += 1
            print(f"  ✓  {app} → {cat}（{mins:.0f} 分钟，"
                  f"标题: {(e['titles'][0] if e['titles'] else '无')[:40]}）")

    print(f"  OK  学到 {learned} 个新应用 → {app_map.map_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
