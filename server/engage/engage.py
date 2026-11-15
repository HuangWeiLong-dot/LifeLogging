#!/usr/bin/env python3
"""App 侧「带锚点的问题」生成器 —— ASSOC 层的 LLM 部分。

**和固定七维的分工**

    七维（心情/体力/睡眠/人际/吃饭/休息/学习）是**每天必问**的，不依赖数据，
    由 App 本地生成。这里只做另一件事：

        **看着当天真实发生的事，写出一两个只有本人能回答的问题。**

    素材是当天的窗口块、听过的歌、去过的地点、已有的回答。模型要做的不是
    复述数据，而是把数据变成一句「你昨天 21:40 在听 X，当时在想什么」——

**两条硬规则（飞书那批废题就是栽在这）**

    1. **必须有锚点。** 每道题都要带一个真实的名字/时刻/数字。没有锚点的
       问题一律丢弃 —— 「今天感觉如何」这种问法问一百天也是同一个答案。
    2. **没有素材就返回空。** 数据平淡的一天，AI 不该硬凑问题。空列表是
       合法输出，App 会退回固定七维。

**成本**

    一天最多调用两次（由 timer 控制），走的是同一份 `cost_ledger.jsonl`。
    素材先压成一段短摘要再进 prompt，不把原始 JSON 丢进去。

用法（服务器上）：
  python server/engage/engage.py --day today
  python server/engage/engage.py --day 2026-11-15 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from common import Day, LLM, CostLedger, load_config, new_run_id  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])
DATA = Path(os.environ.get("LIFELOGGING_DATA", CFG["state_dir"]))

MAX_QUESTIONS = 3

SYSTEM_PROMPT = """You write at most three questions for a person reviewing their own day.

You are given a compact digest of what was actually recorded: window titles,
what they listened to, where they were, and what they already answered today.

Rules, in order of importance:

1. EVERY question must quote a concrete anchor from the digest — a time
   ("21:40"), a title ("Blue in Green"), a place, or a number. If you cannot
   anchor a question, do not write it.
2. NEVER ask something the digest already answers. "How long were you on the
   computer" is not a question — the number is right there. Ask what the data
   cannot see: why, how it felt, what was in the way, whether it was worth it.
3. One sentence per question. No lists, no compound questions.
4. If nothing in the digest is worth asking about, return an empty array.
   A quiet day does not need a question.
5. Do not ask about mood, energy, sleep, social, food, rest or learning —
   those are asked separately every day.

Return ONLY a JSON object, no prose, no markdown fence:

{"questions": [
  {"persona": "OBSERVER|ADVOCATE|ROMANTICIST|IDEALIST",
   "text": "one anchored sentence, ending in a question mark",
   "hint": "one short line saying where the anchor came from",
   "anchor": "the exact string you quoted",
   "options": ["3-5 short answers that cover the realistic possibilities"]}
]}

