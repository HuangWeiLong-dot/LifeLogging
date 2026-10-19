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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    BudgetExceeded,
    CostLedger,
    Day,
    EventsClient,
    LLM,
    TruncatedOutput,
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

## Source discipline (critical)

- The **Time Log** is built **only** from the "Time blocks" section. Those are the
  only measured durations.
- Notes, Q&A answers, location stays, phone unlock counts and health data are
  **context**. They explain or qualify a recorded block; they never add screen time.
- Do **not** invent a Time Log block for an activity mentioned in Q&A or notes if
  it has no recorded block. Mention it in Review instead.
- The hourly questions include ranges like `07:18–08:18`. That is the **question's
  window**, not a measured duration. Never copy those ranges into the Time Log.
- The sum of all `[duration::]` values must stay within 60% of the rule-engine total
  shown under "Rule engine totals". If you are about to write more, you are
  fabricating time — stop and re-read the recorded blocks.

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


# 健康指标 → 单位。没列出来的就不加后缀（大部分指标的名字本身已经够清楚）。
HEALTH_UNITS = {
    "steps": "steps",
    "sleep_hours": "h",
    "run_km": "km",
    "run_minutes": "min",
    "run_count": "runs",
    "vo2max": "ml/kg/min",
    "heart_rate_avg": "bpm",
    "heart_rate_min": "bpm",
    "heart_rate_max": "bpm",
}


def _fmt_health(key: str, value) -> str:
    """把一条健康数据渲染成「模型不会读错」的一行。

    ⚠️ 配速必须**换算**，不能只给秒数。存量是 283（秒/公里），
       直接写「run_pace_s_per_km: 283.0」的话模型有两种翻车方式：
       当成 283 分钟配速（荒谬，它可能不说），或者干脆忽略这一项。
       写成「283 s/km = 4:43/km」就没有歧义了。
    """
    if key == "run_pace_s_per_km" and isinstance(value, (int, float)) and value:
        mmss = f"{int(value // 60)}:{int(value % 60):02d}"
        return f"{key}: {value} s/km (= {mmss} per km)"
    unit = HEALTH_UNITS.get(key)
    return f"{key}: {value} {unit}" if unit else f"{key}: {value}"


def _qa_bullets(qa: list | None, max_items: int = 40,
                q_chars: int = 160, a_chars: int = 300) -> list[str]:
    """把问答压成 prompt 里的几行。兼容 `/qa` 字典和旧的 `(问, 答)`。"""
    out: list[str] = []
    for x in (qa or [])[:max_items]:
        if isinstance(x, dict):
            q = str(x.get("text") or "")
            a = str(x.get("answer") or x.get("value") or "")
            n = str(x.get("note") or "")
        elif isinstance(x, (tuple, list)) and len(x) >= 2:
            q, a = str(x[0] or ""), str(x[1] or "")
            n = str(x[2]) if len(x) > 2 and x[2] else ""
        else:
            continue
        q = " ".join(q.split())[:q_chars]
        a = " ".join(a.split())[:a_chars]
        n = " ".join(n.split())[:a_chars]
        if not (q or a):
            continue
        out.append(f"- Q: {q or '(question text unavailable)'}")
        out.append(f"  A: {a}" + (f"  ({n})" if n else ""))
    return out


