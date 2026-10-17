#!/usr/bin/env python3
"""长期记忆 · 第二层「整合」：把一周压成一份周回顾。

**它解决什么**

    检索层解决「它不知道你以前说过什么」；这一层解决「它没有一个稳定的你」。

    一周的记录（日记 + 你的笔记 + 你的回答）压成一份东西，供两个用途：
      · 给人看：`vault/07-Reviews/<年>-W<周>.md`
      · 给模型用：`state/weeks/<年>-W<周>.json` 里的**结构化事实**

**关键：产物不能只是散文**

    散文读完就没了。真正能复用、能注入的是**稳定事实**：
    作息、常去的地方、正在推进的项目、反复出现的人、反复出现的困扰。

    所以模型必须输出 JSON，而且**每条事实都要带 `days`** —— 哪几天的记录支持
    这句话。没有依据的事实一律丢掉（见 parse_review）。

**为什么优先吃笔记和回答，而不是日记正文**

    日记正文是**模型自己写的**。拿 AI 的输出喂 AI，错误会被一层层放大，
    而且越来越难发现。所以提示词里把「你写的笔记 / 你的回答」标成第一手，
    日记只作背景。

**为什么先只做周，不做画像**

    周整合稳定运行一个月之后，你才有「哪些事实真的稳定」的证据。
    在那之前做画像，等于把一两周的偶然当成规律固化下来。

用法：
  python scripts/weekly_review.py                    # 补最近缺的周（最多 4 周）
  python scripts/weekly_review.py --week 2026-W42    # 指定周
  python scripts/weekly_review.py --dry-run          # 只打印素材，不调模型
  python scripts/weekly_review.py --force            # 已有也重写
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
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
STATE = Path(CFG["state_dir"])
JOURNAL_DIR = VAULT / "05-Journal"
REVIEW_DIR = VAULT / "07-Reviews"
WEEK_DIR = STATE / "weeks"

_FM_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)

# 每篇日记最多喂多少字。一周 7 篇，全喂进去会把提示词撑得很大，
# 而周回顾要的是「模式」不是细节 —— 截断恰恰是对的。
JOURNAL_CAP = 3500
# 一周至少要有几篇日记才值得生成。太少的周做成回顾只会是噪音。
MIN_JOURNALS = 2

KINDS = ("sleep", "place", "project", "person", "trouble", "other")
KIND_LABEL = {
    "sleep": "作息", "place": "地点", "project": "项目",
    "person": "人", "trouble": "困扰", "other": "其它",
}

_SYSTEM = """你在为一个人的「周回顾」做准备。给你这一周的日记、他自己的笔记、
他自己的回答，以及每天的分类时长统计。

输出**一个 JSON 对象**，不要任何代码块之外的文字：

{
  "prose": "300–500 字的中文周回顾",
  "facts": [
    {"kind": "sleep|place|project|person|trouble|other",
     "text": "一句可复用的事实",
     "days": ["2026-10-14", "2026-10-16"]}
  ]
}

规则：

1. **facts 只写这几天的记录真的支持的事**，每条必须带 `days`，写明是哪几天的
   记录支持它。写不出依据的就不要写。
2. facts 要的是**稳定的模式**，不是流水账：
   - sleep：几点睡几点起、周末是否偏移
   - place：常去的地方以及去那儿干嘛
   - project：正在推进的事、反复出现的主题
   - person：反复出现的人
   - trouble：反复出现的困扰（「又熬夜」「又在赶截止」）
   没有任何一条成立，就少写几条 —— **宁可空着，不要凑数**。
3. **「他的笔记」和「他的回答」是第一手材料，比日记正文可靠。** 两者冲突时，
   以第一手为准。日记是模型写的，可能记错。
