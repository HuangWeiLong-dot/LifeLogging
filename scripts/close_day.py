"""服务器侧：把已经关账的那一天写成日记正文。

**为什么这件事要搬到这里**

    原来它跑在笔记本上，于是「一天什么时候结束」是由**笔记本什么时候开机**
    决定的 —— 实测 2026-10-14 那天机器 22:18 就关了，那一天实际到**次日 17:56**
    才被关账。

    现在服务器负责调 AI 写正文。vault 的读写仍然全在笔记本上（见下），所以
    服务器不需要看见 vault，也就不需要做双向同步。

**触发条件：等笔记本说「这天完整了」**

    本来打算按固定时刻（凌晨 4:05）关账，写的时候发现那样不对：

      ActivityWatch **保留完整历史**，所以「读 D 那天的 AW」在任何时候读到的
      都是完整的一天 —— 但笔记本只在开机时才把 timeline 推上来。如果笔记本
      18:00 就关了，那么 4:05 时服务器手里只有它 **18:00 那次推送**的版本，
      据此生成的日记会缺掉 18:00 之后的部分。

    所以真正该等的不是时钟，是**笔记本确认「D 那天的 timeline 已经是终版」**。
    笔记本在「这次处理的这一天已经过完」时才打这个标记（见 run_daily.ps1
    与 push_journal_input.py 的 `final` 字段）。

    于是这个脚本**每 15 分钟跑一次**、幂等地扫一遍：凡是「有终版输入、还没生成
    输出」的那天就生成。这样既不依赖笔记本开机，也不会拿半截数据去写日记。
    笔记本关机导致错过某个时刻的情况自然也被兜住了 —— 它下次开机一推，
    15 分钟内就会被捡起来。

用法：
  python scripts/close_day.py                 # 扫描并生成所有待处理的
  python scripts/close_day.py --dry-run       # 只打印，不调 AI、不写回
  python scripts/close_day.py --force 2026-10-14   # 忽略 final 标记，强制生成一天
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

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
from generate_journal import (  # noqa: E402
    H_NOTES,
    MAX_DEVIATION,
    SYSTEM_PROMPT,
    build_user_prompt,
    validate_body,
)

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])


def relay() -> tuple[str, str] | None:
    """返回 (base, key)。没配好返回 None。"""
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


def _get(base: str, key: str, path: str, **params) -> dict | None:
    """GET，404 返回 None（「还没有」是正常状态，不是错误）。"""
    try:
        r = httpx.get(f"{base}{path}", headers={"X-API-Key": key},
                      params=params, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  relay unreachable at {base}: {e}")
        return None
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        print(f"  ⚠️  relay returned {r.status_code}: {r.text[:200]}")
        return None
    return r.json()


def clean_body(raw: str) -> str:
    """抄自 generate_journal.main 的清洗：去围栏、砍掉模型自作主张的 Notes 段。"""
    body = raw.strip()
    body = re.sub(r"^```(?:markdown|md)?\s*\n", "", body)
    body = re.sub(r"\n```\s*$", "", body)
    if H_NOTES in body:
        body = body.split(H_NOTES)[0].rstrip()
    return body


def generate(day: Day, bundle: dict, dry_run: bool) -> bool:
    """给一天生成正文，存回中继。返回是否成功。"""
    tl = bundle.get("timeline") or {}
    carryover = bundle.get("carryover") or []

    # 相关历史由**笔记本**在推送前检索好（索引和 vault 都在那边）。
    # 老版本推上来的输入包没有这个字段 —— 缺了就当没有，不用报错。
    history = bundle.get("history") or []
    user = build_user_prompt(tl, day, carryover, history)
    print(f"-> {day.iso}: prompt {len(user):,} chars, "
          f"{len(tl.get('blocks') or [])} blocks, {len(carryover)} carryover")

    if dry_run:
        print("   (--dry-run: 不调 AI)")
        return True

    ledger = CostLedger(CFG["budget"]["ledger_path"])
    llm = LLM(cfg=CFG, ledger=ledger)
    run_id = new_run_id()
    before = len(ledger.entries())
    try:
        raw = llm.complete(system=SYSTEM_PROMPT, user=user,
                           stage="journal", run_id=run_id)
    except BudgetExceeded as e:
        # 预算超了是**全局**状态，其余几天也一样过不去 —— 直接放弃整个扫描
        print(f"❌ {e}")
        return False
    except TruncatedOutput as e:
        print(f"❌ {e}")
        return False

    body = clean_body(raw)
    total, warnings = validate_body(body, tl.get("total_hours", 0))
    blocks = len(re.findall(r"\[duration::", body, re.IGNORECASE))
    print(f"   {len(body):,} chars, {blocks} blocks, total {total:.1f}h")
    for w in warnings:
        print(f"   ⚠️  {w}")

    # 硬闸门：和笔记本上那份逐字一致。模型写的时长和规则引擎差太多，
    # 说明时间轴是编的 —— 宁可不写，让下一次重来。
    rule_hours = float(tl.get("total_hours") or 0)
    if rule_hours > 0 and total > 0:
        dev = abs(total - rule_hours) / rule_hours
        if dev > MAX_DEVIATION:
            print(f"❌ {day.iso}: model {total:.1f}h vs rule-engine {rule_hours:.1f}h — "
                  f"{dev*100:.0f}% apart, over the {MAX_DEVIATION*100:.0f}% limit. NOT stored.")
            return False

    entries = ledger.entries()
    last = entries[-1] if len(entries) > before else {}
    stats = {
        "calls": 1,
        "tokens_in": last.get("cache_hit", 0) + last.get("cache_miss", 0),
        "tokens_out": last.get("output", 0),
        "cache_hit": last.get("cache_hit", 0),
        "cache_miss": last.get("cache_miss", 0),
        "reasoning": last.get("reasoning", 0),
        "cost": last.get("cost", 0.0),
        "currency": last.get("currency", CFG["budget"].get("currency", "CNY")),
        "peak": bool(last.get("peak", False)),
        "model": last.get("model", "?"),
        "provider": last.get("provider", "?"),
        "total_hours": round(total, 2),
        "blocks": blocks,
        "warnings": warnings,
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "run_id": run_id,
    }

    base, key = _RELAY
    try:
        r = httpx.post(f"{base}/journal/output",
                       headers={"X-API-Key": key},
                       json={"day": day.iso, "body": body, "stats": stats},
                       timeout=30.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  could not store the output: {e}")
        return False
    if r.status_code != 200:
        print(f"  ⚠️  relay rejected the output ({r.status_code}): {r.text[:200]}")
        return False
    print(f"   OK  {stats['cost']:.4f} {stats['currency']}"
          f"{' (peak)' if stats['peak'] else ''} → stored on the relay")
    return True


_RELAY: tuple[str, str] = ("", "")


def push_ledger() -> None:
    """把服务器这份成本台账整份推回中继。

    **为什么必须推。** 服务器上花的钱记在**服务器的**台账里（`state_dir` 指到
    `/var/lib/lifelogging`），而 Grafana 的「今日 AI 花费」面板读的是笔记本
    事件表里的 `ai/daily_cost` —— 那个数是从笔记本自己的台账算出来的。
    两边一分开，日记那笔钱就从面板上消失了，而且不会有任何提示。

    推失败只警告、不影响生成：日记本身没问题，别因为一个统计面板把正事拦住。
    """
    base, key = _RELAY
    if not base:
        return
    path = CFG["budget"]["ledger_path"]
    try:
        entries = CostLedger(path).entries()
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  could not read the ledger at {path}: {e}")
        return
    if not entries:
        return
    try:
        r = httpx.post(
            f"{base}/ledger",
            headers={"X-API-Key": key},
            json={
                "entries": entries,
                "pushed_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
            },
            timeout=30.0,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  could not push the ledger: {e}")
        return
    if r.status_code != 200:
        print(f"  ⚠️  relay rejected the ledger ({r.status_code}): {r.text[:200]}")
        return
    print(f"  OK  ledger pushed ({len(entries)} entries)")


def main() -> None:
    global _RELAY

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=int(
        ((CFG.get("close_day") or {}).get("lookback_days", 3))),
        help="往前扫描几天")
    ap.add_argument("--dry-run", action="store_true", help="不调 AI、不写回")
    ap.add_argument("--force", default="", help="忽略 final 标记，强制生成指定的一天")
    args = ap.parse_args()

    header("Close day")

    conn = relay()
    if conn is None:
        return
    _RELAY = conn
    base, key = conn

    if args.force:
        days = [Day.parse(args.force, TZ)]
        force = True
    else:
        today = Day.parse("today", TZ)
        # 从旧到新：先补欠着的，再处理最近的
        days = [Day.parse((today.d - timedelta(days=i)).isoformat(), TZ)
                for i in range(args.days, 0, -1)]
        force = False

    done = 0
    for day in days:
        bundle = _get(base, key, "/journal/inputs", day=day.iso)
        if bundle is None:
            continue
        if not force and not bundle.get("final"):
            print(f"-> {day.iso}: 输入包还没标终版，跳过")
            continue
        if _get(base, key, "/journal/output", day=day.iso) is not None:
            print(f"-> {day.iso}: 已经生成过了")
            continue
        if generate(day, bundle, args.dry_run):
            done += 1

    print(f"-> done: {done} day(s)" if done else "-> nothing to do")

    # 每次都推 —— 哪怕这次没生成日记。生成和推台账是两件事：万一上次推失败了，
    # 这次还能补上。
    push_ledger()


if __name__ == "__main__":
    main()