def build_user_prompt(tl: dict, day: Day, carryover: list[str],
                      history: list[dict] | None = None,
                      qa: list | None = None,
                      previous_day_qa: list | None = None,
                      previous_day_recap: str = "") -> str:
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
    lines.append("（rule = decided by rules; "
                 "learned = categorised once by a model and cached, treat it like a rule "
                 "unless the titles clearly say otherwise; "
                 "ambiguous = rules could not decide, **you must judge these yourself**; "
                 "default = rules did not recognise the app, almost certainly Other）")
    lines.append("")

    # 用户在飞书里随手记下的一笔。
    #
    # ⚠️ 放在最前面（紧跟时间块），而且要明说它**高于推断**。
    #    「见了老王，聊了项目」这种事实，窗口标题永远推不出来；反过来，
    #    让模型把笔记和窗口标题放在同等地位，它可能用「你在 VSCode 待了 4 小时」
    #    盖掉「今天主要在陪家人」这种真正重要的事。
    notes = tl.get("notes") or []
    if notes:
        lines.append("## Notes — written by the user during the day")
        lines.append("")
        lines.append("These are things the user explicitly wanted remembered. They are")
        lines.append("**first-hand facts** and outrank anything inferred from window titles")
        lines.append("or location. If a note contradicts a guess of yours, the note wins.")
        lines.append("")
        for n in notes[:50]:
            text = " ".join(str(n.get("text") or "").split())
            if not text:
                continue
            ts = n.get("ts")
            try:
                hhmm = datetime.fromisoformat(str(ts)).astimezone(day.tz).strftime("%H:%M")
            except (TypeError, ValueError):
                hhmm = ""
            lines.append(f"- [{hhmm}] {text}" if hhmm else f"- {text}")
        lines.append("")

    # 今天亲自回答的题。和 Notes 同属第一手材料，所以紧挨着它放。
    # 这是「实时回答也要算进当天数据」的落点：小时题 + 收尾题都在这里。
    if qa:
        lines.append("## Questions you answered today (first-hand)")
        lines.append("")
        lines.append("These are things the user said himself today, answering the")
        lines.append("system's questions. They are **first-hand facts** and outrank")
        lines.append("anything inferred from window titles, location or app use.")
        lines.append("")
        lines.append("They describe what happened, but they are **not measured screen")
        lines.append("time**. Do not create Time Log blocks from them, and do not add")
        lines.append("their durations to the recorded total.")
        lines.append("")
        lines.extend(_qa_bullets(qa, max_items=40, q_chars=160, a_chars=300))
        lines.append("")

    health = tl.get("health") or {}
    if health:
        lines.append("## Health data")
        lines.append("")
        for k, v in health.items():
            lines.append(f"- {_fmt_health(k, v)}")
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
        # ⚠️ 逐键打印会把 top_apps 变成 Python 列表字面量（[['Alipay', 38.0], …]），
        #    模型只能猜那是什么。按真实形状渲染。
        lines.append("## Phone")
        lines.append("")
        lines.append(f"- Unlocks: {phone.get('unlocks', 0)}"
                     + (f" (first {phone['first_unlock']}, last {phone['last_unlock']})"
                        if phone.get("first_unlock") else ""))
        lines.append(f"- App switches: {phone.get('app_switches', 0)}")
        if phone.get("screen_minutes"):
            # 这个是**实测**的（screen_on → screen_off）。跟下面那个推导值
            # 必须区分开，否则模型会拿错那个数字当屏幕时间报出去。
            lines.append(
                f"- Screen-on time: {phone['screen_minutes']:.0f} min "
                "(measured directly from screen on/off events — this IS the phone's "
                "screen time)"
            )
        if phone.get("foreground_minutes"):
            lines.append(
                f"- App foreground time: {phone['foreground_minutes']:.0f} min "
                "(a lower bound — missed app switches and unpaired screen events are "
                "not counted, so it sits BELOW the screen-on time above. Do not report "
                "this one as screen time.)"
            )
        top = phone.get("top_apps") or []
        if top:
            lines.append("- Most used apps, by inferred foreground minutes:")
            for name, mins in top:
                lines.append(f"  - {name}: {mins:.0f} min")
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

    # ── 昨天亲口说的话（问答 + 复盘）──
    #
    # ⚠️ 不能只靠关键词检索：那是有损的、可能召回错的。用户明确要求
    #    「昨天答的题要算到今天的数据里」，所以这里**显式**带上前一天的
    #    全部问答和复盘。它提供的是连续性，不是今天的事件。
    if previous_day_qa or previous_day_recap:
        lines.append("## What the user said yesterday")
        lines.append("")
        lines.append("Carried over so today's entry has continuity. This is yesterday's")
        lines.append("first-hand context, **not** today's events — do not treat it as")
        lines.append("something that happened today. Never turn yesterday's answers into")
        lines.append("today's Time Log blocks.")
        lines.append("")
        if previous_day_recap:
            recap = str(previous_day_recap).strip()[:1500]
            if recap:
                lines.append("### Yesterday's recap")
                lines.append("")
                lines.append(recap)
                lines.append("")
        if previous_day_qa:
            lines.append("### Yesterday's questions and answers")
            lines.append("")
            lines.extend(_qa_bullets(previous_day_qa, max_items=30,
                                     q_chars=120, a_chars=240))
            lines.append("")

    # ── 相关历史（长期记忆 · 检索层）──
    #
    # ⚠️ 必须明说「可能是噪音」。检索是按关键词召回的，召回来的东西经常只是
    #    字面重合；不提醒的话模型会把某篇旧日记里的一句硬套到今天。
    #
    # ⚠️ 也不能只堆素材不说用途。这里给的是**支撑上下文**，不是让它复述。
    #
    # ⚠️ 只取前 5 条、每条 400 字。注入越多越糟 —— 一份两万字的历史塞进去，
    #    模型对今天的注意力会被稀释，主任务反而变差。
    if history:
        lines.append("## Related history (retrieved by keyword — may contain noise)")
        lines.append("")
        lines.append("Pulled from past journals, your own notes and your answers, based on")
        lines.append("overlap with today's places/apps/notes. **Supporting context only**:")
        lines.append("use one only if it genuinely explains something about today. Do not")
        lines.append("force a connection, do not list them, and do not summarise them.")
        lines.append("")
        for h in history[:5]:
            text = " ".join(str(h.get("excerpt") or "").split())
            if not text:
                continue
            lines.append(f"- [{h.get('day', '')}] ({h.get('kind', '')}) {text}")
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


# 模型写出的总时长与规则引擎相差超过这个比例，就判定正文没有落在数据上，
# **拒绝写出**（退出码非 0 → run_daily.ps1 不推进水位线 → 下次自动重来）。
#
# 定 60%：模型合并/拆分块、重新判断分类都会让总数动一些，这是正常的；
# 但差到一倍以上就不是「有出入」，是「编的」。实测那次是 607%。
MAX_DEVIATION = 0.60


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


