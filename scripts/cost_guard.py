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
  python scripts/cost_guard.py emit-events       # 每日合计写进事件表（给 Grafana）
  python scripts/cost_guard.py check-deepseek    # 余额 + 剩余可用天数
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CostLedger,
    Event,
    EventsClient,
    header,
    is_peak,
    load_config,
    require_api_key,
)

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


def daily_totals() -> dict[str, dict[str, float]]:
    """台账 → ``{日期: {cost, calls, hit, miss, output, reasoning, peak}}``。"""
    rows: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cost": 0.0, "calls": 0.0, "hit": 0.0, "miss": 0.0,
                 "output": 0.0, "reasoning": 0.0, "peak": 0.0}
    )
    for e in LEDGER.entries():
        d = e.get("day") or ""
        if not d:
            continue
        r = rows[d]
        r["cost"] += float(e.get("cost", 0))
        r["calls"] += 1
        r["hit"] += int(e.get("cache_hit", 0))
        r["miss"] += int(e.get("cache_miss", 0))
        r["output"] += int(e.get("output", 0))
        # reasoning 已经含在 output 里（见 common.Usage 的注释），这里只是
        # 额外记一份，好在面板上看出「钱主要花在思考上」这件事。
        r["reasoning"] += int(e.get("reasoning", 0))
        r["peak"] += 1 if e.get("peak") else 0
    return dict(rows)


