"""每日提问 —— 生成第二天要拿去问你的问题。

**为什么要有它**

    日记里 AI 已经写好了 ``### Adjustments for Tomorrow``，那是第二天最该被
    追问的东西：

        - Commit the LifeLogging repo in the first 15 minutes of the session.
          The task has carried since yesterday, and today's 34 min on github.com
          went to reading the CompVis repository instead.
        - Put WeChat on Do Not Disturb from 14:00 to 18:00. The phone logged
          426 app switches and 105 min of WeChat foreground time.

    但这些话写完就躺在 vault 里了 —— 没有任何东西会第二天拿着它来问你。
    这个脚本把它们（连同挂着的任务、以及原始数据里能被算出来的矛盾）
    变成几条**只能用「做了 / 没做」回答**的问题，推到服务器中继上等手机来取。

**链路**

    本脚本 ──POST /questions──> 服务器中继 ──手机早上轮询──> 浏览器表单
                                     ↑                          │
                                     └────── POST /answer ──────┘
    答案变成事件 → 下次 collect_phone_events.py 拉回事件表 → apply_answers.py 回写

**为什么问题要「是/否」而不是开放式**

    手机上打字很累。二值答案一眼就能点完，而且能**直接**回写成
    ``- [x]`` / ``- [ ]``，不用再调一次 AI 去解析自然语言。

**用法**

    python scripts/generate_questions.py --date 2026-10-14
    python scripts/generate_questions.py --date today --dry-run
    python scripts/generate_questions.py --date today --no-push
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
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
    TruncatedOutput,
    header,
    load_config,
    new_run_id,
)

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])
VAULT = Path(CFG["vault_path"])
STATE = Path(CFG["state_dir"])

H_ADJUST = "### Adjustments for Tomorrow"   # 和 generate_journal.py 里的一致

SYSTEM_PROMPT = """You write the small batch of check-in questions that a person \
answers on their phone in the morning, about the day that just ended.

The point is ACCOUNTABILITY plus TRACKING, not journalling.

There are exactly two kinds, and the caller tells you how many of each to write:

- kind="commitment" - hold up something they themselves wrote or committed to, and \
ask whether it happened. This is the sharper kind: the source line comes from their \
own "Adjustments for Tomorrow", from an open task that has been carried over, or \
from a place where the recorded data contradicts what they claim they want.

- kind="routine" - a standing question about ONE dimension the automatic record \
cannot see: energy, mood, sleep quality, whether they actually rested, whether they \
ate properly, who they spent time with, what they learned, whether anything \
long-postponed got touched. Anchor it in something concrete from the data (a \
number, a timestamp, a conspicuous absence) so it is answerable and comparable \
from day to day. "Did you feel good today?" is worthless - too vague to answer \
honestly and impossible to compare across days.

Hard rules for the question text:
- ONE thing per question. Never bundle two questions into one, and never ask for a \
list.
- **No two questions in the batch may be answerable by the same sentence.** Before \
you output, read the batch as if you were the person answering it: if two questions \
would get the same reply, keep the sharper one and delete the other. A batch of \
twelve questions that collapses into five real answers is worse than five questions.
- **Never ask about something the data already answers.** "How long did you spend \
on the repo?" is not a question - the number is right there. Ask about the part the \
data cannot see: why it happened, what was in the way, whether it was worth it.
- NEVER use a time word - no 今天 / 昨天 / today / yesterday. The reader opens this \
the morning after, so any such word is either wrong or ambiguous and the whole \
batch reads as nonsense. Name the concrete event, time or number instead: \
"the 20:11 Tailscale key", "14:00-18:00", "the 426 app switches".
- Phrase it POSITIVELY, so that the "it happened" answer means the thing they wanted \
happened. Never ask "did you avoid doing X" or "did you manage not to X".
- Be precise rather than general. Quote the specific number, time or phrase you are \
basing it on. "Commit the repo in the first 15 minutes of the session" beats \
"work on the repo"; "a stretch over 90 minutes with no app switch" beats \
"stay focused".
- Do not moralise, do not congratulate, do not explain why the question matters. \
Ask the question and stop.
- The task list may annotate a task with "← 已经挂了 N 天" (it has been open for N \
days). When it does, say N in the question - a task that has slipped for days is \
the whole point of asking about it.
- Write the question, the evidence and any options in __LANG_NAME__. Keep proper \
nouns, app names, file paths and numbers as they are.