def _local_span_to_utc(day: Day, span: str) -> list[str] | None:
    """``"09:30-12:00"`` → ``["…T01:30:00Z", "…T04:00:00Z"]``（当天本地时区）。"""
    try:
        a, b = span.split("-", 1)
        t1 = datetime.strptime(a.strip(), "%H:%M")
        t2 = datetime.strptime(b.strip(), "%H:%M")
    except (ValueError, AttributeError):
        return None

    d = day.start_local.date()
    start = datetime.combine(d, t1.time(), tzinfo=TZ)
    end = datetime.combine(d, t2.time(), tzinfo=TZ)
    # 跨午夜的块（"23:40-00:30"）要往后挪一天，否则 end < start 会被端点当无效丢掉。
    if end <= start:
        end += timedelta(days=1)

    return [
        start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ]


def write_back_blackholes(day: Day, body: str) -> int:
    """把日记正文里标了 ``[blackhole:: true]`` 的时间段回写进 blocks 表。

    ⚠️ 为什么必须有这一步：
       黑洞是 AI 读完一天活动之后的**语义判断**，规则引擎判不出来。
       但 AI 的输出落在**日记正文**（Markdown），而 Grafana 读的是数据库 ——
       不回写的话面板永远是空的。这正是本次改动之前的状态：
       概览页「今日时间黑洞」和「时间与分类 → 时间黑洞」两张面板全空，
       因为 blocks.blackhole 从来没有任何代码把它设成 True。

    ⚠️ **失败不能影响日记**：日记这时已经写盘了，回写只是给可视化补数据。
       为了它让整天的处理失败（退出码非 0 → 水位线不推进 → 反复重跑）
       是本末倒置。
    """
    ranges = [
        r for r in (
            _local_span_to_utc(day, m.group(1))
            for m in BLOCK_RE.finditer(body)
            if m.group(4).strip().lower() == "true"
        )
        if r
    ]
    if not ranges:
        return 0

    try:
        client = EventsClient(CFG)
        if not client.ping():
            print("  ⚠️  events API is down -- blackhole flags not written back")
            return 0
        r = httpx.post(
            f"{client.base}/blocks/mark-blackhole",
            headers=client.headers,
            json={"day": day.iso, "ranges": ranges},
            timeout=30.0,
        )
        r.raise_for_status()
        return int(r.json().get("marked", 0))
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Blackhole write-back failed: {e}")
        return 0


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
    ]

    # ⚠️ health 必须在收尾的 `---` **之前**追加。
    #
    #    原来写成了「先 fm += ['---', '']，再 fm += ['health:', ...]」——
    #    于是整个 health 块落到 frontmatter **外面**，日记文件的开头变成：
    #        ---             ← frontmatter 收尾
    #        health:         ← 莫名其妙出现在正文里
    #          steps: 18120.0
    #        # 2026-10-13 Tuesday
    #
    #    这个 bug 一直都在，只是从来没有设备产出过健康数据，所以从没触发。
    #    Garmin 的跑步数据一进来就现形了。
    if timeline.get("health"):
        fm.append("health:")
        fm += [f"  {k}: {v}" for k, v in timeline["health"].items()]

    fm += ["---", ""]

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
    except TruncatedOutput as e:
        # 退出码非 0 → run_daily.ps1 不会推进水位线 → 那天会重跑。
        # 关键是**别把半截正文写出去**：写出去就等于这一天被「成功」处理了。
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

    # ── 硬闸门：模型写的时长和规则引擎对不上太多，就不要写 ─────────────
    #
    # 原来这里只是打一条警告，然后把错的正文写进 vault。实测 2026-10-13：
    # 规则引擎 0.41h，模型写了 2.9h，差 607% —— 警告进了 AI 会话日志，
    # 而日记里那几条时间块是编的（连「19:16-21:11」这种数据里根本不存在的
    # 区间都写出来了）。退出码 0、水位线照常推进，于是那一天永远不会重跑。
    #
    # 跟输出被 max_tokens 截断是同一类问题：**发现得了，但拦不住。**
    rule_hours = float(tl.get("total_hours") or 0)
    if rule_hours > 0 and total > 0:
        dev = abs(total - rule_hours) / rule_hours
        if dev > MAX_DEVIATION:
            print(f"❌ Model total {total:.1f}h vs rule-engine {rule_hours:.1f}h — "
                  f"{dev*100:.0f}% apart, over the {MAX_DEVIATION*100:.0f}% limit.")
            print("   The time log is not grounded in the data, so it is NOT written.")
            print("   The day stays unprocessed and will be retried on the next run.")
            sys.exit(1)

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

    # 5b. 把 AI 标出的时间黑洞回写进数据库（Grafana 读的是数据库，不是日记）
    n_bh = write_back_blackholes(day, body)
    if n_bh:
        print(f"OK  Blackhole flags: {n_bh} block(s) marked from the journal")

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
