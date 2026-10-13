"""夜间 AI 生成日记 → 写入 Obsidian。

流程：
  cost_guard 预检 → 读时间轴 → 读健康/位置/手机事件 → 读昨天未完成任务
  → 一次 LLM 调用（分类复核 + 写复盘）→ 组装 Markdown → 写库 → 写 AI 会话日志

**为什么只调一次 LLM**：分类已经由 normalize.py 的规则做掉大半，
这里剩下的工作量不足以拆成两次调用。每次调用都要重传上下文，拆开只会更贵。

用法：
  python scripts/generate_journal.py --date today
  python scripts/generate_journal.py --date today --dry-run    # 只打印不写文件
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    BudgetExceeded,
    CostLedger,
    Day,
    LLM,
    header,
    load_config,
    new_run_id,
)

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])
VAULT = Path(CFG["vault_path"])

# ── 系统提示词 ──────────────────────────────────────────────────────
# 这段每天完全一样，开 prompt caching 后基本按 1/10 价格计费。
# 所以：内容尽量写全、写死，不要怕长。改这段会失效一天缓存。

SYSTEM_PROMPT = """You are a personal time analyst. Your job is to turn one day of raw
captured data into an honest, restrained, actionable journal entry.

## What you receive

1. **Time blocks** — computer usage already classified once by rules, each tagged
   with a confidence level
2. **Health data** — sleep, steps, heart rate (from a wearable)
3. **Location data** — places visited and time spent (from phone GPS)
4. **Phone data** — unlock counts, app switch counts
5. **Yesterday's unfinished tasks** — used to build today's task list

## Classification scheme

Use exactly these 8 categories. Do not invent new ones:

`Work / Study / Life / Transport / Leisure / Health / Social / Other`

Definitions:
- **Work** — produces output, someone pays for it, or it directly supports your living
- **Study** — input aimed at building capability, with no deliverable in the short term
- **Life** — sustaining existence and order (meals, chores, shopping, finances, medical)
- **Transport** — moving from A to B
- **Leisure** — chosen relaxation you don't regret afterwards
- **Health** — exercise, sleep preparation, meditation, checkups
- **Social** — interaction whose purpose is the relationship itself (not work talk)
- **Other** — genuinely can't tell

**Boundary rules** (these are the ones most often misjudged — follow them strictly):
- Using AI tools (Claude/ChatGPT/Copilot) to get work done → **Work**;
  using them purely to learn something new → **Study**
- Work talk in WeChat/Feishu/DingTalk → **Work**; casual chat → **Social**
- Watching a tutorial on Bilibili/YouTube → **Study**; watching entertainment → **Leisure**.
  The discriminator is the **window title**: course names, technical terms, or "tutorial"
  → Study; otherwise → Leisure