Choosing the answer format. **THIS IS AS IMPORTANT AS THE QUESTION ITSELF, AND A \
BATCH WHERE EVERY QUESTION IS yes_no HAS FAILED THIS INSTRUCTION.** Before you \
output anything, go through your list and ask of each one: "is a binary really the \
honest answer here, or am I about to make them tell me a lie because the buttons \
left them no choice?"

- format="yes_no" - it really is binary and nothing is lost by forcing it. Two \
buttons.
    Example: "20:11 开始的那次 Tailscale key 配置，节点最后上线了吗？"

- format="choice" - the honest answer has 3 to 5 natural, mutually exclusive values \
and a binary throws away the interesting part. **Write the options yourself**, in \
the same language as the question, short (2-8 characters where you can). Mark \
EXACTLY ONE of them "done": true - the one that means it actually happened, i.e. \
what would tick the task off. All the others "done": false. If none of your options \
means "it happened", mark none.
    Example question: "14:00-18:00 的微信免打扰，做到什么程度？"
    Example options: 整段都做到了 (done: true) / 只做到一部分 (done: false) / \
基本没做到 (done: false)
    ⚠️ Only mark an option "done": true when the question has a non-empty "ref" - \
i.e. when there is actually a task to tick off. A routine question grounded in the \
data has no task behind it, so every one of its options is "done": false.

    **A choice question about a task MUST offer a way to close it.** Add one option \
marked "drop": true meaning "not doing this — drop it from the list" (already solved \
another way, no longer relevant, decided against it). Without that option a task the \
person has abandoned keeps returning every morning — that is exactly how a task list \
turns into a zombie list. At most one option may carry "drop": true, and it must \
never be combined with "done": true.

- format="text" - the question where any short option would be a lie: what you want \
to remember from the day, what was actually on your mind, what got in the way. No \
options at all. At most ONE per batch.

The question TEXT must match the format. A "choice" question asks for a degree or a \
kind ("做到什么程度", "是什么原因"), not "有没有". An all-"yes_no"-phrased batch \
cannot be answered any other way, so the phrasing is where this decision is actually \
made.

Rough target for a batch of twelve: about half "choice", at least one "text", the \
rest "yes_no". If you produced twelve "yes_no", you did not follow this - go back \
and look for the questions whose real answer has a middle.

Rules that follow from the formats:
- If "ref" is non-empty (the question is about a specific task) the format MUST be \
"yes_no" or "choice" - a "text" answer can never tick a task off. format="text" \
always has an empty "ref".

Rules about the material you are given:
- The data section may say a source is **stale** ("data stopped at 2026-10-22", \
"last reported 6 hours ago"). Do NOT build a question on numbers from a stale \
source - by the time this is read the number is a day old and the question becomes \
unanswerable. Ask about something that cannot go stale instead.
- If the data section says "(no timeline data)" or "(nothing recorded)", that means \
the collection was down, NOT that the person did nothing. Do not use it as evidence \
that they were idle, and do not ask them to explain a gap that is your own pipeline's \
fault.
- If a previous day's unanswered question is listed below, do not simply ask it \
again in different words. Either ask something that follows from a "no", or leave it.

The "ref" field is machine-read, so it has its own rules:
- kind="commitment": if the question is about one of the "Open tasks" or \
"Adjustments" bullets, copy that line into "ref" CHARACTER FOR CHARACTER exactly as \
it appears above. It gets matched against the task list to tick the item off, so a \
paraphrase silently breaks it.
- kind="commitment" based on the data alone, and every kind="routine": empty string.

Output ONLY a JSON array, no prose and no code fences:

[
  {"kind": "commitment" | "routine",
   "format": "yes_no" | "choice" | "text",
   "text": "<the question>",
   "evidence": "<the specific thing you are basing it on>",
   "ref": "<verbatim source line, or empty string>",
   "options": [{"label": "<short>", "done": true}, {"label": "<short>", "done": false}, \
{"label": "<short>", "drop": true}]}
]

"options" is REQUIRED for format="choice" (3-5 of them, at most one "done": true) and \
MUST be absent or empty for the other two formats.