The options are the point: a free-text box gets skipped, chips get answered.
Write options that are short (1-4 words), mutually exclusive, and specific to
this question — not generic ("Yes"/"No"/"Other"). They should read like the
person's own possible answers.
"""


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def build_digest(day: Day) -> str:
    """把当天数据压成一段短摘要。原始 JSON 不进 prompt。"""
    lines: list[str] = []

    bundle = DATA / f"journal-input-{day.iso}.json"
    if bundle.exists():
        try:
            data = json.loads(bundle.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        tl = data.get("timeline") or {}
        blocks = tl.get("blocks") or []
        if blocks:
            lines.append("## Screen blocks")
            for b in blocks[:40]:
                title = " / ".join(b.get("titles") or [])[:60]
                lines.append(
                    f"- {b.get('time','')} {b.get('hours',0)}h "
                    f"{b.get('category','')} {b.get('app','')[:18]} {title}".rstrip()
                )
        health = tl.get("health") or {}
        if health:
            bits = ", ".join(f"{k}={v}" for k, v in list(health.items())[:8])
            lines.append(f"\n## Health\n- {bits}")
        loc = tl.get("location") or []
        if loc:
            lines.append("\n## Places")
            for l in loc[:5]:
                lines.append(f"- {l.get('minutes',0):.0f} min @ {l.get('place','')}")
        notes = data.get("notes") or []
        if notes:
            lines.append("\n## Things they wrote")
            for n in notes[:10]:
                lines.append(f"- {str(n)[:160]}")

    events = _read_jsonl(DATA / f"phone-{day.iso}.jsonl")
    music = [e for e in events if e.get("metric") == "music"][-12:]
    if music:
        lines.append("\n## Music")
        for e in music:
            ts = str(e.get("ts_utc") or e.get("ts") or "")[11:16]
            meta = e.get("meta") or {}
            artist = meta.get("artist") or ""
            title = str(e.get("value_text") or "")[:60]
            lines.append(f"- {ts} {artist} — {title}".strip(" —"))

    answered = [
        e for e in events
        if e.get("domain") == "question" and e.get("metric") == "answer"
    ]
    if answered:
        lines.append("\n## Already answered today (do not repeat these)")
        for e in answered[-12:]:
            meta = e.get("meta") or {}
            q = str(meta.get("question") or "")[:80]
            a = str(e.get("value_text") or "").split("|")[1][:60] if "|" in str(e.get("value_text") or "") else ""
            lines.append(f"- Q: {q} → A: {a}")

    return "\n".join(lines).strip()


def generate(day: Day, dry_run: bool) -> list[dict]:
    digest = build_digest(day)
    if not digest:
        print("  . 当天没有任何可用的素材，不生成问题")
        return []

    if dry_run:
        print(digest[:1500])
        return []

    ledger = CostLedger(CFG["budget"]["ledger_path"])
    llm = LLM(cfg=CFG, ledger=ledger)
    raw = llm.complete(
        SYSTEM_PROMPT,
        f"Digest for {day.iso}:\n\n{digest}\n\nReturn the JSON object.",
        stage="engage",
        run_id=new_run_id(),
    )

    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        print("  ⚠️ 模型没有返回 JSON")
        return []
    try:
        obj = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        print("  ⚠️ 模型返回的 JSON 解析失败")
        return []

    out: list[dict] = []
    for i, q in enumerate(obj.get("questions") or []):
        if not isinstance(q, dict):
            continue
        text = str(q.get("text") or "").strip()
        anchor = str(q.get("anchor") or "").strip()
        # 硬规则：没有锚点、或者锚点根本不在摘要里 → 丢弃
        if not text or not anchor:
            continue
        if anchor not in digest:
            print(f"  ⚠️ 锚点「{anchor[:40]}」不在当天数据里，丢弃")
            continue
        opts = [str(o)[:40] for o in (q.get("options") or []) if str(o).strip()][:6]
        out.append({
            "id": f"{day.iso}-llm-{i + 1}",
            "options": opts,
            "day": day.iso,
            "persona": str(q.get("persona") or "OBSERVER")[:20],
            "control": "CHOICE" if opts else "TEXT",
            "text": text[:400],
            "hint": str(q.get("hint") or "")[:300],
            "layer": "assoc",
            "anchor": anchor[:200],
        })
        if len(out) >= MAX_QUESTIONS:
            break
    return out


def push(day: Day, questions: list[dict]) -> bool:
    relay = (CFG.get("sources") or {}).get("phone_relay") or {}
    base = (relay.get("api_base") or "").rstrip("/")
    key = relay.get("api_key") or ""
    if not base or not key:
        print("  ⚠️ sources.phone_relay 没配好，推不上去")
        return False
    try:
        r = httpx.post(
            f"{base}/engage/questions",
            json={"day": day.iso, "questions": questions},
            headers={"X-API-Key": key},
            timeout=20.0,
        )
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 推送到中继失败：{type(e).__name__}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="today")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    day = Day.parse(args.day, TZ)
    print(f"Engage · {day.iso}")

    questions = generate(day, args.dry_run)
    if args.dry_run:
        return 0
    if not questions:
        print("  . 这次没有值得问的（空列表是合法结果）")
        return 0

    for q in questions:
        print(f"  · [{q['persona']}] {q['text'][:90]}")

    if push(day, questions):
        print(f"OK  已推送 {len(questions)} 道")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
