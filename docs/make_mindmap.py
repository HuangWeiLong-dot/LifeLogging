#!/usr/bin/env python3
"""生成 LLS 项目的思维导图源文件 → `docs/lls-mindmap.md`。

**为什么是「生成」而不是「手画」**

    图上的数字（天数、条数、判定分布）每天都在变。手画的图三天就过时，
    而过时的项目图比没有更误导 —— 它会让人以为某个模块还在用。
    所以树在这里用代码写一次，数字从 `state/` 现扫。

**渲染**

    这个脚本只产 Markdown（markmap 的输入）。渲染交给 skill：

        npx skills add LingyiChen-AI/multi-chart-draw-skills   # 首次
        markmap docs/lls-mindmap.md -o docs/lls-mindmap-markmap.html --no-open

    ⚠️ 技能的 `scripts/render_mindmap.py` 在 Windows 上调不动：它用
       `subprocess.run(["markmap", ...])`，而 npm 全局装出来的是
       `markmap.cmd` 垫片 —— 不走 shell 解析不到。所以这里直接调 markmap，
       不去改第三方技能的文件（改了下次 update 会被覆盖）。

用法：
  python docs/make_mindmap.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "state"
OUT = Path(__file__).resolve().parent / "lls-mindmap.md"


def _count(name: str) -> int:
    p = STATE / name
    try:
        return sum(1 for line in p.read_text(encoding="utf-8").splitlines()
                   if line.strip())
    except OSError:
        return 0


def _jsonl(name: str) -> list[dict]:
    try:
        lines = (STATE / name).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def stats() -> dict:
    ll = _jsonl("labels-llm.jsonl")
    ev = _jsonl("events.jsonl")
    dv = _jsonl("deviations.jsonl")
    return {
        "days": len(list(STATE.glob("timeline-*.json"))),
        "features": _count("features.jsonl"),
        "self_labels": _count("labels.jsonl"),
        "ai_labels": len(ll),
        "with_evidence": sum(1 for r in ll if r.get("evidence")),
        "links": sum(len(r.get("links") or []) for r in ll),
        "events": len(ev),
        "ev_src": Counter(r.get("src") for r in ev),
        "outcome": Counter(r.get("outcome") for r in ev if r.get("outcome")),
        "reason": Counter(r.get("reason_class") for r in ev if r.get("reason_class")),
        "verdict": Counter(r.get("verdict") for r in dv),
        "observed_by": Counter(r.get("observed_by") for r in dv if r.get("observed_by")),
        "dataset": _count("dataset.jsonl"),
    }


def build(s: dict) -> list[str]:
    """markmap 的 Markdown。`#` 中心，`##` 一级，`###` 二级，`-` 是叶子说明。"""
    v = s["verdict"]
    reasons = " · ".join(f"{k} {n}" for k, n in s["reason"].most_common()) or "还没有"
    return [
        "# LLS · LifeLogging System",
        "- 一个人的可回归生活数据集",
        "",
        "## ① 采集层",
        "- 原始信号，不做解释",
        f"### 桌面 ActivityWatch\n- {s['days']} 天 timeline",
        "### 手机（Tasker → 中继）\n- 解锁 / 应用切换",
        "### 位置（Dawarich）\n- 停留推导 + 离线反查地名",
        "### 手表（Gadgetbridge）\n- 步数 / 心率 / 睡眠",
        "### 网页（aw-watcher-web）\n- 站点 / 页面标题",
        "",
        "## ② 结构化层",
        "- LLM 把文字变成事件，不改原文",
        f"### 小时问答\n- {s['ev_src'].get('hourly', 0)} 条事件",
        f"### 问责备注\n- {s['ev_src'].get('commitment', 0)} 条事件",
        "### 记：随手笔记\n- 接口就绪 · 目前 0 条",
        "### 每条都带 quote 逐字回查\n- 对不上就丢弃，防改写事实",
        "",
        "## ③ 标注层",
        "- 分数 + 逐字证据 + 证词边",
        f"### 自评 1–5\n- {s['self_labels']} 条 · 本人亲口 · 观测值",
        f"### AI 补分\n- {s['ai_labels']} 条，其中 {s['with_evidence']} 条带证据",
        "### 七个维度\n- 体力 心情 睡眠 · 人际 吃饭 休息 学习",
        "### 分数来源互不覆盖\n- labels 与 labels_llm 是两个字段",
        f"### 证词边\n- {s['links']} 条 · 他说过的关联，不是因果",
        "",
        "## ④ 合成层",
        "- 带参照系的数据",
        "### 课表偏离\n- "
        + f"{sum(v.values())} 时段：{v.get('on_track', 0)} 符合 · "
          f"{v.get('deviated', 0)} 偏离 · {v.get('unobserved', 0)} 未观测",
          f"{v.get('deviated', 0)} 偏离 · {v.get('unobserved', 0)} 未观测",
        "### 第二观测源\n- "
        + f"屏幕 {s['observed_by'].get('screen', 0)} · "
          f"自述 {s['observed_by'].get('self', 0)}",
        f"### 数据集\n- {s['dataset']} 行 · 今天 + 昨天（prev_gap_days）",
        "",
        "## ⑤ 交互层（飞书）",
        "- 问 → 答 → 标注",
        "### 四批题目 · 卡片\n- 问责 / 实时 / 收尾 / 打卡",
        "### 打完分追一句「为什么」\n- 证据的唯一来源",
        "### 偏离追问（红色卡片）\n- 七类理由 → reason_class",
        "### 今日复盘\n- 数据 + 你的回答 + 建议",
        "",
        "## ⑥ 标注语义",
        "- 三者混为一谈是这套系统最容易犯的错",
        "### 观测值\n- 自评 1–5 / 屏幕记录 / 他说的活动 —— 可直接当事实",
        "### 估计值\n- AI 从文本推的分 —— 带 confidence，不足时是 null 不是 3",
        "### 证词\n- links / reason_class —— 他说的原因，不是验证过的因果",
        "### 目前问责结果\n- "
        + " · ".join(f"{k} {n}" for k, n in s["outcome"].most_common()),
        f"### 目前偏离理由\n- {reasons}",
        "",
        "## ⑦ 还没做的",
        "- 预测（先攒样本）· 可视化 · 代理层（授权问题，不是数据问题）",
    ]


def main() -> int:
    s = stats()
    lines = build(s)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  OK  {OUT}")
    print(f"      {s['days']} 天原始数据 · {s['features']} 行特征 · "
          f"{s['self_labels']} 条自评 · {s['ai_labels']} 条 AI 标注 · "
          f"{s['events']} 条结构化事件 · {s['dataset']} 行数据集")
    print("      渲染：markmap docs/lls-mindmap.md -o docs/lls-mindmap-markmap.html --no-open")
    return 0


if __name__ == "__main__":
    sys.exit(main())