Order them strongest first within each kind. Do not pad a kind with weak questions \
to hit the quota - if you cannot find four honest routine questions, write three."""


def read_journal_section(day: Day, heading: str) -> list[str]:
    """读日记里某一段的 bullet 列表。文件不在就返回空。"""
    p = VAULT / "05-Journal" / f"{day.iso}.md"
    if not p.exists():
        return []
    out: list[str] = []
    inside = False
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(heading):
            inside = True
            continue
        if inside and re.match(r"^#{2,3}\s", line):
            break
        if inside:
            m = re.match(r"^\s*-\s*(?:\[[ xX]\]\s*)?(.+)$", line)
            if m and m.group(1).strip():
                out.append(m.group(1).strip())
    return out


def read_open_tasks() -> list[str]:
    """读 04-Tasks/Tasks.md 的 ``## Open``（那个文件是滚动维护的，不是按天的）。"""
    p = VAULT / "04-Tasks" / "Tasks.md"
    if not p.exists():
        return []
    out: list[str] = []
    inside = False
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("## Open"):
            inside = True
            continue
        if inside and line.strip().startswith("## "):
            break
        if inside:
            m = re.match(r"^\s*-\s*\[ \]\s*(.+)$", line)
            if m and m.group(1).strip() and "nothing pending" not in m.group(1).lower():
                out.append(m.group(1).strip())
    return out


def _timeline(day: Day) -> dict:
    p = STATE / f"timeline-{day.iso}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_notes(day: Day) -> list[str]:
    """当天他自己记的笔记（飞书里发的一句话）。

    ⚠️ 这是全系统**最可靠**的材料：不是推断、不是模型写的，是他直接说的。
       问责题引用它，比引用任何统计数字都更有力。
    """
    return [str(n.get("text") or "").strip()
            for n in (_timeline(day).get("notes") or [])
            if str(n.get("text") or "").strip()]


def read_time_sinks(day: Day) -> list[str]:
    """被规则标成「黑洞」的时间块。

    点名「18:22 那 26 分钟你在 Chrome 里看什么」比「你今天效率不高」有用得多 ——
    后者既不能被回答，也不能被证伪。
    """
    out: list[str] = []
    for b in (_timeline(day).get("blocks") or []):
        if not b.get("blackhole"):
            continue
        titles = " / ".join(str(t) for t in (b.get("titles") or [])[:2])
        out.append(f"{b.get('time', '')} {b.get('hours', '?')}h {b.get('app', '')}"
                   + (f" — {titles[:80]}" if titles else ""))
    return out[:6]