def cmd_emit_events(days: int) -> int:
    """把台账**按天**写成 ``ai/daily_cost`` 事件，供 Grafana 读。

    为什么要有这一步：每次调用的花费记在 ``state/cost_ledger.jsonl``（JSONL），
    而 Grafana 挂的是 ``state/events.db``。JSONL 那边它一个字都读不到 ——
    所以「每日 AI 花费」这个面板在补上这条链路之前一直是空的。
    真话是：不是面板坏了，是**没有任何东西往事件表写这个 metric**。

    ⚠️ 必须「先删再写」，不能直接 post
    ---------------------------------
    事件表的唯一索引是 ``(source, domain, metric, ts_utc, COALESCE(value_text,''))``，
    **不含 value_num**。而台账是**当天累加**的 —— 早上 0.003、晚上 0.05，
    第二次会被当成重复静默丢掉（``INSERT OR IGNORE``），那天就永远停在早上的数字上。

    手表步数踩的是同一个坑，见 ``middleware/ingest_gadgetbridge.py`` 里那段注释。
    """
    cur = _cur()
    client = EventsClient(CFG)
    if not client.ping():
        print(f"FAIL events API is not running ({client.base})")
        return 1

    since = (datetime.now(TZ).date() - timedelta(days=days - 1)).isoformat()
    rows = {d: r for d, r in daily_totals().items() if d >= since}

    header(f"Emit daily cost events · last {days} days")
    if not rows:
        print("  No ledger records in this window; nothing to write.")
        return 0

    for d in sorted(rows):
        r = rows[d]
        # 当天本地 00:00 → UTC。和 health/location 的日汇总事件同一个约定。
        try:
            y, m, dd = (int(x) for x in d.split("-"))
        except ValueError:
            print(f"  WARN unparseable day {d!r}, skipped")
            continue
        ts = datetime(y, m, dd, tzinfo=TZ).astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        # 删掉这一天上一次写的（可能只有半天数据），再写完整的。
        try:
            httpx.delete(
                f"{client.base}/day",
                headers=client.headers,
                params={"day": d, "source": "cost_guard"},
                timeout=30.0,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  WARN delete failed for {d}: {e} (a stale row may survive)")

        client.post_batch([
            Event(
                ts_utc=ts,
                domain="ai",
                metric="daily_cost",
                value_num=round(r["cost"], 6),
                value_text=cur,
                source="cost_guard",
                meta={
                    "calls": int(r["calls"]),
                    "cache_hit": int(r["hit"]),
                    "cache_miss": int(r["miss"]),
                    "output": int(r["output"]),
                    "reasoning": int(r["reasoning"]),
                    "peak_calls": int(r["peak"]),
                },
            )
        ])
        print(f"  OK  {d}  {r['cost']:.4f} {cur}  "
              f"({int(r['calls'])} calls, {int(r['peak'])} at peak rate)")

    print(f"\nOK  {len(rows)} day(s) written as ai/daily_cost")
    return 0


def cmd_check_deepseek() -> int:
    """查 DeepSeek 余额，并用台账的消耗速度推算还能用多久。

    为什么不是「对账」：DeepSeek 不提供按日/按模型的账单明细 API，
    只有余额。所以只能算出「余额还能撑几天」这个更有用的数字。
    真正的精确对账要等它在控制台出账单，人工看一眼量级是否吻合。
    """
    env = CFG["ai"]["deepseek_api_key_env"]
    try:
        key = require_api_key(env)
    except RuntimeError as e:
        print(f"FAIL {e}")
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

    print()

    # ── 用**余额差**推算还能撑几天 ──────────────────────────────────
    #
    # ⚠️ 这里原来是拿台账算的，那个数**会骗人**：
    #    台账只记本流水线自己的调用（一天几分钱），而余额是被**所有**
    #    用这把 key 的东西一起扣的 —— 最典型的是 Claude Code 和
    #    AI 编辑器，它们一个字节都不往我们的台账里写。
    #    实测：台账算出来「还能用 41 天」，而真实消耗是它的 100 倍。
    #
    #    余额是唯一能反映真实支出的数。两次采样做差即可。
    #    样本在 events 表里（collect_ai_usage.py 每天写一条 ai/balance）。
    real = _balance_history()
    if len(real) >= 2:
        first_day, first_bal = real[0]
        last_day, last_bal = real[-1]
        span = max((last_day - first_day).days, 1)
        burn = (first_bal - last_bal) / span
        print(f"  Balance history: {len(real)} samples "
              f"({first_day.isoformat()} -> {last_day.isoformat()})")
        print(f"  Real burn: {burn:.2f} {_cur()}/day "
              f"(all tools, not just this pipeline)")
        if burn > 0:
            remain = total / burn
            print(f"  -> At the real rate the balance lasts about {remain:.1f} days")
            if remain < 3:
                print("  ⚠️  CRITICAL: under three days. Top up, or the nightly")
                print("      journal will start failing on an empty balance.")
            elif remain < 14:
                print("  WARN Under two weeks. Top up, or reduce usage.")
        else:
            print("  (balance flat or topped up over this window; no projection)")
        print()
        print(f"  For comparison, this pipeline's own ledger says "
              f"{LEDGER.spend_on(today, TZ):.4f} today "
              f"-- that is a different number and a much smaller one.")
        return 0

    # 只有一个样本（或没有）：退回台账口径，但**必须说清楚它只覆盖本流水线**
    today = datetime.now(TZ).date()
    recent = [LEDGER.spend_on(today - timedelta(days=i), TZ) for i in range(7)]
    spent_7d = sum(recent)
    days_with_data = sum(1 for x in recent if x > 0)

    if days_with_data == 0:
        print("  No spend in the last 7 days of ledger; cannot project runway.")
        print("  (Only one balance sample so far -- runway needs at least two.)")
        return 0

    per_day = spent_7d / 7
    print(f"  Last 7 days (pipeline ledger only): {spent_7d:.4f} {_cur()}")
    print(f"  Daily average {per_day:.4f} {_cur()}")
    if per_day > 0:
        print(f"  -> At this rate the balance lasts about {total / per_day:.0f} days")
    print()
    print("  ⚠️  This projection uses state/cost_ledger.jsonl, which only sees THIS")
    print("      pipeline's calls. Anything else using the same key (Claude Code,")
    print("      editors, ...) is invisible here and can easily be 100x larger.")
    print("      At least two days of balance samples are needed for the real rate --")
    print("      collect_ai_usage.py records one per run.")
    return 0


def _balance_history() -> list[tuple[date, float]]:
    """从 events 表里读余额采样，按天取最后一条。

    直接读 SQLite（只读）而不是走 HTTP：这是本地诊断命令，
    不该依赖事件表服务在不在跑。
    """
    db = Path(CFG["state_dir"]) / "events.db"
    if not db.exists():
        return []
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT day_local, value_num FROM events "
                "WHERE domain='ai' AND metric='balance' ORDER BY ts_utc"
            ).fetchall()
    except sqlite3.Error:
        return []

    per_day: dict[date, float] = {}
    for day_s, value in rows:
        try:
            per_day[date.fromisoformat(day_s)] = float(value)
        except (TypeError, ValueError):
            continue
    return sorted(per_day.items())


def main() -> None:
    ap = argparse.ArgumentParser(description="LifeLogging cost guard")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="what has been spent today")
    sub.add_parser("gate", help="pre-flight gate (exit 1 when over budget)")
    p = sub.add_parser("report", help="report for the last N days")
    p.add_argument("--days", type=int, default=30)
    p = sub.add_parser("emit-events",
                       help="write daily totals into the events table (for Grafana)")
    p.add_argument("--days", type=int, default=90)
    sub.add_parser("check-deepseek", help="balance + projected runway")

    args = ap.parse_args()

    if args.cmd == "status":
        sys.exit(cmd_status())
    elif args.cmd == "gate":
        sys.exit(cmd_gate())
    elif args.cmd == "report":
        sys.exit(cmd_report(args.days))
    elif args.cmd == "emit-events":
        sys.exit(cmd_emit_events(args.days))
    elif args.cmd == "check-deepseek":
        sys.exit(cmd_check_deepseek())


if __name__ == "__main__":
    main()
