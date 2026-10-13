"""成本护栏 —— 预算查询、预检闸门、报告、余额核对。

三个层次，从外到内：
  1. **厂商侧硬封顶**（唯一真正可靠的一层）
     DeepSeek 控制台可以设消费限额，或干脆控制账户余额。
     本地这些脚本都是「软」的 —— 它们只能阻止你自己启动的调用。
  2. **运行前预检**（本脚本的 gate）—— 流水线启动前查台账，超了就退出，一次请求都不发。
  3. **流水线内熔断** —— common.LLM 每调一次就记账，超限抛 BudgetExceeded，
     且 max_llm_calls_per_run 防止 agent 循环跑飞（超支的第一大原因）。

⚠️ 关于 DeepSeek 的对账：
   DeepSeek **没有** Anthropic 那种 usage_report / cost_report API。
   它只提供余额接口（/user/balance）。所以对账思路是
   「余额变化 + 本地台账」互相印证，而不是拉一张账单明细来逐条比。
   见 cmd_check_deepseek。

用法：
  python scripts/cost_guard.py status            # 今天花了多少
  python scripts/cost_guard.py gate              # 预检（超了返回退出码 1）
  python scripts/cost_guard.py report --days 30  # 近 30 天报表
  python scripts/cost_guard.py check-deepseek    # 余额 + 剩余可用天数
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CostLedger, header, is_peak, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])
LEDGER = CostLedger(CFG["budget"]["ledger_path"])


def _cap() -> float:
    return float(CFG["budget"]["daily_cap"])


def _cur() -> str:
    return CFG["budget"].get("currency", "CNY")


def cmd_status() -> int:
    b = CFG["budget"]
    cap, cur = _cap(), _cur()
    today = datetime.now(TZ).date()
    spent = LEDGER.spend_on(today, TZ)
    pct = spent / cap if cap else 0

    entries = [e for e in LEDGER.entries() if e.get("day") == today.isoformat()]
    hit = sum(int(e.get("cache_hit", 0)) for e in entries)
    miss = sum(int(e.get("cache_miss", 0)) for e in entries)
    out = sum(int(e.get("output", 0)) for e in entries)

    header(f"Cost ledger · {today.isoformat()}")

    bar_len = 40
    filled = int(min(pct, 1.0) * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)

    print(f"  [{bar}] {pct*100:.1f}%")
    print(f"  Spent    {spent:.4f} / {cap:.2f} {cur}")
    print(f"  Left     {max(0, cap - spent):.4f} {cur}")
    print(f"  Calls    {len(entries)} / {b.get('max_llm_calls_per_run', '-')} (per-run cap)")
    if hit or miss or out:
        tot_in = hit + miss
        rate = hit / tot_in * 100 if tot_in else 0
        print(f"  Input    {tot_in:,} (cache hits {hit:,} = {rate:.0f}%)")
        print(f"  Output   {out:,}")

    if entries:
        print(f"\n  By stage:")
        by_stage: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
        for e in entries:
            n, c = by_stage[e.get("stage", "?")]
            by_stage[e.get("stage", "?")] = (n + 1, c + float(e.get("cost", 0)))
        for stage, (n, c) in sorted(by_stage.items(), key=lambda x: -x[1][1]):
            print(f"    {stage:20s} {n:3d} calls  {c:.4f} {cur}")

    print(f"\n  Rate window: {'PEAK' if is_peak() else 'off-peak (half price)'}"
          f"  · Beijing time {datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%H:%M %a')}")

    warn = float(b.get("warn_pct", 0.8))
    print()
    if pct >= 1.0:
        print("  RED  At cap. " + ("fail_closed=true, pipeline will abort."
                                  if b.get("fail_closed") else "fail_closed=false, so it will keep running."))
        return 2
    elif pct >= warn:
        print(f"  AMBER Past the {warn*100:.0f}% warning line.")
        return 1
    print("  GREEN Budget is fine.")
    return 0


def cmd_gate() -> int:
    """给调度脚本调用的预检闸门。超预算返回非 0，流水线应立刻退出。"""
    b = CFG["budget"]
    cap, cur = _cap(), _cur()
    spent = LEDGER.spend_on(datetime.now(TZ).date(), TZ)

    if spent >= cap:
        msg = f"spent today {spent:.4f} / {cap:.2f} {cur}, pre-flight check failed."
        if b.get("fail_closed", True):
            print(f"BLOCKED: {msg}")
            return 1
        print(f"WARN: {msg} (fail_closed=false, continuing)")
        return 0

    print(f"OK: spent {spent:.4f} / {cap:.2f} {cur} today, {cap-spent:.4f} left")
    return 0


def cmd_report(days: int) -> int:
    today = datetime.now(TZ).date()
    since = today - timedelta(days=days - 1)
    cur = _cur()

    rows: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cost": 0.0, "calls": 0, "hit": 0, "miss": 0, "out": 0, "peak": 0}
    )
    for e in LEDGER.entries():
        d = e.get("day") or ""
        if d < since.isoformat():
            continue
        r = rows[d]
        r["cost"] += float(e.get("cost", 0))
        r["calls"] += 1
        r["hit"] += int(e.get("cache_hit", 0))
        r["miss"] += int(e.get("cache_miss", 0))
        r["out"] += int(e.get("output", 0))
        if e.get("peak"):
            r["peak"] += 1

    header(f"Cost report · last {days} days")
    if not rows:
        print("  No records.")
        return 0

    print(f"  {'Date':12s} {'Cost':>9s} {'Calls':>5s} {'Hit%':>7s} {'Out tok':>9s}")
    total = 0.0
    for d in sorted(rows, reverse=True):
        r = rows[d]
        total += r["cost"]
        tin = r["hit"] + r["miss"]
        rate = f"{r['hit']/tin*100:.0f}%" if tin else "—"
        print(f"  {d:12s} {r['cost']:>8.4f} {r['calls']:>5.0f} {rate:>7s} {r['out']:>9,.0f}")

    n_days = len(rows)
    daily = total / n_days
    print(f"\n  Total {total:.4f} {cur} · daily {daily:.4f} · ~{daily*30:.2f} {cur}/month")

    tot_hit = sum(r["hit"] for r in rows.values())
    tot_miss = sum(r["miss"] for r in rows.values())
    if tot_hit + tot_miss > 0:
        rate = tot_hit / (tot_hit + tot_miss)
        print(f"  Cache hit rate {rate*100:.1f}%"
              + ("  ✅" if rate > 0.3 else " -- low. Is the system prompt changing every day?"))

    peak_calls = sum(r["peak"] for r in rows.values())
    print(f"  Peak-rate calls {peak_calls}/{sum(r['calls'] for r in rows.values()):.0f}"
          + ("" if peak_calls == 0 else " -- moving to off-peak halves the cost"))
    return 0


def cmd_check_deepseek() -> int:
    """查 DeepSeek 余额，并用台账的消耗速度推算还能用多久。

    为什么不是「对账」：DeepSeek 不提供按日/按模型的账单明细 API，
    只有余额。所以只能算出「余额还能撑几天」这个更有用的数字。
    真正的精确对账要等它在控制台出账单，人工看一眼量级是否吻合。
    """
    env = CFG["ai"]["deepseek_api_key_env"]
    key = os.environ.get(env)
    if not key:
        print(f"FAIL env var {env} is not set.")
        return 1

    base = CFG["ai"]["deepseek_base"].rstrip("/")
    try:
        r = httpx.get(
            f"{base}/user/balance",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30.0,
        )
    except Exception as e:  # noqa: BLE001
        print(f"FAIL request failed: {e}")
        return 1

    if r.status_code == 401:
        print("FAIL 401: API key is invalid.")
        return 1
    if r.status_code != 200:
        print(f"❌ {r.status_code}: {r.text[:300]}")
        return 1

    d = r.json()
    header("DeepSeek balance")

    if not d.get("is_available", True):
        print("  FAIL Account unavailable (out of balance or suspended).")

    balances = d.get("balance_infos") or []
    if not balances:
        print(f"  (unrecognised response shape; raw follows)\n  {str(d)[:400]}")
        return 1

    total = 0.0
    for b in balances:
        cur = b.get("currency", "?")
        try:
            amt = float(b.get("total_balance", 0))
        except (TypeError, ValueError):
            amt = 0.0
        total += amt
        print(f"  {amt:>10.2f} {cur}"
              f"   (topped up {b.get('topped_up_balance', '?')} / granted {b.get('granted_balance', '?')}）")

    # 用最近 7 天的实际消耗推算
    recent = []
    today = datetime.now(TZ).date()
    for i in range(7):
        d_i = today - timedelta(days=i)
        recent.append(LEDGER.spend_on(d_i, TZ))
    spent_7d = sum(recent)
    days_with_data = sum(1 for x in recent if x > 0)

    print()
    if days_with_data == 0:
        print("  No spend in the last 7 days of ledger; cannot project runway.")
        return 0

    per_day = spent_7d / 7
    print(f"  Last 7 days: {spent_7d:.4f} {_cur()} ({days_with_data} days with data)")
    print(f"  Daily average {per_day:.4f} {_cur()}")

    if per_day > 0:
        remaining_days = total / per_day
        print(f"  -> At this rate the balance lasts about {remaining_days:.0f} days")
        if remaining_days < 14:
            print("  WARN Under two weeks. Top up, or reduce call frequency.")

    print()
    print("  Note: DeepSeek exposes no itemised billing API, so this can only project runway.")
    print("        To check precisely, compare the usage page at platform.deepseek.com")
    print("        against the magnitude of state/cost_ledger.jsonl.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="LifeLogging cost guard")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="what has been spent today")
    sub.add_parser("gate", help="pre-flight gate (exit 1 when over budget)")
    p = sub.add_parser("report", help="report for the last N days")
    p.add_argument("--days", type=int, default=30)
    sub.add_parser("check-deepseek", help="balance + projected runway")

    args = ap.parse_args()

    if args.cmd == "status":
        sys.exit(cmd_status())
    elif args.cmd == "gate":
        sys.exit(cmd_gate())
    elif args.cmd == "report":
        sys.exit(cmd_report(args.days))
    elif args.cmd == "check-deepseek":
        sys.exit(cmd_check_deepseek())


if __name__ == "__main__":
    main()