4. prose 用平实的中文，不要打气、不要评价好坏、不要给建议、不要列清单。
5. 不要复述日记，不要罗列时间块。"""


# ── 周的计算 ────────────────────────────────────────────────────────
def week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(year: int, week: int) -> tuple[date, date]:
    return (date.fromisocalendar(year, week, 1),
            date.fromisocalendar(year, week, 7))


def parse_week_key(s: str) -> tuple[int, int]:
    m = re.match(r"^(\d{4})-W(\d{1,2})$", (s or "").strip())
    if not m:
        raise ValueError(f"周格式应为 2026-W42，收到 {s!r}")
    return int(m.group(1)), int(m.group(2))


def last_completed_week(today: date) -> tuple[int, int]:
    """上一个**已经过完**的 ISO 周。"""
    monday = today - timedelta(days=today.isoweekday() - 1)
    y, w, _ = (monday - timedelta(days=7)).isocalendar()
    return y, w


def review_paths(key: str) -> tuple[Path, Path]:
    return REVIEW_DIR / f"{key}.md", WEEK_DIR / f"{key}.json"


# ── 素材 ────────────────────────────────────────────────────────────
def _strip_fm(text: str) -> str:
    return _FM_RE.sub("", text).strip()


def collect(mon: date, sun: date) -> dict:
    """把这一周能用的素材收齐。"""
    journals: list[dict] = []
    stats: list[dict] = []
    for i in range(7):
        d = mon + timedelta(days=i)
        ds = d.isoformat()

        p = JOURNAL_DIR / f"{ds}.md"
        if p.exists():
            try:
                body = _strip_fm(p.read_text(encoding="utf-8"))
            except OSError:
                body = ""
            if body:
                journals.append({"day": ds, "text": body[:JOURNAL_CAP]})

        tl = STATE / f"timeline-{ds}.json"
        if tl.exists():
            try:
                t = json.loads(tl.read_text(encoding="utf-8"))
                stats.append({
                    "day": ds,
                    "by_category": t.get("by_category") or {},
                    "total_hours": t.get("total_hours"),
                    "screen_minutes": (t.get("phone") or {}).get("screen_minutes"),
                })
            except (OSError, json.JSONDecodeError):
                pass

    notes: list[dict] = []
    answers: list[dict] = []
    try:
        import sqlite3

        conn = sqlite3.connect(STATE / "events.db")
        conn.execute("PRAGMA busy_timeout=5000;")
        try:
            for day, text, ts in conn.execute(
                    """SELECT day_local, value_text, ts_utc FROM events
                       WHERE domain='manual' AND day_local BETWEEN ? AND ?
                       ORDER BY ts_utc""", (mon.isoformat(), sun.isoformat())):
                if str(text or "").strip():
                    notes.append({"day": day, "text": str(text).strip()})
            for day, value_text, ts in conn.execute(
                    """SELECT day_local, value_text, ts_utc FROM events
                       WHERE domain='question' AND metric='answer'
                         AND day_local BETWEEN ? AND ?
                       ORDER BY ts_utc""", (mon.isoformat(), sun.isoformat())):
                parts = str(value_text or "").split("|", 2)
                if len(parts) < 2:
                    continue
                note = parts[2] if len(parts) > 2 else ""
                text = f"{parts[1]} {note}".strip()
                if text:
                    answers.append({"day": day, "text": text})
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  读事件表失败（只用手上的日记）：{type(e).__name__}: {e}")

    return {"journals": journals, "notes": notes,
            "answers": answers, "stats": stats}


def build_user_prompt(data: dict, mon: date, sun: date) -> str:
    lines = [f"# 周回顾素材：{mon.isoformat()} ~ {sun.isoformat()}", ""]

    if data["notes"]:
        lines.append("## 他自己的笔记（第一手）")
        lines.append("")
        for n in data["notes"][:40]:
            lines.append(f"- [{n['day']}] {n['text']}")
        lines.append("")

    if data["answers"]:
        lines.append("## 他自己的回答（第一手）")
        lines.append("")
        for a in data["answers"][:60]:
            lines.append(f"- [{a['day']}] {a['text']}")
        lines.append("")

    if data["stats"]:
        lines.append("## 每天的分类时长")
        lines.append("")
        lines.append("| 日期 | 合计 | 分类 | 屏幕 |")
        lines.append("|---|---|---|---|")
        for s in data["stats"]:
            cats = " / ".join(f"{k} {v}h" for k, v in
                              (s.get("by_category") or {}).items())
            screen = (f"{s['screen_minutes']:.0f}min"
                      if s.get("screen_minutes") else "—")
            lines.append(f"| {s['day']} | {s.get('total_hours')}h | {cats} | {screen} |")
        lines.append("")

    if data["journals"]:
        lines.append("## 日记正文（模型写的，作背景，可信度低于上面的第一手材料）")
        lines.append("")
        for j in data["journals"]:
            lines.append(f"### {j['day']}")
            lines.append("")
            lines.append(j["text"])
            lines.append("")
    else:
        lines.append("（这周没有日记正文）")

    lines.append("---")
    lines.append("现在输出 JSON。")
    return "\n".join(lines)


# ── 解析与落盘 ──────────────────────────────────────────────────────
def _extract_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def parse_review(raw: str, valid_days: set[str]) -> tuple[str, list[dict], int]:
    """解析模型输出。返回 (散文, 合格的事实, 被丢掉的事实数)。

    ⚠️ **没有 days、或者 days 里没有一个落在本周内的，一律丢掉。**
       这是这条链上最重要的一道闸：一条「感觉你最近很累」这种没有依据的断言
       一旦进了周回顾，下个月的画像会继承它，再往下的日记又会继承画像 ——
       错误会被一层层放大，而且越来越难发现。
    """
    obj = _extract_json(raw)
    if obj is None:
        return "", [], 0

    prose = str(obj.get("prose") or "").strip()
    raw_facts = obj.get("facts")
    facts: list[dict] = []
    dropped = 0
    if isinstance(raw_facts, list):
        for f in raw_facts:
            if not isinstance(f, dict):
                dropped += 1
                continue
            text = " ".join(str(f.get("text") or "").split())
            kind = str(f.get("kind") or "other").strip().lower()
            days = [str(d) for d in (f.get("days") or [])
                    if str(d) in valid_days]
            if not text or kind not in KINDS or not days:
                dropped += 1
                continue
            facts.append({"kind": kind, "text": text,
                          "days": sorted(set(days))})
    return prose, facts, dropped


def render_markdown(key: str, mon: date, sun: date, prose: str,
                    facts: list[dict], cost: float) -> str:
    lines = [
        "---",
        f'week: "{key}"',
        f'range: "{mon.isoformat()} .. {sun.isoformat()}"',
        f'generated_at: "{datetime.now(TZ).strftime("%Y-%m-%d %H:%M")}"',
        f"ai_cost: {cost:.6f}",
        "tags:",
        "  - review",
        "  - weekly",
        "---",
        "",
        f"# {key}（{mon.isoformat()} ~ {sun.isoformat()}）",
        "",
        "## 这周",
        "",
        prose or "（没有生成散文）",
        "",
        "## 稳定下来的事实",
        "",
        "> 每条都带依据（哪几天的记录支持它）。没有依据的断言被丢掉了 ——",
        "> 这一层最容易出的问题是「读起来很像，但其实没根据」。",
        "",
    ]
    if facts:
        lines += ["| 类别 | 事实 | 依据 |", "|---|---|---|"]
        for f in facts:
            days = "、".join(d[5:] for d in f["days"])
            lines.append(f"| {KIND_LABEL.get(f['kind'], f['kind'])} "
                         f"| {f['text']} | {days} |")
    else:
        lines.append("（这周没有足够依据支撑的稳定事实）")
    lines.append("")
    return "\n".join(lines)


def generate(key: str, mon: date, sun: date, data: dict,
             dry_run: bool, force: bool) -> bool:
    md_path, json_path = review_paths(key)
    if md_path.exists() and not force:
        print(f"  . {key}: 已经生成过了")
        return False

    if len(data["journals"]) < MIN_JOURNALS:
        print(f"  . {key}: 只有 {len(data['journals'])} 篇日记"
              f"（至少要 {MIN_JOURNALS} 篇），跳过")
        return False

    user = build_user_prompt(data, mon, sun)
    print(f"-> {key}: {len(data['journals'])} 篇日记、"
          f"{len(data['notes'])} 条笔记、{len(data['answers'])} 个回答，"
          f"prompt {len(user):,} 字符")

    if dry_run:
        print("   (--dry-run: 不调 AI、不落盘)")
        return False

    ledger = CostLedger(CFG["budget"]["ledger_path"])
    before = len(ledger.entries())
    llm = LLM(cfg=CFG, ledger=ledger)
    try:
        out = llm.complete(system=_SYSTEM, user=user,
                           stage="weekly_review", run_id=new_run_id())
    except BudgetExceeded as e:
        print(f"  ⚠️  预算到顶，跳过 {key}：{e}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  {key} 生成失败：{type(e).__name__}: {e}")
        return False

    entries = ledger.entries()
    cost = sum(float(e.get("cost") or 0) for e in entries[before:])

    valid_days = {(mon + timedelta(days=i)).isoformat() for i in range(7)}
    prose, facts, dropped = parse_review(out, valid_days)
    if not prose and not facts:
        # ⚠️ 解析失败不能静默写成空文件 —— 那会让这一周永久地被标成「已完成」。
        print(f"  ⚠️  {key}: 模型输出没解析出内容，不落盘。"
              f"原始输出前 200 字：{out[:200]!r}")
        return False
    if dropped:
        print(f"  . {key}: 丢掉了 {dropped} 条没有依据的事实")

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    WEEK_DIR.mkdir(parents=True, exist_ok=True)

    json_path.write_text(json.dumps(
        {"week": key, "from": mon.isoformat(), "to": sun.isoformat(),
         "facts": facts, "prose": prose,
         "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
         "ai_cost": round(cost, 6)},
        ensure_ascii=False, indent=2), encoding="utf-8")

    md_path.write_text(render_markdown(key, mon, sun, prose, facts, cost),
                       encoding="utf-8")
    print(f"  OK  {key}: {len(facts)} 条事实 → {md_path}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="长期记忆 · 周整合")
    ap.add_argument("--week", default="", help="如 2026-W42；默认补最近缺的周")
    ap.add_argument("--max-weeks", type=int, default=4,
                    help="自动模式最多往回补几周")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="已有也重写")
    args = ap.parse_args()

    header("Weekly review")
    today = datetime.now(TZ).date()

    if args.week:
        weeks = [parse_week_key(args.week)]
    else:
        ly, lw = last_completed_week(today)
        weeks = []
        for i in range(max(1, args.max_weeks)):
            wd = date.fromisocalendar(ly, lw, 1) - timedelta(days=7 * i)
            y, w, _ = wd.isocalendar()
            weeks.append((y, w))

    did = 0
    for (y, w) in weeks:
        mon, sun = week_bounds(y, w)
        key = week_key(mon)
        data = collect(mon, sun)
        if generate(key, mon, sun, data, args.dry_run, args.force):
            did += 1

    if not did:
        print("  . 没有需要生成的周")
    return 0


if __name__ == "__main__":
    sys.exit(main())
