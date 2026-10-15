"""笔记本侧：把服务器那份成本台账合并进本机台账。

**为什么要有它**

    AI 写日记那一步搬到服务器上之后，那笔钱记在**服务器的**台账里。而 Grafana
    的「今日 AI 花费」面板读的是笔记本事件表里的 `ai/daily_cost` —— 那个数是
    从**笔记本自己的**台账算出来的。

    两边一分开，日记那笔钱就从面板上消失了，**而且两边都不会报错**：
    服务器按时把日记写出来了，笔记本按时把日记取回来了，中间只是少了一个数字。

**为什么不能直接把两份文件拼起来**

    台账是**哈希链**的（每条记录带一个基于前一条的 sha256），直接拼接会让两条
    链互相打断，`reconcile_cost.py` 的完整性校验当场报错。

    这里的做法是逐条重新 `append()` 进本机台账 —— chain 由本机重算，本机那条
    链仍然完整。

**幂等**

    判重用**内容指纹**（`ledger_fingerprint`，哈希里不含 chain）：同一个调用在
    服务器和笔记本两边算出来一样。合并过的再遇到就跳过，所以重复运行是安全的，
    `run_daily` 每次跑都调它也不会重复记账。

用法：
  python scripts/merge_ledger.py
  python scripts/merge_ledger.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CostLedger, header, ledger_fingerprint, load_config  # noqa: E402

CFG = load_config()


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


def merge_into(ledger: CostLedger, remote: list) -> list[dict]:
    """把远端的条目并进本机台账，返回**实际合并进去的**那些。幂等。

    抽成函数是为了能测 —— 这段是唯一会把两条哈希链拼到一起的地方，错了的话
    `reconcile_cost.py` 的完整性校验会开始报错，而那个报错看起来像「台账被人
    改过」，会把人引向完全错误的方向。
    """
    have = ledger.fingerprints()
    fresh: list[dict] = []
    for e in remote:
        if not isinstance(e, dict):
            continue
        fp = ledger_fingerprint(e)
        if fp in have:
            continue
        # ⚠️ 合并进去的要立刻记进 have。只在开头算一次是不够的 ——
        #    远端列表**自己内部**如果有两条一模一样的，两条都会被当成新的。
        have.add(fp)
        # 逐条 append：chain 由本机重算，本机那条链保持完整。
        # 服务器那条 chain 丢掉 —— 它依赖服务器台账里它前面是谁，在这里没有意义。
        ledger.append({k: v for k, v in e.items() if k != "chain"})
        fresh.append(e)
    return fresh


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写台账")
    args = ap.parse_args()

    header("Merge ledger")

    conn = relay()
    if conn is None:
        return
    base, key = conn

    try:
        r = httpx.get(f"{base}/ledger", headers={"X-API-Key": key}, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Could not reach the phone relay at {base}: {e}")
        return
    if r.status_code == 404:
        # 「服务器还没推过台账」是**正常状态**（它还没跑过任何 AI），不是错误。
        # 当成错误会让每天第一次运行都报红。
        print("-> 服务器还没推过台账（close_day 还没跑过）")
        return
    if r.status_code != 200:
        print(f"  ⚠️  relay returned {r.status_code}: {r.text[:200]}")
        return

    remote = r.json().get("entries") or []
    ledger = CostLedger(CFG["budget"]["ledger_path"])

    if args.dry_run:
        have = ledger.fingerprints()
        fresh = [e for e in remote
                 if isinstance(e, dict) and ledger_fingerprint(e) not in have]
        if not fresh:
            print(f"-> 没有新的（服务器 {len(remote)} 条，本机都已经有了）")
            return
        total = sum(float(e.get("cost") or 0.0) for e in fresh)
        for e in fresh:
            print(f"  + {e.get('day', '?')}  {str(e.get('stage', '?')):10}  "
                  f"{float(e.get('cost') or 0):.4f} {e.get('currency', '')}")
        print(f"-> dry run：会合并 {len(fresh)} 条，合计 {total:.4f}")
        return

    fresh = merge_into(ledger, remote)
    if not fresh:
        print(f"-> 没有新的（服务器 {len(remote)} 条，本机都已经有了）")
        return
    total = sum(float(e.get("cost") or 0.0) for e in fresh)
    for e in fresh:
        print(f"  + {e.get('day', '?')}  {str(e.get('stage', '?')):10}  "
              f"{float(e.get('cost') or 0):.4f} {e.get('currency', '')}")
    print(f"-> 合并了 {len(fresh)} 条，合计 {total:.4f} {ledger.currency()}")


if __name__ == "__main__":
    main()
