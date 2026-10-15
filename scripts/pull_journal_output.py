"""笔记本侧：取回服务器写好的日记正文，在本机组装进 vault。

服务器只回**正文**和一份用量统计 —— frontmatter、手写的 `## Notes`、副笔记、
`06-AI-Sessions` 的记录全在这里拼。这样切的好处是服务器不需要看见 vault：
没有双向同步，也就没有「两边同时写同一个库」那种冲突。

**「还没生成」不是错误。** 笔记本完全可能醒得比服务器生成得早（凌晨关机、
早上开机，而那天还没被关账）。那种情况这里安静退出、退出码 0，下一次运行
自然会取到。把它当失败会让流水线报红，而实际上什么都没坏。

用法：
  python scripts/pull_journal_output.py --date 2026-10-14
  python scripts/pull_journal_output.py --date today
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
from generate_journal import (  # noqa: E402
    H_NOTES,
    VAULT,
    actual_sources,
    compose,
    extract_section,
    read_carryover,
    write_ai_session,
)
from write_notes import write_all as write_notes_all  # noqa: E402

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


def pull_one(day: Day, base: str, key: str) -> str:
    """取一天并组装。返回 'written' / 'not-ready' / 'already' / 'failed'。"""
    try:
        r = httpx.get(f"{base}/journal/output", headers={"X-API-Key": key},
                      params={"day": day.iso}, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Could not reach the phone relay at {base}: {e}")
        return "failed"
    if r.status_code == 404:
        # 不是错误，见模块开头
        return "not-ready"
    if r.status_code != 200:
        print(f"  ⚠️  relay returned {r.status_code}: {r.text[:200]}")
        return "failed"

    payload = r.json()
    body = payload.get("body") or ""
    stats = dict(payload.get("stats") or {})
    warnings = stats.pop("warnings", []) or []
    run_id = str(stats.get("run_id") or "")

    # 幂等：同一版正文已经组装过了就跳过。不跳的话每次运行都会重写一遍日记、
    # 并往 06-AI-Sessions 里再append 一条同样的记录。
    marker = STATE / f"journal-pulled-{day.iso}.txt"
    if run_id and marker.exists() and marker.read_text(encoding="utf-8").strip() == run_id:
        return "already"

    tl_path = STATE / f"timeline-{day.iso}.json"
    if not tl_path.exists():
        # 没有 timeline 就组装不出来（副笔记也要它）。但**不能退出** ——
        # 这个脚本现在按天循环调用，一天坏了不该让其余几天也停。
        print(f"  ⚠️  {day.iso}: timeline 不在（{tl_path.name}），跳过组装")
        return "failed"
    tl = json.loads(tl_path.read_text(encoding="utf-8"))

    # sources 由**本机**算：服务器手里没有「这一天到底哪些数据源有数据」的完整
    # 视图（比如桌面那半边是本机才知道的）。不去回传它，各自算各自的。
    stats.setdefault("sources", actual_sources(tl))
    stats.setdefault("generated_at", datetime.now(TZ).strftime("%Y-%m-%d %H:%M"))

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
    print("OK  Appended to the AI session log")

    carryover = read_carryover(day)
    write_notes_all(day.iso, tl, carryover)
    print("OK  Side notes written")

    if run_id:
        marker.write_text(run_id, encoding="utf-8")
    return "written"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    # 默认往前看几天：AI 那一步在服务器上、什么时候生成不受本机控制，
    # 本机完全可能醒得比它早。多扫几天，关机错过的那份下次开机还能捡回来。
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()

    conn = relay()
    if conn is None:
        return
    base, key = conn

    if args.date:
        days = [Day.parse(args.date, TZ)]
    else:
        today = Day.parse("today", TZ)
        days = [Day.parse((today.d - timedelta(days=i)).isoformat(), TZ)
                for i in range(args.days - 1, -1, -1)]

    header(f"Pull journal output · {' '.join(d.iso for d in days)}")

    counts = {"written": 0, "not-ready": 0, "already": 0, "failed": 0}
    for day in days:
        print(f"-> {day.iso}")
        counts[pull_one(day, base, key)] += 1

    print(f"-> written {counts['written']}, already {counts['already']}, "
          f"not ready {counts['not-ready']}, failed {counts['failed']}")


if __name__ == "__main__":
    main()
