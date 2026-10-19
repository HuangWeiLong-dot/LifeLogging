"""笔记本侧：把「写日记需要的一切」推给服务器。

服务器会在那边调 AI 写正文，然后把正文回传，我们在本机组装进 vault。
切分点选在这里，是因为**服务器不需要看见 vault** —— 它只要一堆数据就能写，
而 vault 的读写（保留手写的 `## Notes`、写副笔记、更新任务表）全留在本机，
不用做双向同步。

推上去三样东西：

  timeline    normalize 产出的当天时间轴（服务器靠它组 prompt）
  carryover   昨天日记里未完成的任务（服务器写「Yesterday's unfinished」要用）
  tasks       04-Tasks/Tasks.md 的 open 项（提问机器人要用）

**`final` 标记是这套东西的关键。** ActivityWatch 保留完整历史，所以「读 D 那天的
AW」在任何时候读到的都是完整的一天 —— 但笔记本只在开机时才推。如果它 18:00
就关机了，服务器手里那份只到 18:00。所以只有在「这次处理的这一天已经过完」时
才打 `final`（由本脚本按 `now >= 那天的结束时刻` 自己判断），服务器见到 `final`
才会拿去写日记。**宁可不写，也不拿半截数据写。**

用法：
  python scripts/push_journal_input.py --date 2026-10-14
  python scripts/push_journal_input.py --date today --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Day, header, load_config  # noqa: E402
from generate_journal import read_carryover  # noqa: E402
from generate_questions import read_open_tasks  # noqa: E402
import qa_client  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])
STATE = Path(CFG["state_dir"])


def relay() -> tuple[str, str] | None:
    cfg = (CFG.get("sources") or {}).get("phone_relay") or {}
    if not cfg.get("enabled"):
        print("-> phone relay disabled (config.yaml -> sources.phone_relay.enabled)")
        return None
    base = (cfg.get("api_base") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    if not base or "REPLACE_WITH" in base or "REPLACE_WITH" in key:
        print("  ⚠️  phone_relay is enabled but still has placeholder values in config.yaml.")
        return None
    return base, key


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="today")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不推")
    args = ap.parse_args()

    day = Day.parse(args.date, TZ)
    header(f"Push journal input · {day.iso}")

    tl_path = STATE / f"timeline-{day.iso}.json"
    if not tl_path.exists():
        print(f"FAIL timeline not found {tl_path}")
        print(f"   Run first: python scripts/normalize.py --date {day.iso}")
        sys.exit(1)
    timeline = json.loads(tl_path.read_text(encoding="utf-8"))

    carryover = read_carryover(day)
    tasks = read_open_tasks()

    # ── 相关历史（长期记忆 · 检索层）──
    #
    # ⚠️ **必须在这里做，不能放到服务器上。**
    #    索引在 state/events.db，日记正文在 vault/ 里 —— 两样都在笔记本上。
    #    而 `build_user_prompt` 跑在服务器（close_day），它看不到这些。
    #    所以检索结果跟着 payload 一起推过去。
    history: list[dict] = []
    try:
        import memory

        note_texts = [str(n.get("text") or "")
                      for n in (timeline.get("notes") or [])]
        q = memory.build_query(timeline, notes=note_texts)
        history = memory.search(q, 5) if q else []
    except Exception as e:  # noqa: BLE001
        # 索引坏了/没建过都不该拦住推输入 —— 没有历史是退化，不是失败。
        print(f"  ⚠️  历史检索失败，本次不带历史：{type(e).__name__}: {e}")

    # ── 当天问答（今天作答的 + 昨天作答的）──
    #
    # ⚠️ 答案在中继里只有 `<题号>|<答案>|<备注>`，问题原文要靠 /qa 合并。
    #    中继不可用时返回 None，这里退化成空列表，不打断推送 —— 日记少了
    #    问答是退化，不是失败。
    prev_day = (day.d - timedelta(days=1)).isoformat()
    qa_payload = qa_client.fetch(day.iso)
    prev_qa_payload = qa_client.fetch(prev_day)
    qa = (qa_payload or {}).get("qa") or []
    previous_day_qa = (prev_qa_payload or {}).get("qa") or []
    previous_day_recap = qa_client.fetch_previous_recap(prev_day)
    if qa_payload:
        qa_client.save_local(day.iso, qa_payload)
    if prev_qa_payload:
        qa_client.save_local(prev_day, prev_qa_payload)

    # ⚠️ 这一行是整套东西的关键。见模块开头的说明 —— 没有它，服务器会拿
    #    笔记本 18:00 那次推送的半截数据去写一整天的日记。
    final = datetime.now(TZ) >= day.end_local
    payload = {
        "day": day.iso,
        "final": final,
        "pushed_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "timeline": timeline,
        "carryover": carryover,
        "tasks": tasks,
        "history": history,
        "qa": qa,
        "previous_day_qa": previous_day_qa,
        "previous_day_recap": previous_day_recap,
    }

    print(f"-> timeline: {len(timeline.get('blocks') or [])} blocks, "
          f"{timeline.get('total_hours')}h")
    print(f"-> carryover: {len(carryover)}, tasks: {len(tasks)}")
    print(f"-> history: {len(history)} 条"
          + ("" if history else "  （没有召回相关的历史）"))
    print(f"-> qa: {len(qa)} today / {len(previous_day_qa)} yesterday"
          + ("" if (qa or previous_day_qa) else "  （中继没有 /qa 或没答过）"))
    print(f"-> final={final}"
          + ("" if final else f"  （{day.iso} 还没过完，服务器不会据此写日记）"))

    if args.dry_run:
        print("-> dry run, not pushed")
        return

    conn = relay()
    if conn is None:
        return
    base, key = conn
    try:
        r = httpx.post(f"{base}/journal/inputs", headers={"X-API-Key": key},
                       json=payload, timeout=60.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Could not reach the phone relay at {base}: {e}")
        return
    if r.status_code != 200:
        print(f"  ⚠️  relay returned {r.status_code}: {r.text[:200]}")
        return
    print(f"  OK  pushed ({r.json().get('detail')})")


if __name__ == "__main__":
    main()
