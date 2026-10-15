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
import sys
from datetime import datetime
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
- Every question MUST be answerable with "yes" or "no". One clause, ONE thing. \
Never ask for a number, a list, or an explanation.
- NEVER use a time word - no 今天 / 昨天 / today / yesterday. The reader opens this \
the morning after, so any such word is either wrong or ambiguous and the whole \
batch reads as nonsense. Name the concrete event, time or number instead: \
"the 20:11 Tailscale key", "14:00-18:00", "the 426 app switches".
- Phrase it POSITIVELY, so that "yes / 做了" means the thing they wanted to happen \
happened. Never ask "did you avoid doing X" or "did you manage not to X" - a yes/no \
button cannot represent a negative question, and the answer becomes meaningless.
- Be precise rather than general. Quote the specific number, time or phrase you are \
basing it on. "Commit the repo in the first 15 minutes of the session" beats \
"work on the repo"; "a stretch over 90 minutes with no app switch" beats \
"stay focused".
- Do not moralise, do not congratulate, do not explain why the question matters. \
Ask the question and stop.
- If a commitment has already been carried over more than once, say how many days \
it has slipped - that is the whole point.
- Write the question and the evidence in __LANG_NAME__. Keep proper nouns, app \
names, file paths and numbers as they are.

The "ref" field is machine-read, so it has its own rules:
- kind="commitment": if the question is about one of the "Open tasks" or \
"Adjustments" bullets, copy that line into "ref" CHARACTER FOR CHARACTER exactly as \
it appears above. It gets matched against the task list to tick the item off, so a \
paraphrase silently breaks it.
- kind="commitment" based on the data alone, and every kind="routine": empty string.

Output ONLY a JSON array, no prose and no code fences:

[
  {"kind": "commitment" | "routine",
   "text": "<the yes/no question>",
   "evidence": "<the specific thing you are basing it on>",
   "ref": "<verbatim source line, or empty string>"}
]

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

    return "\n".join(lines) if lines else "(nothing recorded)"


def build_user_prompt(day: Day, adjustments: list[str], tasks: list[str],
                      digest: str, n_commit: int, n_routine: int) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {x}" for x in items) if items else "(none)"

    return f"""# Day to review: {day.iso} ({day.d.strftime('%A')})

## What they wrote in their own journal for that day

### Adjustments for Tomorrow (things they said they would do)
{bullets(adjustments)}

### Open tasks carried in the task list
{bullets(tasks)}

## What the recorded data says about that day

{digest}

---

Write {n_commit} questions of kind "commitment" and {n_routine} of kind "routine", \
for the morning after this day. Every one must be answerable with "yes" or "no"."""


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
        seen[kind] += 1
        # 题号里带日期，这样答案落到别的天也能对回来（见中继的 answered_ids）
        digest = hashlib.sha1(q.encode("utf-8")).hexdigest()[:8]
        out.append({"id": f"{day.iso}-{digest}", "kind": kind, "text": q,
                    "evidence": ev, "ref": ref})
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

    n_commit = int(qcfg.get("commitment", 7))
    n_routine = int(qcfg.get("routine", 5))

    adjustments = read_journal_section(day, H_ADJUST)
    tasks = read_open_tasks()
    # 常规类是从**数据**里出的，不依赖日记里的承诺或任务表 —— 所以只有两类
    # 素材全空、且常规配额也是 0 的时候才真的没得问。
    if not adjustments and not tasks and n_routine <= 0:
        print("-> nothing to ask about (no journal adjustments, no open tasks)")
        return
    print(f"-> Material: {len(adjustments)} adjustment(s), {len(tasks)} open task(s)")
    print(f"-> Quota: {n_commit} commitment + {n_routine} routine")

    digest = data_digest(day)
    lang = str(qcfg.get("language", "zh")).lower()
    system = SYSTEM_PROMPT.replace(
        "__LANG_NAME__",
        "Chinese (简体中文)" if lang.startswith("zh") else "English")
    user = build_user_prompt(day, adjustments, tasks, digest, n_commit, n_routine)

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
            print(f"     {it['text']}")
            if it["evidence"]:
                print(f"       └ {it['evidence']}")

    payload = {
        "day": day.iso,
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "questions": items,
    }

    if args.dry_run:
        print("-> dry run, nothing written or pushed")
        return

    out = STATE / f"questions-{day.iso}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  OK  Written: {out}")

    if args.no_push:
        print("-> --no-push, not sent to the relay")
        return
    if qcfg.get("push", True):
        push_to_relay(payload)


if __name__ == "__main__":
    main()
