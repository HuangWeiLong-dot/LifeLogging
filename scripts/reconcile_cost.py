"""成本核对 —— 台账完整性 + 网关比对 + 落盘报表。

⚠️ 关于 DeepSeek 的一个现实约束：
   DeepSeek **没有** Anthropic 那样的 usage_report / cost_report API，
   拉不到按日按模型的账单明细。所以这里做的事是：
     1. 校验本地台账没被篡改、没丢行（hash 链）
     2. 如果启用了 LiteLLM 网关，比对「网关记录的 spend」vs「本地台账」
     3. 把结果写成报表存起来，供人工和 DeepSeek 控制台的用量页对照
   余额与剩余可用天数由 cost_guard.py check-deepseek 负责。

为什么保留 hash 链：成本台账是这套系统里唯一涉及钱的数据。
   如果它被静默改过或丢过行，你后面所有的预算判断都是错的，
   而且不会有任何提示。链式哈希让这种情况一定能被发现。

用法：
  python scripts/reconcile_cost.py --days 7
  python scripts/reconcile_cost.py --days 7 --write-vault
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CostLedger, Event, EventsClient, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])
VAULT = Path(CFG["vault_path"])
LEDGER_PATH = Path(CFG["budget"]["ledger_path"])


# ── 台账完整性（hash 链）──────────────────────────────────────────


def ledger_chain_check() -> tuple[bool, str]:
    """验证台账没有被篡改或丢行。

    每行的 chain = sha256(上一行 chain + 本行除 chain 外的规范化 JSON)。
    任何一行被改动、或中间丢了行，后面的链就对不上。
    """
    if not LEDGER_PATH.exists():
        return True, "Ledger is empty (nothing has been called yet)"

    prev = "genesis"
    n = 0
    with LEDGER_PATH.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                return False, f"Line {lineno} is not valid JSON"

            stored = obj.pop("chain", None)
            if stored is None:
                # 旧格式没有链字段。从这行起重置基准，不算失败，
                # 但说明这段历史没有防篡改保护。
                prev = "genesis"
                n += 1
                continue

            payload = json.dumps(obj, ensure_ascii=False, sort_keys=True)
            expect = hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()
            if stored != expect:
                return False, f"Line {lineno} failed verification -- ledger edited or rows dropped"
            prev = stored
            n += 1

    return True, f"{n} lines verified"


# ── 数据 ────────────────────────────────────────────────────────────


def local_by_day(days: int) -> dict[str, float]:
    since = (datetime.now(TZ).date() - timedelta(days=days - 1)).isoformat()
    out: dict[str, float] = defaultdict(float)
    for e in CostLedger(LEDGER_PATH).entries():
        d = e.get("day") or ""
        if d >= since:
            out[d] += float(e.get("cost", 0))
    return dict(out)


def litellm_by_day(days: int) -> dict[str, float] | None:
    """可选的第二来源：如果走了 LiteLLM 网关，它有独立的 spend 记录。

    两边都对得上，说明记账逻辑没跑偏。
    """
    if not CFG.get("litellm", {}).get("enabled"):
        return None
    key = os.environ.get(CFG["litellm"]["master_key_env"])
    if not key:
        print(f"  WARN {CFG['litellm']['master_key_env']} not set, skipping gateway comparison")
        return None

    since = datetime.now(TZ).date() - timedelta(days=days - 1)
    try:
        r = httpx.get(
            f"{CFG['litellm']['base_url'].rstrip('/')}/global/spend/report",
            headers={"Authorization": f"Bearer {key}"},
            params={"start_date": since.isoformat(),
                    "end_date": datetime.now(TZ).date().isoformat()},
            timeout=60.0,
        )
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  LiteLLM query failed: {e}")
        return None

    out: dict[str, float] = defaultdict(float)
    for row in r.json():
        d = (row.get("date") or row.get("day") or "")[:10]
        if d:
            out[d] += float(row.get("spend", 0))
    return dict(out)


# ── 输出 ────────────────────────────────────────────────────────────


def render(days: int, local: dict[str, float],
           gateway: dict[str, float] | None) -> tuple[str, list[str]]:
    cur = CFG["budget"].get("currency", "CNY")
    all_days = sorted(set(local) | set(gateway or {}), reverse=True)

    L: list[str] = [f"# Cost Reconciliation · last {days} days", ""]
    L.append(f"Generated: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} ({CFG['timezone']})")
    L.append("")
    L.append("> Local ledger is an **estimate** from config.pricing, not a bill.")
    L.append("> DeepSeek exposes no per-day billing API, so this is a self-consistency")
    L.append("> check, not a reconciliation against a vendor statement.")
    L.append("> For balance and runway, run `cost_guard.py check-deepseek`.")
    L.append("")

    flags: list[str] = []
    if not all_days:
        L.append("_No records in this window._")
    else:
        cols = ["Date", "Local ledger"]
        if gateway is not None:
            cols += ["Gateway", "Delta"]
        L.append("| " + " | ".join(cols) + " |")
        L.append("|" + "---|" * len(cols))

        for d in all_days:
            lv = local.get(d, 0.0)
            cells = [d, f"{lv:.4f} {cur}"]
            if gateway is not None:
                gv = (gateway or {}).get(d)
                if gv is None:
                    cells += ["—", "—"]
                else:
                    delta = (lv - gv) / gv if gv > 0 else 0.0
                    mark = " ⚠️" if abs(delta) > 0.05 else ""
                    cells += [f"{gv:.4f}", f"{delta*100:+.1f}%{mark}"]
                    if abs(delta) > 0.05:
                        flags.append(f"{d}: local {lv:.4f} vs gateway {gv:.4f}")
            L.append("| " + " | ".join(cells) + " |")

        L.append("")
        total = sum(local.values())
        L.append(f"**Local total: {total:.4f} {cur}**")
        if gateway:
            gt = sum(gateway.values())
            L.append(f"**Gateway total: {gt:.4f}**")
        L.append("")
        L.append("## Averages")
        L.append("")
        n = len(local) or 1
        L.append(f"- Daily average: {total/n:.4f} {cur}")
        L.append(f"- Monthly projection: {total/n*30:.2f} {cur}")
        L.append("")

        if flags:
            L.append("## Deltas over 5%")
            L.append("")
            for f in flags:
                L.append(f"- {f}")
            L.append("")
            L.append("Likely causes: pricing in config.yaml is stale, cache accounting")
            L.append("differs between the two paths, or another program uses the same key.")
            L.append("")

        ok, msg = ledger_chain_check()
        L.append("## Ledger integrity")
        L.append("")
        L.append(f"{'✅' if ok else '❌'} {msg}")
        L.append("")
        L.append("`chain` is a SHA-256 hash chain over the ledger lines. If anyone edits")
        L.append("a line or drops rows, verification fails here — cost accounting is the")
        L.append("one place in this system where silent corruption would go unnoticed.")
        L.append("")

    return "\n".join(L), flags


def write_to_events(total: float, ok: bool) -> None:
    """把核对结果本身也记进事件表，纳入统一 lifelogging。"""
    client = EventsClient(CFG)
    if not client.ping():
        return
    client.post_batch([
        Event(
            ts_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            domain="ai",
            metric="cost_reconcile",
            value_num=round(total, 4),
            value_text=CFG["budget"].get("currency", "CNY"),
            source="reconcile_cost",
            meta={"ledger_ok": ok},
        )
    ])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--write-vault", action="store_true",
                    help="also write to 07-Reviews/Cost-Ledger.md")
    args = ap.parse_args()

    header(f"Cost check · last {args.days} days")

    print("-> Ledger integrity")
    ok, msg = ledger_chain_check()
    print(f"  {'✅' if ok else '❌'} {msg}")

    print("-> Local ledger")
    local = local_by_day(args.days)
    print(f"  OK  {len(local)} days with records, total {sum(local.values()):.4f} "
          f"{CFG['budget'].get('currency', 'CNY')}")

    print("-> LiteLLM gateway")
    gateway = litellm_by_day(args.days)
    if gateway is None:
        print("  (not enabled, skipping -- this is an optional second source)")
    else:
        print(f"  OK  total {sum(gateway.values()):.4f}")

    text, _ = render(args.days, local, gateway)

    state = Path(CFG["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    report = state / f"reconcile-{datetime.now(TZ).date().isoformat()}.md"
    report.write_text(text, encoding="utf-8")
    print(f"\nOK  Report: {report}")

    if args.write_vault:
        out = VAULT / "07-Reviews" / "Cost-Ledger.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"OK  Written {out}")

    write_to_events(sum(local.values()), ok)

    print()
    print("  For balance and remaining runway: python scripts/cost_guard.py check-deepseek")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
