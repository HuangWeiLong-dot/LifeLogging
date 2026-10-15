"""把手机上答的「做了 / 没做」回写进 vault。

**为什么要有它**

    提问机器人把问题问出去了，答案也经中继落进了事件表 —— 但如果没人把答案
    拿出来看，它就只是数据库里的一堆字符串。这个脚本把「做了」变成任务表上
    真实的勾选。

**它做什么**

    · 读事件表里 ``domain=question`` / ``metric=answer`` 的行。**不需要新的
      采集链路** —— collect_phone_events.py 早就把当天的手机事件（含答案）
      一起拉回来了。
    · 同一题重复回答时**以最后一次为准**。
    · 答「做了」且能对上任务表里某条 ``- [ ]`` 的 → 从 Open 挪进 Done，
      标成 ``- [x]``，后面缀一个记录来源的 HTML 注释。
    · 全部答案都打印出来，**对不上的也打印** —— 别让它悄悄消失。

**为什么重答会产生新行而不是改旧行**

    事件表是只追加的，去重键含 ``value_text``，所以同一题答两次就是两行。
    这是有意的：答错了可以改，历史留着。取最后一条是这个脚本的责任。

用法：
  python scripts/apply_answers.py                 # 默认处理「昨天」的日记
  python scripts/apply_answers.py --date 2026-10-14
  python scripts/apply_answers.py --no-write      # 只打印，不动 Tasks.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Day, EventsClient, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])
VAULT = Path(CFG["vault_path"])
STATE = Path(CFG["state_dir"])
TASKS = VAULT / "04-Tasks" / "Tasks.md"

# 「互相包含」这条兜底路径要求的最小公共长度。太短会把两条无关的任务对上 ——
# 而这个脚本的动作是**把任务标成完成**，错一次就是骗自己一次。
MIN_PARTIAL = 12


def _norm(s: str) -> str:
    """归一化，用来比对「问题依据的那一行」和「任务表里的那一行」。"""
    s = s.strip().lower()
    s = re.sub(r"^-\s*\[[ xX]\]\s*", "", s)
    return re.sub(r"[\s。．.,，;；:：!？?!]+", " ", s).strip()


def find_task(open_items: list[str], ref: str) -> str | None:
    """把问题依据的那一行对回任务表里的某条。

    先要求归一化后完全相等 —— 模型被要求逐字照抄，多数情况走这条。
    不行才退回「互相包含」，并且要求公共部分够长。
    """
    if not ref:
        return None
    target = _norm(ref)
    if not target:
        return None
    for item in open_items:
        if _norm(item) == target:
            return item
    for item in open_items:
        n = _norm(item)
        if len(target) >= MIN_PARTIAL and (target in n or n in target):
            return item
    return None


def read_section_items(heading: str, prefix: str) -> list[str]:
    """某个小节里各条的**正文**（去掉 ``- [ ]`` / ``- [x]`` 前缀）。

    返回正文而不是整行：整行拿去做 `- [x] {text}` 会写出
    ``- [x] - [ ] …`` 这种双重前缀。
    """
    if not TASKS.exists():
        return []
    out: list[str] = []
    inside = False
    for line in TASKS.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("## "):
            inside = s.startswith(heading)
            continue
        if inside and s.startswith(prefix):
            body = s[len(prefix):].strip()
            # 去掉我们写进去的那个 HTML 注释，否则正文里会一直挂着它
            body = re.sub(r"\s*<!--.*?-->\s*$", "", body).strip()
            if body and "nothing pending" not in body.lower():
                out.append(body)
    return out


def read_open_items() -> list[str]:
    return read_section_items("## Open", "- [ ]")


def read_done_items() -> list[str]:
    return read_section_items("## Done", "- [")


def read_answers(day: Day) -> dict[str, tuple[str, str, str]]:
    """读某一天那些问题的答案 → ``{题号: (yes|no, 备注, ts_utc)}``，重答取最后一条。"""
    client = EventsClient(CFG)
    if not client.ping():
        print(f"FAIL events API is not running ({client.base}). Start it with:")
        print("   python -m middleware.events_api")
        sys.exit(1)

    rows = client.query(domain="question", metric="answer", limit=100_000)
    prefix = f"{day.iso}-"
    latest: dict[str, tuple[str, str, str]] = {}
    for r in rows:
        parts = str(r.get("value_text") or "").split("|", 2)
        if len(parts) < 2 or not parts[0].startswith(prefix):
            continue
        qid = parts[0]
        ts = str(r.get("ts_utc") or "")
        # ⚠️ 不假设返回顺序。接口没承诺按时间排，这里显式比较一次 ——
        #   「重答取最后一条」全靠这一行，靠错顺序就会把旧答案当成新的。
        if qid not in latest or ts >= latest[qid][2]:
            latest[qid] = (parts[1], parts[2] if len(parts) > 2 else "", ts)
    return latest


def mark_done(done: list[tuple[str, str]]) -> None:
    """把 Open 里的条目挪到 Done 并勾上。**行级改写**，其余内容原样保留。

    不重建整个文件：Tasks.md 里有用户手写的东西（Done 段的注释、
    Rolled up 那段 Dataview），全量重写很容易把没预料到的东西冲掉。
    """
    lines = TASKS.read_text(encoding="utf-8").splitlines()
    stamp = datetime.now(TZ).strftime("%Y-%m-%d")
    targets = {_norm(t) for t, _ in done}

    out: list[str] = []
    done_at: int | None = None
    for line in lines:
        s = line.strip()
        if s.startswith("## Done"):
            done_at = len(out)
        if s.startswith("- [ ]") and _norm(s) in targets:
            continue                       # 从 Open 里摘掉
        out.append(line)

    entries = []
    for text, note in done:
        # 用 HTML 注释记来源：Obsidian 里不显示，但翻原文时看得出是手机确认的
        entries.append(f"- [x] {text}  <!-- {stamp} 手机确认"
                       + (f" · {note}" if note else "") + " -->")

    if done_at is None:
        out += ["", "## Done", ""] + entries + [""]
    else:
        at = done_at + 1
        # 跳过空行和那句「完成的直接移到这里」的注释
        while at < len(out) and (not out[at].strip() or out[at].strip().startswith("<!--")):
            at += 1
        # 尾随一个空行，否则下一个小节标题会紧贴着任务行
        out[at:at] = entries + [""]

    TASKS.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="yesterday",
                    help="问的是**哪一天的日记**（不是作答那天）")
    ap.add_argument("--no-write", action="store_true", help="只打印，不改 Tasks.md")
    args = ap.parse_args()

    day = Day.parse(args.date, TZ)
    header(f"Apply answers · {day.iso}")

    qfile = STATE / f"questions-{day.iso}.json"
    if not qfile.exists():
        print(f"-> no questions were generated for {day.iso} ({qfile.name} missing)")
        return
    try:
        questions = json.loads(qfile.read_text(encoding="utf-8")).get("questions") or []
    except (OSError, json.JSONDecodeError):
        print(f"FAIL could not read {qfile}")
        sys.exit(1)
    by_id = {q.get("id"): q for q in questions if isinstance(q, dict)}

    answers = read_answers(day)
    if not answers:
        print(f"-> no answers for {day.iso} yet")
        return
    print(f"-> {len(answers)} answer(s) for {len(questions)} question(s)")

    open_items = read_open_items()
    done_items = read_done_items()
    to_close: list[tuple[str, str]] = []
    unmatched: list[str] = []

    for qid, (val, note, _ts) in sorted(answers.items(), key=lambda kv: kv[1][2]):
        q = by_id.get(qid) or {}
        text = q.get("text") or f"(题号 {qid} 不在本地问题文件里 —— 可能是别的机器生成的)"
        mark = "做了" if val == "yes" else "没做"
        print(f"  [{mark}] {text}" + (f"\n         备注：{note}" if note else ""))
        if val != "yes":
            continue
        ref = q.get("ref") or ""
        if not ref:
            continue
        hit = find_task(open_items, ref)
        if hit:
            to_close.append((hit, note))
        elif find_task(done_items, ref):
            # 已经勾过了（比如这个脚本今天已经跑过一次）。不是问题，别报警，
            # 否则每天第二次运行都会刷一条吓人的「对不上」。
            pass
        else:
            unmatched.append(ref)

    for ref in unmatched:
        print(f"  ⚠️  答了「做了」但对不上任务表里任何一条：{ref}")

    if not to_close:
        print("-> nothing to tick off")
        return

    if args.no_write:
        print("-> --no-write, Tasks.md untouched. Would move to Done:")
        for text, _ in to_close:
            print(f"     {text}")
        return

    mark_done(to_close)
    print(f"-> {len(to_close)} item(s) moved from Open to Done in {TASKS.name}")


if __name__ == "__main__":
    main()
