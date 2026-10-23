#!/usr/bin/env python3
"""生成 LLS 项目的思维导图 → docs/lls-mindmap.html（自包含，无外部依赖）。

**为什么不用现成的绘图技能**

    这张图要能**长期跟着数据变**：样本量、事件条数、偏离判定都在动。
    每次手画一遍必然过时。所以数据从 `state/` 现扫，图由代码生成。

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
OUT = Path(__file__).resolve().parent / "lls-mindmap.html"

# ── 画布参数 ────────────────────────────────────────────────────────
NODE_H = 40
GAP_Y = 14
# 三层：根 → 分支 → 叶子。列宽要容得下最长的一行说明文字。
COL_X = (40, 330, 660)
COL_W = (210, 210, 330)
PAD = 40

# 分支配色：色相拉开，明度接近，打印/深色底都读得出来
COLORS = {
    "1": "#2f6f9f",   # 采集
    "2": "#8a5a2b",   # 结构化
    "3": "#6b4b9a",   # 标注
    "4": "#2c7a5e",   # 合成
    "5": "#9a3b4a",   # 交互
    "0": "#1f2328",   # 根
}


def _count(name: str) -> int:
    p = STATE / name
    if not p.exists():
        return 0
    try:
        return sum(1 for line in p.read_text(encoding="utf-8").splitlines()
                   if line.strip())
    except OSError:
        return 0


def _jsonl(name: str) -> list[dict]:
    p = STATE / name
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
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
        "ai_with_evidence": sum(1 for r in ll if r.get("evidence")),
        "links": sum(len(r.get("links") or []) for r in ll),
        "events": len(ev),
        "ev_src": Counter(r.get("src") for r in ev),
        "ev_cat": Counter(r.get("category") for r in ev if r.get("category")),
        "outcome": Counter(r.get("outcome") for r in ev if r.get("outcome")),
        "reason": Counter(r.get("reason_class") for r in ev if r.get("reason_class")),
        "verdict": Counter(r.get("verdict") for r in dv),
        "observed_by": Counter(r.get("observed_by") for r in dv if r.get("observed_by")),
        "dataset": _count("dataset.jsonl"),
    }


def build_tree(s: dict) -> dict:
    """树结构。每个节点：text / note / children。note 是灰色小字。"""
    return {
        "text": "LLS · LifeLogging System",
        "note": "一个人的可回归生活数据集",
        "color": COLORS["0"],
        "children": [
            {
                "text": "① 采集层",
                "note": "原始信号，不做解释",
                "color": COLORS["1"],
                "children": [
                    {"text": "桌面 ActivityWatch",
                     "note": f"{s['days']} 天 timeline",
                     "color": COLORS["1"]},
                    {"text": "手机（Tasker → 中继）",
                     "note": "解锁 / 应用切换",
                     "color": COLORS["1"]},
                    {"text": "位置（Dawarich）",
                     "note": "停留推导 + 离线反查地名",
                     "color": COLORS["1"]},
                    {"text": "手表（Gadgetbridge）",
                     "note": "步数 / 心率 / 睡眠",
                     "color": COLORS["1"]},
                    {"text": "网页（aw-watcher-web）",
                     "note": "站点 / 页面标题",
                     "color": COLORS["1"]},
                ],
            },
            {
                "text": "② 结构化层",
                "note": "LLM 把文字变成事件",
                "color": COLORS["2"],
                "children": [
                    {"text": "小时问答",
                     "note": f"{s['ev_src'].get('hourly', 0)} 条事件",
                     "color": COLORS["2"]},
                    {"text": "问责备注",
                     "note": f"{s['ev_src'].get('commitment', 0)} 条事件",
                     "color": COLORS["2"]},
                    {"text": "记：随手笔记",
                     "note": "接口就绪 · 目前 0 条",
                     "color": COLORS["2"]},
                    {"text": "每条都带 quote 逐字回查",
                     "note": "对不上就丢弃，防改写事实",
                     "color": COLORS["2"]},
                ],
            },
            {
                "text": "③ 标注层",
                "note": "分数 + 证据 + 关联",
                "color": COLORS["3"],
                "children": [
                    {"text": f"自评 1–5（{s['self_labels']} 条）",
                     "note": "本人亲口 · 观测值",
                     "color": COLORS["3"]},
                    {"text": f"AI 补分（{s['ai_labels']} 条）",
                     "note": f"{s['ai_with_evidence']} 条带逐字证据",
                     "color": COLORS["3"]},
                    {"text": "七个维度",
                     "note": "体力 心情 睡眠 · 人际 吃饭 休息 学习",
                     "color": COLORS["3"]},
                    {"text": f"证词边 {s['links']} 条",
                     "note": "他说过的关联 · 不是因果",
                     "color": COLORS["3"]},
                ],
            },
            {
                "text": "④ 合成层",
                "note": "带参照系的数据",
                "color": COLORS["4"],
                "children": [
                    {"text": "课表偏离",
                     "note": (f"{sum(s['verdict'].values())} 时段："
                              f"{s['verdict'].get('on_track', 0)} 符合 · "
                              f"{s['verdict'].get('deviated', 0)} 偏离 · "
                              f"{s['verdict'].get('unobserved', 0)} 未观测"),
                     "color": COLORS["4"]},
                    {"text": "第二观测源",
                     "note": (f"屏幕 {s['observed_by'].get('screen', 0)} · "
                              f"自述 {s['observed_by'].get('self', 0)}"),
                     "color": COLORS["4"]},
                    {"text": f"数据集 {s['dataset']} 行",
                     "note": "今天 + 昨天（prev_gap_days）",
                     "color": COLORS["4"]},
                ],
            },
            {
                "text": "⑤ 交互层（飞书）",
                "note": "问 → 答 → 标注",
                "color": COLORS["5"],
                "children": [
                    {"text": "四批题目 · 卡片",
                     "note": "问责 / 实时 / 收尾 / 打卡",
                     "color": COLORS["5"]},
                    {"text": "打完分追一句「为什么」",
                     "note": "证据的唯一来源",
                     "color": COLORS["5"]},
                    {"text": "偏离追问（红色卡片）",
                     "note": "七类理由 → reason_class",
                     "color": COLORS["5"]},
                    {"text": "今日复盘",
                     "note": "数据 + 你的回答 + 建议",
                     "color": COLORS["5"]},
                ],
            },
        ],
    }


def layout(tree: dict) -> tuple[list[tuple[dict, int, float]], float]:
    """横向树布局。叶子依次占一行，父节点取子节点竖直中心。"""
    placed: list[tuple[dict, int, float]] = []
    cursor = [0.0]

    def walk(node: dict, depth: int) -> float:
        kids = node.get("children") or []
        if not kids:
            y = cursor[0] * (NODE_H + GAP_Y)
            cursor[0] += 1
        else:
            ys = [walk(k, depth + 1) for k in kids]
            y = (ys[0] + ys[-1]) / 2
        node["_y"] = y
        node["_depth"] = depth
        placed.append((node, depth, y))
        return y

    walk(tree, 0)
    height = max(cursor[0] * (NODE_H + GAP_Y) - GAP_Y, NODE_H)
    # ⚠️ 根节点不能取子节点的平均：五条分支上下铺得很开，平均下来会落在
    #    最下面几条附近，整张图变成「根在左下角往上散」的怪样子。
    #    根就放在画布正中。
    tree["_y"] = (height - 58) / 2
    return placed, height


def _esc(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_svg(placed: list[tuple[dict, int, float]], height: float) -> str:
    width = COL_X[-1] + COL_W[-1] + PAD
    box_h = {0: 58, 1: 52}.get(0, NODE_H)
    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height + PAD * 2}" '
        f'width="100%" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif">'
    ]

    # 先画连线，压在节点下面
    for node, depth, y in placed:
        for kid in node.get("children") or []:
            x1 = COL_X[depth] + COL_W[depth]
            y1 = y + (box_h if depth == 0 else NODE_H) / 2 + PAD
            x2 = COL_X[depth + 1]
            y2 = kid["_y"] + NODE_H / 2 + PAD
            mx = (x1 + x2) / 2
            parts.append(
                f'<path d="M{x1},{y1} C{mx},{y1} {mx},{y2} {x2},{y2}" '
                f'fill="none" stroke="{kid["color"]}" stroke-width="1.6" '
                f'stroke-opacity=".55"/>'
            )

    # 再画节点
    for node, depth, y in placed:
        h = box_h if depth == 0 else NODE_H
        w = COL_W[depth]
        x = COL_X[depth]
        top = y + PAD
        fill = "#ffffff"
        stroke = node["color"]
        parts.append(
            f'<rect x="{x}" y="{top}" width="{w}" height="{h}" rx="9" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>'
        )
        parts.append(
            f'<rect x="{x}" y="{top}" width="5" height="{h}" rx="2.5" '
            f'fill="{stroke}"/>'
        )
        lines = node["text"].split("\n")
        base = top + h / 2 - (len(lines) - 1) * 8
        if len(lines) > 1 or node.get("note"):
            base -= 7
        for i, ln in enumerate(lines):
            parts.append(
                f'<text x="{x + 16}" y="{base + i * 16}" font-size="13.5" '
                f'font-weight="600" fill="#1f2328">{_esc(ln)}</text>'
            )
        if node.get("note"):
            ny = top + h - 11 if len(lines) > 1 else top + h / 2 + 11
            parts.append(
                f'<text x="{x + 16}" y="{ny}" font-size="11" fill="#6a737d">'
                f'{_esc(node["note"])}</text>'
            )

    parts.append("</svg>")
    return "\n".join(parts)


def main() -> int:
    s = stats()
    tree = build_tree(s)
    placed, height = layout(tree)
    svg = render_svg(placed, height)

    legend = [
        ("#2f6f9f", "采集层", "原始信号，不做解释"),
        ("#8a5a2b", "结构化层", "LLM 把文字变成事件"),
        ("#6b4b9a", "标注层", "分数 + 逐字证据 + 证词边"),
        ("#2c7a5e", "合成层", "课表偏离 · 一天一行"),
        ("#9a3b4a", "交互层", "飞书卡片：问 → 答 → 标注"),
    ]
    legend_html = "".join(
        f'<div class="lg"><span class="dot" style="background:{c}"></span>'
        f'<b>{t}</b><i>{d}</i></div>' for c, t, d in legend)

    # 标注语义：这三类东西混在一起是这个项目最容易出的错，图上必须写清楚。
    semantics = [
        ("#1f6f43", "观测值",
         "本人自评 1–5 · 屏幕记录 · 他自己说的活动。可以直接当事实用。"),
        ("#8a5a2b", "估计值",
         "AI 从自由文本推的分。带 confidence，信息不足时是 null 而不是 3。"),
        ("#8a3b6b", "证词",
         "links / reason_class。他说的原因，不是验证过的因果。"),
    ]
    sem_html = "".join(
        f'<div class="sm"><span class="dot" style="background:{c}"></span>'
        f'<b>{t}</b><i>{d}</i></div>' for c, t, d in semantics)

    out_note = (f"{s['days']} 天原始数据 · {s['features']} 行特征 · "
                f"{s['self_labels']} 条自评 · {s['ai_labels']} 条 AI 标注 · "
                f"{s['events']} 条结构化事件 · {s['dataset']} 行数据集")

    html = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>LLS · 项目思维导图</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0; padding: 32px 28px 56px;
    font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #fbfbfa; color: #1f2328;
  }}
  h1 {{ font-size: 20px; margin: 0 0 4px; letter-spacing: .01em; }}
  .sub {{ color: #6a737d; font-size: 13px; margin-bottom: 22px; }}
  .card {{
    background: #fff; border: 1px solid #e5e7eb; border-radius: 14px;
    padding: 18px 16px; overflow-x: auto;
  }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 10px 26px; margin: 22px 0 0; }}
  .sem {{ display:flex; flex-wrap:wrap; gap:8px 26px; margin-top:18px;
          padding-top:16px; border-top:1px solid #e5e7eb; }}
  .sem .sm {{ display:flex; align-items:baseline; gap:7px; font-size:12.5px;
              max-width: 520px; }}
  .sem .sm i {{ color:#6a737d; font-style:normal; }}
  .lg {{ display: flex; align-items: center; gap: 7px; font-size: 12.5px; }}
  .lg i {{ color: #6a737d; font-style: normal; }}
  .lg b {{ font-weight: 600; }}
  .dot {{ width: 10px; height: 10px; border-radius: 3px; display: inline-block; }}
  .foot {{ margin-top: 22px; color: #6a737d; font-size: 12px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background:#16181c; color:#e6edf3; }}
    .card {{ background:#1c1f24; border-color:#2d333b; }}
    text {{ fill: #e6edf3 !important; }}
    rect[fill="#ffffff"] {{ fill: #1c1f24 !important; }}
  }}
</style>
<h1>LLS · LifeLogging System</h1>
<div class="sub">{out_note}</div>
<div class="card">{svg}</div>
<div class="legend">{legend_html}</div>
<div class="sem">{sem_html}</div>
<div class="foot">
  数字由 <code>docs/make_mindmap.py</code> 从 <code>state/</code> 现扫生成 ·
  改完数据重跑一次即可，不要手改这张图。
</div>
</html>
"""
    OUT.write_text(html, encoding="utf-8")
    print(f"  OK  {OUT}")
    print(f"      {out_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