def read_previous_qa(day: Day) -> list[str]:
    """前一天早上的问题 + 他当时怎么答的。

    ⚠️ 回答落在**作答那天**（中继就是这么记的），所以这里配的是「前一天生成的
       问题」和事件表里的答案，按题号对上。答「没做」的那些是第二天最好的
       问责素材 —— 比任何数据都直接。
    """
    prev = day.d - timedelta(days=1)
    qp = STATE / f"questions-{prev.isoformat()}.json"
    if not qp.exists():
        return []
    try:
        payload = json.loads(qp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    texts = {str(q.get("id")): str(q.get("text") or "")
             for q in (payload.get("questions") or []) if q.get("id")}
    if not texts:
        return []

    try:
        conn = sqlite3.connect(STATE / "events.db")
        conn.execute("PRAGMA busy_timeout=5000;")
        try:
            rows = conn.execute(
                """SELECT value_text FROM events
                   WHERE domain='question' AND metric='answer'"""
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return []

    answers: dict[str, str] = {}
    for (vt,) in rows:
        parts = str(vt or "").split("|", 2)
        if parts[0] in texts:
            note = parts[2] if len(parts) > 2 else ""
            val = parts[1] + (f"（{note}）" if note else "")
            answers[parts[0]] = val

    return [f"Q: {texts[qid]}  →  A: {answers[qid]}"
            for qid in texts if qid in answers]


def task_age_days(task: str, day: Day, lookback: int = 7) -> int:
    """这条任务在往前**连续**几天的 Adjustments 里出现过。

    系统提示词早就要求「挂了好几天就说几天」，但以前的输入里根本没有这个信息，
    模型只能猜。这里补上。

    ⚠️ 复用 `apply_answers.find_task` 的比对逻辑 —— 再写一套「这两句话算不算
       同一条任务」的模糊匹配，两边迟早会给出不同答案，而那种不一致没人会发现。
    """
    try:
        from apply_answers import find_task
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for i in range(1, lookback + 1):
        d = Day(day.d - timedelta(days=i), day.tz)
        adjs = read_journal_section(d, H_ADJUST)
        if any(find_task([a], task) for a in adjs):
            n += 1
        else:
            break
    return n


def data_digest(day: Day) -> str:
    """把当天的原始数字压成几行 —— 用来找「你说的」和「数据说的」对不上的地方。

    ⚠️ 只给汇总，**不给** 时间块明细。日记正文里已经有模型自己对时间块的解读，
       这里补的是它没看到的那些数（手机切换次数、屏幕时长、步数、去了哪）。
       给全量时间块只会让 prompt 变长、缓存失效，还容易让它复述一遍。
    """
    p = STATE / f"timeline-{day.iso}.json"
    if not p.exists():
        return "(no timeline data)"
    try:
        tl = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "(timeline unreadable)"

    lines: list[str] = []
    if tl.get("total_hours"):
        cats = ", ".join(f"{k} {v}h" for k, v in (tl.get("by_category") or {}).items())
        lines.append(f"- Recorded time: {tl['total_hours']}h total ({cats})")

    ph = tl.get("phone") or {}
    if ph:
        lines.append(
            f"- Phone: {ph.get('unlocks', 0)} unlocks, {ph.get('app_switches', 0)} app switches"
            + (f", screen on {ph['screen_minutes']:.0f} min" if ph.get("screen_minutes") else "")
            + (f", first unlock {ph['first_unlock']}, last {ph['last_unlock']}"
               if ph.get("first_unlock") else "")
        )
        top = ph.get("top_apps") or []
        if top:
            lines.append("- Top phone apps: "
                         + ", ".join(f"{n} {m:.0f}min" for n, m in top[:6]))

    health = tl.get("health") or {}
    if health:
        lines.append("- Health: " + ", ".join(f"{k} {v}" for k, v in list(health.items())[:6]))

    loc = tl.get("location") or []
    if loc:
        lines.append("- Places: "
                     + ", ".join(f"{x.get('place')} {x.get('minutes', 0):.0f}min"
                                 for x in loc[:4]))

    web = tl.get("web") or []
    if web:
        lines.append("- Top sites: "
                     + ", ".join(f"{x.get('site')} {x.get('minutes', 0):.0f}min"
                                 for x in web[:6]))

    track = tl.get("track") or {}
    if track.get("span_km"):
        lines.append(f"- Movement: {track['points']} points over {track['span_km']} km")

    # 这一天的「形状」——第一段活动、最后一段、以及最长的一块。
    # 模型很难从上面那些汇总数字里自己推出「几点开电脑、几点收工」，
    # 而这两点恰恰是最容易被拿来做问责的具体坐标。
    blocks = tl.get("blocks") or []
    if blocks:
        def _start(b: dict) -> str:
            return str(b.get("time") or "").split("-")[0]
        longest = max(blocks, key=lambda b: float(b.get("hours") or 0))
        lines.append(
            f"- Day shape: first block {_start(blocks[0])}, last block "
            f"{_start(blocks[-1])}; longest {longest.get('time')} "
            f"{longest.get('hours')}h on {longest.get('app', '')}"
        )

    return "\n".join(lines) if lines else "(nothing recorded)"


def build_user_prompt(day: Day, adjustments: list[str], tasks: list[str],
                      digest: str, n_commit: int, n_routine: int,
                      notes: list[str] | None = None,
                      sinks: list[str] | None = None,
                      prev_qa: list[str] | None = None,
                      task_ages: dict[str, int] | None = None) -> str:
    """拼输入。

    ⚠️ 顺序**有意的**：他自己写的东西排在最前（笔记 → 昨天的问题与回答 →
       今天的 Adjustments），统计数字排后面。模型最先读到什么，就最可能
       以什么为依据发问 —— 而「他亲口说的」永远比「推算出来的」更有力。
    """
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {x}" for x in items) if items else "(none)"

    def task_lines() -> str:
        if not tasks:
            return "(none)"
        ages = task_ages or {}
        out = []
        for t in tasks:
            age = int(ages.get(t) or 0)
            out.append(f"- {t}" + (f"   ← 已经挂了 {age} 天" if age >= 1 else ""))
        return "\n".join(out)

    parts = [f"# Day to review: {day.iso} ({day.d.strftime('%A')})", ""]

    parts += [
        "## Things THEY said themselves (first-hand — the strongest material)",
        "",
    ]
    if notes:
        parts += ["### Notes they jotted during the day", ""]
        parts += [f"- {n}" for n in notes[:20]]
        parts += [""]
    if prev_qa:
        parts += [
            "### Yesterday morning's questions and how they answered",
            "",
            "Answers of \"no\" here are the sharpest possible basis for a question:",
            "they already admitted it did not happen.",
            "",
        ]
        parts += [f"- {x}" for x in prev_qa[:20]]
        parts += [""]
    if not notes and not prev_qa:
        parts += ["(nothing they wrote directly today)", ""]

    parts += [
        "## What their journal said they would do",
        "",
        "### Adjustments for Tomorrow",
        bullets(adjustments),
        "",
        "### Open tasks carried in the task list",
        task_lines(),
        "",
        "## What the recorded data says about that day",
        "",
        digest,
        "",
    ]

    if sinks:
        parts += ["## Time sinks (blocks the rules flagged)", ""]
        parts += [f"- {s}" for s in sinks]
        parts += [""]

    parts += [
        "---",
        "",
        f"Write {n_commit} questions of kind \"commitment\" and {n_routine} of kind "
        f"\"routine\", for the morning after this day.",
        "",
        "Every question must quote something concrete from above — a time, a number,",
        "an app, a place, or a phrase they wrote. A question that could have been",
        "asked without any of this evidence is a wasted question.",
        "",
        "Choose the answer format that lets them answer honestly (see the system",
        "prompt): some are genuinely binary, but a batch where everything is yes_no",
        "has thrown away the middle.",
    ]

    return "\n".join(parts)


FORMATS = ("yes_no", "choice", "text")
MIN_OPTIONS = 3
MAX_OPTIONS = 5
MAX_LABEL = 40


def parse_options(raw) -> list[dict]:
    """规范化 choice 的选项。认不出来就返回空 —— 调用方会把它降级成 yes_no。

    ⚠️ `done` 只认**第一个**。模型偶尔会给两个选项都标 done（它觉得「做了一半」
       也算完成），那样两行都能勾任务，回写就随机了。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    done_taken = False
    for o in raw:
        if isinstance(o, dict):
            label = " ".join(str(o.get("label") or "").split())
            done = bool(o.get("done"))
            drop = bool(o.get("drop"))
        else:
            label = " ".join(str(o or "").split())
            done = drop = False
        if not label:
            continue
        if done and done_taken:
            done = False
        done_taken = done_taken or done
        # ⚠️ `drop` 是「不做了，把这条任务关掉」。它和 `done` 互斥：
        #    一条任务不可能既完成又放弃。两个都标的话只认 done。
        if done:
            drop = False
        out.append({"label": label[:MAX_LABEL], "done": done, "drop": drop})
        if len(out) >= MAX_OPTIONS:
            break
    return out


def parse_questions(raw: str, day: Day, n_commit: int,
                    n_routine: int) -> list[dict]:
    """从模型输出里抠出问题数组，按 kind 分别限数。

    模型有时会包一层 ```json 围栏、有时会在前后加一句寒暄，所以不能直接
    json.loads —— 退回到「取第一个 [ 到最后一个 ]」。

    ⚠️ 题号是**本地**算的（正文的 hash），不是模型给的。
       让模型保持 id 稳定是不现实的，而 apply_answers 要靠它把答案对回问题。
    ⚠️ 按 kind 分别限数而不是总共限数：不然模型多吐几条问责、就把常规类挤没了，
       而这两类是要按配额来的。
    """
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("["), text.rfind("]")
        if i < 0 or j <= i:
            raise ValueError("no JSON array in the model output") from None
        data = json.loads(text[i:j + 1])
    if not isinstance(data, list):
        raise ValueError("model output is not a JSON array")

    limits = {"commitment": n_commit, "routine": n_routine}
    seen = {"commitment": 0, "routine": 0}
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in limits or seen[kind] >= limits[kind]:
            continue
        q = " ".join(str(item.get("text") or "").split())
        if not q:
            continue
        ev = " ".join(str(item.get("evidence") or "").split())
        ref = " ".join(str(item.get("ref") or "").split())

        fmt = str(item.get("format") or "").strip().lower()
        if fmt not in FORMATS:
            fmt = "yes_no"            # 认不出来的一律当二值，最保守
        opts = parse_options(item.get("options")) if fmt == "choice" else []
        if fmt == "choice" and len(opts) < MIN_OPTIONS:
            # 选项太少就不成其为选择题。降级成二值 ——
            # 宁可少一个变量，也别把「两个选项的单选」渲染出来。
            fmt, opts = "yes_no", []
        if fmt == "text":
            ref = ""                  # 文字题勾不了任务，ref 必须空

        seen[kind] += 1
        # 题号里带日期，这样答案落到别的天也能对回来（见中继的 answered_ids）
        digest = hashlib.sha1(q.encode("utf-8")).hexdigest()[:8]
        out.append({"id": f"{day.iso}-{digest}", "kind": kind, "format": fmt,
                    "text": q, "evidence": ev, "ref": ref, "options": opts})
    return out


def push_to_relay(payload: dict) -> bool:
    """把问题推到中继。返回是否成功。

    中继的地址和 key 复用 sources.phone_relay —— 跟 collect_phone_events.py
    和 collect_watch.py 读的是同一段配置。
    """
    import httpx

    cfg = (CFG.get("sources") or {}).get("phone_relay") or {}
    if not cfg.get("enabled"):
        print("  . phone_relay disabled (config.yaml -> sources.phone_relay.enabled)")
        return False
    base = (cfg.get("api_base") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    if not base or "REPLACE_WITH" in base or "REPLACE_WITH" in key:
        print("  ⚠️  phone_relay is enabled but still has placeholder values in config.yaml.")
        return False
    try:
        r = httpx.post(f"{base}/questions", json=payload,
                       headers={"X-API-Key": key}, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Could not reach the phone relay at {base}: {e}")
        return False
    if r.status_code != 200:
        print(f"  ⚠️  relay returned {r.status_code}: {r.text[:200]}")
        return False
    print(f"  OK  pushed to relay: {r.json().get('count')} question(s)")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="today")
    ap.add_argument("--dry-run", action="store_true", help="Print only, write nothing")
    ap.add_argument("--no-push", action="store_true", help="Skip the relay push")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate even if the result file already exists")
    args = ap.parse_args()

    day = Day.parse(args.date, TZ)
    header(f"Generate questions · {day.iso}")

    qcfg = CFG.get("questions") or {}
    if not qcfg.get("enabled", False):
        print("-> questions disabled (config.yaml -> questions.enabled)")
        return

    # 只对最近几天提问。补跑历史时冒出一堆过期问题没有任何意义 ——
    # 尤其是它们会覆盖掉「今天该问的」那一份。
    max_age = int(qcfg.get("max_age_days", 2))
    today = datetime.now(TZ).date()
    if (today - day.d).days > max_age:
        print(f"-> {day.iso} is more than {max_age} day(s) old; no questions generated")
        return

    out = STATE / f"questions-{day.iso}.json"
    if out.exists() and not args.force:
        # ⚠️ 水位线修复后同一天可能被处理多次。已经生成过就别再调一次模型 ——
        #    重复生成会覆盖掉手机可能已经收到的那批题，还白花钱。
        print(f"-> already generated: {out} (use --force to regenerate)")
        return

    # ⚠️ 默认配额从 7+5 降到 4+3。
    #
    #    12 道早上答、再加上晚上收尾那 10 道，"每天问二十多道"的结果是
    #    回答退化成「还行」「一致」—— 提的问题越多，收到的信息越少。
    #    7 道是能在一分半里认真答完的量。
    n_commit = int(qcfg.get("commitment", 4))
    n_routine = int(qcfg.get("routine", 3))

    # ⚠️ 日记缺失**不再直接放弃**。
    #
    # 原来的逻辑是：日记里的 `### Adjustments for Tomorrow` 是问责题的
    # 核心素材，没有它就等于没有上下文，宁愿不生成。
    #
    # 但日记由服务器写，而 23:00 的流水线跑在服务器写日记之前 —— 于是
    # 「每天早上 7 条问责题」在正常作息下**永远生成不出来**（10-17、10-18
    # 就是这么断的）。现在改成：日记在就用；不在就用任务表 + 时间线兜底，
    # 并在 prompt 里明说这次没有日记。少一点上下文，也好过整条问责链断掉。
    journal = VAULT / "05-Journal" / f"{day.iso}.md"
    if journal.exists():
        adjustments = read_journal_section(day, H_ADJUST)
    else:
        adjustments = []
        print(f"-> {day.iso} 的日记还没生成（{journal.name} 不在）")
        print("   用任务表 + 时间线兜底生成，不等日记")

    tasks = read_open_tasks()
    # 常规类是从**数据**里出的，不依赖日记里的承诺或任务表 —— 所以只有两类
    # 素材全空、且常规配额也是 0 的时候才真的没得问。
    if not adjustments and not tasks and n_routine <= 0:
        print("-> nothing to ask about (no journal adjustments, no open tasks)")
        return
    print(f"-> Material: {len(adjustments)} adjustment(s), {len(tasks)} open task(s)")
    print(f"-> Quota: {n_commit} commitment + {n_routine} routine")

    digest = data_digest(day)
    if not journal.exists():
        digest += ("\n- Note: this day's journal is not written yet; the questions "
                   "below are based on the task list and the timeline only.")
    lang = str(qcfg.get("language", "zh")).lower()
    system = SYSTEM_PROMPT.replace(
        "__LANG_NAME__",
        "Chinese (简体中文)" if lang.startswith("zh") else "English")
    user = build_user_prompt(
        day, adjustments, tasks, digest, n_commit, n_routine,
        notes=read_notes(day),
        sinks=read_time_sinks(day),
        prev_qa=read_previous_qa(day),
        task_ages={t: task_age_days(t, day) for t in tasks},
    )

    ledger = CostLedger(CFG["budget"]["ledger_path"])
    llm = LLM(cfg=CFG, ledger=ledger)
    print(f"-> Calling {llm.provider} / {llm._model_name()}")
    try:
        raw = llm.complete(system=system, user=user, stage="questions",
                           run_id=new_run_id())
    except (BudgetExceeded, TruncatedOutput) as e:
        # 退出码非 0 → run_daily 不推进水位线 → 那天会重跑。
        # 提问失败不该让整天的日记白写，所以这里**只警告不中断**，
        # 由调用方决定。见 run_daily.ps1 里那一步用 Invoke-Step 包着。
        print(f"❌ {e}")
        sys.exit(1)

    try:
        items = parse_questions(raw, day, n_commit, n_routine)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"❌ could not parse the model output: {e}")
        print(f"   first 300 chars: {raw[:300]!r}")
        sys.exit(1)

    if not items:
        print("❌ the model returned no usable questions")
        sys.exit(1)

    for kind, label in (("commitment", "问责"), ("routine", "常规")):
        group = [x for x in items if x["kind"] == kind]
        print(f"  ── {label}（{len(group)} 条）")
        for it in group:
            fmt = it["format"]
            tag = {"yes_no": "[二值]", "choice": "[选择]", "text": "[文字]"}[fmt]
            print(f"     {tag} {it['text']}")
            if it["evidence"]:
                print(f"           └ {it['evidence']}")
            for o in it.get("options") or []:
                # 只有带 ref 的题才真能勾任务 —— 常规题就算标了 done 也无处可勾
                tick = o["done"] and bool(it.get("ref"))
                print(f"           · {o['label']}" + ("   ← 选中会勾掉任务" if tick else ""))

    payload = {
        "day": day.iso,
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "questions": items,
    }

    if args.dry_run:
        print("-> dry run, nothing written or pushed")
        return

    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  OK  Written: {out}")

    if args.no_push:
        print("-> --no-push, not sent to the relay")
        return
    if qcfg.get("push", True):
        push_to_relay(payload)


if __name__ == "__main__":
    main()