- Scrolling your phone while commuting → **Transport** (not Leisure — your control over
  that time is constrained by the fact that you're in transit)
- Any block whose confidence is marked `ambiguous` **must be judged by you**.
  Do not copy the rule engine's provisional answer.

## What counts as a "time sink"

This is the most important output of the system. A block counts as a time sink
**only if all four hold**:
1. It lasted at least 15 minutes
2. In hindsight you cannot say what it produced
3. It was not restorative (rest you chose is not a sink — sleeping, going for a walk)
4. It was not something you planned

**Typical sinks**: aimless feed scrolling, drifting between apps with no purpose,
videos you finished but can't recall, repeatedly checking things that didn't need checking.

**Not sinks**: deliberately scheduled entertainment (gaming, a show — you knew you were
relaxing), a long conversation with family, staring into space while you figured something out.

When unsure, do **not** mark it. A false positive corrupts the whole review — worse
than a miss.

## Output format

Output the journal body only. **Start at `## Time Log` and end with the content of
`### Adjustments for Tomorrow`.** Do not output frontmatter. Do not output a
`## Notes` section — that part is written by hand and preserved by the system.

Follow this structure exactly:

## Time Log

- [time:: HH:MM-HH:MM] [duration:: X.Xh] [category:: Category] [blackhole:: true/false] One line saying what you did

(Requirements:
 - Chronological order
 - Merge adjacent small blocks into meaningful ones. **Keep the day to 6–18 blocks.**
   Do not list every minute.
 - Every description must be specific. "Office work" is useless;
   "Fixed form validation on the login page" is a real entry.
 - No Markdown special characters inside the description
 - Duration in human-readable form: 1.5h, 0.5h, 2h)

## Tasks

- [ ] Specific task (from yesterday's unfinished items plus anything today's data implies)
- [x] Completed task

(If there are no tasks at all, write `- [ ] (no tasks recorded today)`)

## Review

### Time Sinks

(List each sink: time range + duration + what you were doing + why you judge it a sink.
 If there genuinely were none, write "No blocks were flagged as time sinks today."
 Do not invent one to fill the section.)

### What Went Well

(2–4 items. Must be grounded in data — name the specific time range and the specific
 behaviour. Never write something content-free like "worked hard today".)

### Adjustments for Tomorrow

(1–3 **concrete, executable** adjustments. Each must be verifiable tomorrow.
  BAD: "spend less time on my phone" — unverifiable
  GOOD: "Mute WeChat notifications 9–11am, because the data shows 40 minutes lost to it
         in that window")

## Tone

- State facts, not feelings. If the data says 4 hours of scrolling, write 4 hours of scrolling.
- No comfort, no encouragement, no "great job".
- No self-flagellation either — a time sink is a data point, not a moral failing.
- **Write in English.** Concise. No emoji.
"""


# ── 日记小节标题 ────────────────────────────────────────────────────
# 记录一律英文。这些标题在四处被引用：本文件的组装逻辑、read_carryover 的解析、
# vault/99-Templates/Journal-Template.md、以及各 Dataview 面板。
# 提成常量是为了改名字时只改一处 —— 中文英文各写一遍必然会漂移。
H_TIME = "## Time Log"
H_TASKS = "## Tasks"
H_REVIEW = "## Review"
H_SINKS = "### Time Sinks"
H_GOOD = "### What Went Well"
H_ADJUST = "### Adjustments for Tomorrow"
H_NOTES = "## Notes"
H_AI = "## AI Usage"

# ── 数据准备 ────────────────────────────────────────────────────────


def load_timeline(day: Day) -> dict:
    p = Path(CFG["state_dir"]) / f"timeline-{day.iso}.json"
    if not p.exists():
        print(f"FAIL timeline not found {p}")
        print(f"   Run first: python scripts/normalize.py --date {day.iso}")
        sys.exit(1)
    return json.loads(p.read_text(encoding="utf-8"))


def build_user_prompt(tl: dict, day: Day, carryover: list[str]) -> str:
    lines: list[str] = []
    lines.append(f"# Data for {day.iso} ({day.d.strftime('%A')})")
    lines.append("")

    blocks = tl.get("blocks", [])
    lines.append(f"## Time blocks ({len(blocks)} blocks, rule engine first pass)")
    lines.append("")
    lines.append("| Time | Duration | Rule category | Confidence | App | Window title |")
    lines.append("|---|---|---|---|---|---|")
    for b in blocks:
        titles = " / ".join(b.get("titles", []))[:70].replace("|", "/")
        lines.append(
            f"| {b['time']} | {b['hours']}h | {b['category']} | "
            f"{b['confidence']} | {b.get('app','')[:18]} | {titles} |"
        )
    lines.append("")
    lines.append("（rule = decided by rules; ambiguous = rules could not decide, **you must judge these yourself**; "
                 "default = rules did not recognise the app, almost certainly Other）")
    lines.append("")

    health = tl.get("health") or {}
    if health:
        lines.append("## Health data")
        lines.append("")
        for k, v in health.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    loc = tl.get("location") or []
    if loc:
        lines.append("## Location (by time spent)")
        lines.append("")
        for l in loc[:10]:
            lines.append(f"- {l['minutes']:.0f} min @ {l['place']}")
        lines.append("")

    phone = tl.get("phone") or {}
    if phone:
        lines.append("## Phone")
        lines.append("")
        for k, v in phone.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    web = tl.get("web") or []
    if web:
        lines.append("## Top sites (from the browser extension — real URLs, not just window titles)")
        lines.append("")
        for w in web[:15]:
            lines.append(f"- {w['minutes']:.0f} min on {w['site']}")
        lines.append("")
        lines.append("Use these to disambiguate browser time: a window title saying")
        lines.append("『GitHub』 means nothing, but `github.com/acme/pull/12` tells you it was work.")
        lines.append("")

    lines.append("## Rule engine totals (for cross-checking, do not copy them)")
    lines.append("")
    for cat, h in (tl.get("by_category") or {}).items():
        lines.append(f"- {cat}: {h}h")
    lines.append(f"- TOTAL: {tl.get('total_hours', 0)}h")
    lines.append("")

    if carryover:
        lines.append("## Yesterday's unfinished tasks")
        lines.append("")
        for t in carryover:
            lines.append(f"- {t}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("Now output the journal body for this day, following the format in the system prompt.")

    return "\n".join(lines)


def read_carryover(day: Day) -> list[str]:
    """读昨天日记里未勾选的任务。"""
    prev = day.d - timedelta(days=1)
    p = VAULT / "05-Journal" / f"{prev.isoformat()}.md"
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    out = []
    in_tasks = False
    for line in text.splitlines():
        if line.strip().startswith(H_TASKS):
            in_tasks = True
            continue
        if in_tasks and line.strip().startswith("## "):
            break
        if in_tasks:
            m = re.match(r"^\s*-\s*\[ \]\s*(.+)$", line)
            if m:
                t = m.group(1).strip()
                if t and "no tasks recorded" not in t.lower():
                    out.append(t)
    return out[:20]


# ── 输出解析 ────────────────────────────────────────────────────────

BLOCK_RE = re.compile(
    r"\[time::\s*([0-9:]{4,5}-[0-9:]{4,5})\]\s*"
    r"\[duration::\s*([0-9.]+h)\]\s*"
    r"\[category::\s*([^\]]+)\]\s*"
    r"\[blackhole::\s*(true|false)\]",
    re.IGNORECASE,
)


def validate_body(body: str, expected_hours: float) -> tuple[float, list[str]]:
    """抽出 AI 输出的时间块，算总时长，返回 (总时长, 警告列表)。

    告警会写进 06-AI-Sessions 的日志里，所以用英文（记录一律英文）。
    """
    warnings: list[str] = []
    blocks = BLOCK_RE.findall(body)

    if not blocks:
        warnings.append("No well-formed time blocks parsed. Check the prompt or the model output.")
        return 0.0, warnings

    total = sum(float(m.group(1)) for m in re.finditer(
        r"\[duration::\s*([0-9.]+)\s*h\]", body, re.IGNORECASE))

    cats = [b[2].strip() for b in blocks]
    allowed = set(CFG["categories"])
    bad = sorted({c for c in cats if c not in allowed})
    if bad:
        warnings.append(f"Non-standard categories emitted: {bad} (must be in config.categories)")

    n = len(blocks)
    if n < 6:
        warnings.append(f"Only {n} blocks. Possibly over-merged — check whether half a day became one block.")
    if n > 18:
        warnings.append(f"{n} blocks. Too granular to read.")

    if expected_hours and total > 0:
        dev = abs(total - expected_hours) / expected_hours
        if dev > 0.35:
            warnings.append(
                f"Model total {total:.1f}h vs rule-engine total {expected_hours:.1f}h "
                f"— {dev*100:.0f}% apart. Worth a human look."
            )

    bh = sum(1 for b in blocks if b[3].lower() == "true")
    if bh == 0 and n >= 6:
        warnings.append("Zero time sinks flagged — verify that's real and not the model being agreeable.")

    return total, warnings


# ── 组装与写出 ──────────────────────────────────────────────────────


def extract_section(text: str, heading: str) -> str:
    """从已有日记里抠出某一节（用于保留用户手写的随手记）。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith(heading):
            start = i
            break
    if start is None:
        return ""
    depth = len(heading) - len(heading.lstrip("#"))
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("#") and (len(line) - len(line.lstrip("#"))) <= depth:
            break
        out.append(line)
    return "\n".join(out).rstrip()


def actual_sources(tl: dict) -> list[str]:
    """这个日记真正用到了哪些源。

    ⚠️ 不能列「配置里 enabled 的源」—— 那会撒谎。
    比如 Dawarich 配了但服务器没通，日记却写着 sources: dawarich。
    provenance 字段的唯一价值就是它准确；不准还不如没有。

    判断依据是「时间轴里到底有没有这类数据」，而不是「配置里开没开」。
    """
    out: list[str] = []
    if tl.get("blocks"):
        out.append("activitywatch")
    if tl.get("location"):
        out.append("dawarich")
    if tl.get("health"):
        out.append("gadgetbridge")
    if tl.get("phone"):
        out.append("tasker")
    return out or ["none"]


def compose(day: Day, body: str, stats: dict, timeline: dict,
            casual_note: str) -> str:
    fm = [
        "---",
        f'date: "{day.iso}"',
        "tags:",
        "  - journal",
        "  - lifelog",
        f"total_hours: {stats['total_hours']}",
        f"ai_cost: {stats['cost']}",
        f'ai_cost_currency: "{stats["currency"]}"',
        f"ai_calls: {stats['calls']}",
        f"ai_tokens_in: {stats['tokens_in']}",
        f"ai_tokens_out: {stats['tokens_out']}",
        f"ai_cache_hit: {stats['cache_hit']}",
        f"ai_peak_pricing: {str(stats['peak']).lower()}",
        f'generated_at: "{stats["generated_at"]}"',
        "sources:",
        *[f"  - {s}" for s in stats["sources"]],
        "---",
        "",
    ]
    if timeline.get("health"):
        fm += [
            "health:",
            *[f"  {k}: {v}" for k, v in timeline["health"].items()],
            "",
        ]

    head = f"# {day.iso} {day.d.strftime('%A')}\n"

    tail = [
        "",
        "---",
        "",
        H_AI,
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Calls | {stats['calls']} |",
        f"| Input tokens | {stats['tokens_in']:,} |",
        f"| Output tokens | {stats['tokens_out']:,} |",
        f"| Cache hits | {stats['cache_hit']:,} |",
        f"| Reasoning tokens | {stats['reasoning']:,} |",
        f"| Cost ({stats['currency']}) | {stats['cost']:.4f} |",
        f"| Peak-rate pricing | {'yes' if stats['peak'] else 'no (off-peak, half price)'} |",
        f"| Model | {stats['model']} |",
        f"| Generated at | {stats['generated_at']} |",
        "",
        f"- Details: [[06-AI-Sessions/{day.iso}]]",
        "- Running total and budget status: [[07-Reviews/Cost-Ledger]]",
        "",
    ]

    parts = [*fm, head, body.rstrip(), "", H_NOTES, ""]
    parts.append(casual_note if casual_note else "<!-- Written by hand. The script preserves this section. -->")
    parts += tail
    return "\n".join(parts)


def write_ai_session(day: Day, stats: dict, run_id: str, warnings: list[str]) -> None:
    p = VAULT / "06-AI-Sessions" / f"{day.iso}.md"
    p.parent.mkdir(parents=True, exist_ok=True)

    existing = p.read_text(encoding="utf-8") if p.exists() else (
        f"---\ndate: \"{day.iso}\"\ntags:\n  - ai-session\n---\n\n# AI Session · {day.iso}\n"
    )

    entry = [
        "",
        f"## {stats['generated_at']} · `{run_id}`",
        "",
        "- Stage: `journal`",
        f"- Provider: `{stats['provider']}` · Model: `{stats['model']}`",
        f"- Tokens: in {stats['tokens_in']:,} (cache hits {stats['cache_hit']:,}) "
        f"· out {stats['tokens_out']:,} (reasoning {stats['reasoning']:,})",
        f"- Cost: **{stats['cost']:.4f} {stats['currency']}** "
        f"at {'peak' if stats['peak'] else 'off-peak'} rate",
        f"- Blocks parsed: {stats['blocks']} · total {stats['total_hours']}h",
    ]
    if warnings:
        entry.append("- Warnings:")
        entry += [f"    - {w}" for w in warnings]

    p.write_text(existing.rstrip() + "\n" + "\n".join(entry) + "\n", encoding="utf-8")


# ── main ────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="today")
    ap.add_argument("--dry-run", action="store_true", help="Print only, write nothing")
    args = ap.parse_args()

    day = Day.parse(args.date, TZ)
    header(f"Generate journal · {day.iso}")

    # 1. 预检
    ledger = CostLedger(CFG["budget"]["ledger_path"])
    cap = float(CFG["budget"]["daily_cap"])
    cur = CFG["budget"].get("currency", "CNY")
    spent = ledger.spend_on(datetime.now(TZ).date(), TZ)
    print(f"-> Budget: spent today {spent:.4f} / {cap:.2f} {cur}")
    if spent >= cap and CFG["budget"].get("fail_closed", True):
        print("FAIL Daily budget reached, aborting.")
        sys.exit(1)

    # 2. 数据
    print("-> Reading timeline")
    tl = load_timeline(day)
    carryover = read_carryover(day)
    print(f"  {len(tl.get('blocks', []))} time blocks"
          + (f"，{len(carryover)}  unfinished tasks from yesterday" if carryover else ""))

    user_prompt = build_user_prompt(tl, day, carryover)
    print(f"  prompt length {len(user_prompt):,} chars")

    # 3. 调用
    run_id = new_run_id()
    llm = LLM(cfg=CFG, ledger=ledger)
    print(f"-> Calling {llm.provider} / {llm._model_name()}")

    before = len(ledger.entries())
    try:
        body = llm.complete(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            stage="journal",
            run_id=run_id,
        )
    except BudgetExceeded as e:
        print(f"❌ {e}")
        sys.exit(1)

    entries = ledger.entries()
    last = entries[-1] if len(entries) > before else {}
    stats = {
        "calls": 1,
        # "输入 token" 对人有意义的是总数，但成本取决于其中多少走了缓存
        "tokens_in": last.get("cache_hit", 0) + last.get("cache_miss", 0),
        "tokens_out": last.get("output", 0),
        "cache_hit": last.get("cache_hit", 0),
        "cache_miss": last.get("cache_miss", 0),
        "reasoning": last.get("reasoning", 0),
        "cost": last.get("cost", 0.0),
        "currency": last.get("currency", cur),
        "peak": bool(last.get("peak", False)),
        "model": last.get("model", "?"),
        "provider": last.get("provider", "?"),
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "sources": actual_sources(tl),
    }

    # 4. 清洗 + 校验
    body = body.strip()
    body = re.sub(r"^```(?:markdown|md)?\s*\n", "", body)
    body = re.sub(r"\n```\s*$", "", body)
    # 模型有时会自作主张带上 Notes 段，砍掉（那部分归用户手写）
    if H_NOTES in body:
        body = body.split(H_NOTES)[0].rstrip()

    total, warnings = validate_body(body, tl.get("total_hours", 0))
    stats["total_hours"] = round(total, 2)
    stats["blocks"] = len(BLOCK_RE.findall(body))

    print(f"-> Output {len(body):,} chars, {stats['blocks']} blocks, total {total:.1f}h")
    for w in warnings:
        print(f"  ⚠️  {w}")

    if args.dry_run:
        print("\n" + "=" * 60)
        print(body)
        print("=" * 60)
        print(f"\n(--dry-run, nothing written. Cost: {stats['cost']:.4f} {stats['currency']})")
        return

    # 5. 写库（保留手写的随手记）
    out = VAULT / "05-Journal" / f"{day.iso}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    casual = ""
    if out.exists():
        casual = extract_section(out.read_text(encoding="utf-8"), H_NOTES)
        if casual.startswith(H_NOTES):
            casual = "\n".join(casual.splitlines()[1:]).strip()
        if "the script never overwrites" in casual or "Written by hand" in casual:
            casual = ""

    out.write_text(compose(day, body, stats, tl, casual), encoding="utf-8")
    print(f"OK  Written {out}")

    write_ai_session(day, stats, run_id, warnings)
    print(f"OK  Appended to the AI session log")

    # 6. 各分区的副笔记（位置 / 手机 / 时间明细 / 任务 / 周报）
    #    有数据才写，没数据不生成空文件 —— 空目录比没有目录更让人困惑，
    #    但每天 4 个空文件也是噪音。
    from write_notes import write_all

    notes = write_all(day.iso, tl, carryover)
    if notes:
        print(f"OK  Side notes: {', '.join(notes)}")

    rate_note = "peak" if stats["peak"] else "off-peak (half price)"
    print(f"\nCost {stats['cost']:.4f} {stats['currency']} ({rate_note}) · "
          f"today total {spent + stats['cost']:.4f} / {cap:.2f} {cur}")
    if stats["cache_hit"]:
        tot_in = stats["cache_hit"] + stats["cache_miss"]
        print(f"Cache hits {stats['cache_hit']:,} / {tot_in:,} "
              f"({stats['cache_hit']/tot_in*100:.0f}%) -- cached portion is billed at roughly 1/50")


if __name__ == "__main__":
    main()
