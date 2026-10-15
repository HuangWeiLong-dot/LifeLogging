"""AI 用量采集 —— DeepSeek 平台的精确账单 + Claude Code 的分布 + 账户余额。

**为什么需要它**

`state/cost_ledger.jsonl` 只记**本流水线自己**发起的调用（每晚写日记那几次，
一天约 ¥0.02）。但你白天真正在花钱的是 Claude Code —— 它跑的是同一个
DeepSeek key、扣的是同一个余额，而它**不会**往我们的台账里写任何东西。
于是 Grafana 上「今日 AI 花费 ¥0.2」和你实际花掉的「二十几块」差了两个数量级。
面板没算错，是它只看得见 1% 的支出。

**三个来源，各司其职（优先级从高到低）**

1. **DeepSeek 平台的用量接口** —— **精确**，和官网「用量」页显示的是同一份数据。
   官方没有公开这个接口，走的是网页自己的 `/api/v0/usage/{amount,cost}`，
   用**登录态 userToken** 鉴权（不是 API key），见下面「userToken」一节。
   它能给：按天 × 按模型 的 token 明细（含请求数）和金额。
   实测对得上官网：2026-10-14 当天 ¥22.95，与页面上的 ¥22.84 一致
   （差异只是取数时间不同）。
2. **账户余额**（`/user/balance`）—— 精确，但只有**当前值**。
   余额差 = 当天真实被扣的钱（跨所有工具），作为交叉校验。
3. **Claude Code 的 transcript**（`~/.claude/projects/**/*.jsonl`）——
   **估算**，只用来出「形状」：花在哪个项目、主会话还是子代理。
   平台接口答不出这两样，而它们是本地文件里唯一有的信息。

⚠️ **为什么不用 ccusage**（路线图里提过它）
   实测它**不按 message.id 去重**，而 Claude Code 会把同一次 API 响应的日志
   **重复写十几行**（实测同一个 id 在 3 秒内出现 11 次、usage 完全相同）。
   于是它把当天的 806M 缓存读 tokens 全算了一遍，比真实值高约 **2.4 倍**，
   而且按 Anthropic 的价格表报**美元**。
   自己解析并按 message.id 去重后仍偏低约 26%（917 条唯一消息 vs 平台 1599 次请求）
   —— 所以 transcript 这条路**永远只是估算**，精确值只能来自平台。

**userToken 怎么来**

  浏览器打开 platform.deepseek.com → F12 → Application → Local Storage →
  找 `userToken`，复制它的 `value` 字段（形如 `{"value":"...","__version":"0"}`，
   要的是里面的字符串）。

  ⚠️ 它是**登录态凭证，等于你的账号**，不是 API key：
     - 别提交进 git、别贴进聊天记录。
     - 会过期（重新登录就换），过期后这里会报 401，重新拷一个即可。
     - 存法和其他密钥一致：设成环境变量，配置里只写变量名。
         setx DEEPSEEK_USER_TOKEN "..."

用法：
  python scripts/collect_ai_usage.py                # 今天
  python scripts/collect_ai_usage.py --days 30      # 顺带回填本月最近 30 天
  python scripts/collect_ai_usage.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Day,
    Event,
    EventsClient,
    Usage,
    compute_cost,
    header,
    load_config,
    require_api_key,
)

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])
SOURCE = "collect_ai_usage"

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
SKIP_MODELS = {"<synthetic>", "synthetic", ""}

PLATFORM = "https://platform.deepseek.com"

# 浏览器 UA 是必需的：这个接口只服务网页，默认的 python-httpx UA 会被挡。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def _platform_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": PLATFORM,
        "Referer": f"{PLATFORM}/usage",
    }


# ── 1. 平台账单（精确）──────────────────────────────────────────────


def _biz_data(payload: dict):
    """剥掉 {code,msg,data:{biz_data:...}} 这几层。

    ⚠️ 两个接口的 biz_data **形状不一样**，这是实测的，不是笔误：
       /usage/amount → {"total": [...], "days": [...], "currency": ...}
       /usage/cost   → [{"total": [...], "days": [...], "currency": ...}]   ← 多套一层数组
    所以两种都认。
    """
    data = (payload or {}).get("data") or {}
    bd = data.get("biz_data")
    if isinstance(bd, list):
        return bd[0] if bd else {}
    return bd or {}


def read_platform(day: Day, token: str, days: int) -> dict[str, dict] | None:
    """读平台用量接口 → ``{日期: {模型: 指标}}``。

    一次调用拿回**整月**，所以回填 30 天也只花两个请求。

    返回 None 表示拿不到（没配 token、token 过期、接口变了）——
    调用方会退回估算值，并在日志里说清楚。
    """
    out: dict[str, dict] = defaultdict(dict)
    cost_seen = tok_seen = False

    for path, field in (("/api/v0/usage/cost", "cost"),
                        ("/api/v0/usage/amount", "tokens")):
        try:
            r = httpx.get(
                f"{PLATFORM}{path}",
                headers=_platform_headers(token),
                params={"month": day.start_local.month, "year": day.start_local.year},
                timeout=60.0,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  {path} request failed: {e}")
            continue

        if r.status_code == 401:
            print("  ❌ 401 —— userToken 过期或无效。重新拷一个（见文件头说明）。")
            return None
        if r.status_code != 200:
            print(f"  ⚠️  {path} returned {r.status_code}: {r.text[:200]}")
            continue

        try:
            days_data = _biz_data(r.json()).get("days") or []
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  {path} response shape changed: {e}")
            continue

        for entry in days_data:
            date = entry.get("date")
            if not date:
                continue
            for model_entry in entry.get("data") or []:
                model = model_entry.get("model") or "?"
                if model in SKIP_MODELS:
                    continue
                metrics = {
                    u.get("type"): float(u.get("amount") or 0)
                    for u in (model_entry.get("usage") or [])
                }
                if not any(metrics.values()):
                    continue  # 平台会把当月所有模型都列出来，没用的全是 0
                bucket = out[date].setdefault(
                    model,
                    {"cost": 0.0, "requests": 0, "in_hit": 0.0, "in_miss": 0.0, "out": 0.0},
                )
                if field == "cost":
                    # 金额是按「命中 / 未命中 / 输出」三段分别给的，加起来才是这个模型当天的钱。
                    bucket["cost"] = (
                        metrics.get("PROMPT_CACHE_HIT_TOKEN", 0.0)
                        + metrics.get("PROMPT_CACHE_MISS_TOKEN", 0.0)
                        + metrics.get("RESPONSE_TOKEN", 0.0)
                    )
                else:
                    bucket["requests"] = int(metrics.get("REQUEST", 0))
                    bucket["in_hit"] = metrics.get("PROMPT_CACHE_HIT_TOKEN", 0.0)
                    bucket["in_miss"] = metrics.get("PROMPT_CACHE_MISS_TOKEN", 0.0)
                    bucket["out"] = metrics.get("RESPONSE_TOKEN", 0.0)
        cost_seen = cost_seen or field == "cost"
        tok_seen = tok_seen or field == "tokens"

    if not (cost_seen and tok_seen):
        return None

    # 只保留请求范围内的那些天
    since = (day.start_local.date() - timedelta(days=days - 1)).isoformat()
    return {d: m for d, m in out.items() if since <= d <= day.start_local.date().isoformat()}


# ── 2. 账户余额（精确，但只有当前值）────────────────────────────────


def read_balance() -> dict | None:
    env = CFG["ai"].get("deepseek_api_key_env", "DEEPSEEK_API_KEY")
    try:
        key = require_api_key(env)
    except RuntimeError as e:
        # 余额只是附加信息（拿不到就少一个数字），不该让整个采集失败 ——
        # 但也别静默：说清楚为什么拿不到。
        print(f"  . balance unavailable: {e}")
        return None
    try:
        r = httpx.get(
            CFG["ai"]["deepseek_base"].rstrip("/") + "/user/balance",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30.0,
        )
        r.raise_for_status()
        infos = r.json().get("balance_infos") or []
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Balance query failed: {e}")
        return None
    if not infos:
        return None
    info = infos[0]
    return {
        "balance": float(info.get("total_balance", 0)),
        "currency": info.get("currency", "CNY"),
        "topped_up": float(info.get("topped_up_balance", 0)),
        "granted": float(info.get("granted_balance", 0)),
    }


# ── 3. Claude Code transcript（估算，只出形状）──────────────────────


def _usage_of(msg: dict) -> Usage | None:
    u = msg.get("usage")
    if not isinstance(u, dict):
        return None
    return Usage(
        cache_hit=int(u.get("cache_read_input_tokens") or 0),
        # 写缓存按**未命中**计价：那是实打实送进去的新 token。
        cache_miss=int(u.get("input_tokens") or 0)
        + int(u.get("cache_creation_input_tokens") or 0),
        output=int(u.get("output_tokens") or 0),
        reasoning=int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0),
    )


def read_transcripts(day: Day) -> list[dict]:
    """当天每条 Claude Code 消息 → dict（**已按 message.id 去重**）。

    ⚠️ 去重是核心。同一次 API 响应在 transcript 里会被写很多行
       （流式/分块各落一次），每行的 usage **完全相同**、message.id 也相同，
       只有行自己的 uuid 不同。不去重就会把花费放大两三倍，
       而且放大得毫无提示 —— 出来一个看起来很像真的数字。

       同一条 id 出现多次时取**用量最大**的那行。
    """
    if not CLAUDE_PROJECTS.exists():
        return []

    best: dict[str, dict] = {}
    for path in CLAUDE_PROJECTS.rglob("*.jsonl"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < day.start_utc:
                continue
        except OSError:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for line in text.splitlines():
            if '"usage"' not in line:          # 便宜的预过滤
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = d.get("message") or {}
            usage = _usage_of(msg)
            if usage is None:
                continue
            model = msg.get("model") or "?"
            if model in SKIP_MODELS:
                continue

            try:
                at = datetime.fromisoformat(str(d.get("timestamp")).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if at.astimezone(TZ).date() != day.start_local.date():
                continue

            mid = msg.get("id") or d.get("uuid")
            total = usage.total_in + usage.output
            prev = best.get(mid)
            if prev is not None and prev["tokens"] >= total:
                continue

            cwd = d.get("cwd") or ""
            best[mid] = {
                "usage": usage,
                "at": at,
                "model": model,
                "agent": "subagent" if "subagents" in path.parts else "main",
                "project": Path(cwd).name if cwd else path.relative_to(CLAUDE_PROJECTS).parts[0],
                "tokens": total,
            }
    return list(best.values())


def shape_from_transcripts(day: Day) -> dict:
    """只汇总「哪个项目 / 主会话还是子代理」—— 金额以平台为准，这里不算总账。"""
    by_project: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "calls": 0, "cost": 0.0})
    by_agent: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "calls": 0, "cost": 0.0})

    for r in read_transcripts(day):
        u = r["usage"]
        cost = float(compute_cost(u, r["model"], CFG.get("pricing", {}), r["at"]).get("cost", 0))
        p = by_project[r["project"] or "unknown"]
        p["tokens"] += r["tokens"]; p["calls"] += 1; p["cost"] += cost
        a = by_agent[f"claude-code:{r['agent']}"]
        a["tokens"] += r["tokens"]; a["calls"] += 1; a["cost"] += cost
    return {"by_project": dict(by_project), "by_agent": dict(by_agent)}


# ── 写事件表 ────────────────────────────────────────────────────────


def events_for(day: Day, platform: dict | None, shape: dict, balance: dict | None) -> list[Event]:
    cur = CFG["budget"].get("currency", "CNY")
    ts = day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[Event] = []

    if platform:
        for date, models in platform.items():
            day_ts = datetime.fromisoformat(date).replace(tzinfo=TZ)
            day_ts = day_ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            total = 0.0
            for model, v in models.items():
                total += v["cost"]
                out.append(Event(
                    ts_utc=day_ts, domain="ai", metric="model_cost",
                    value_num=round(v["cost"], 4), value_text=model, source=SOURCE,
                    meta={"currency": cur, "source": "platform",
                          "requests": v["requests"]},
                ))
                out.append(Event(
                    ts_utc=day_ts, domain="ai", metric="model_tokens",
                    value_num=int(v["in_hit"] + v["in_miss"] + v["out"]),
                    value_text=model, source=SOURCE,
                    meta={"source": "platform", "requests": v["requests"],
                          "in_cache_hit": int(v["in_hit"]),
                          "in_cache_miss": int(v["in_miss"]),
                          "out": int(v["out"]), "currency": cur},
                ))
            # 当天总额：**这一个数就是权威答案**，别再自己估。
            out.append(Event(
                ts_utc=day_ts, domain="ai", metric="spend",
                value_num=round(total, 4), value_text=cur, source=SOURCE,
                meta={"source": "platform", "models": sorted(models),
                      "requests": sum(v["requests"] for v in models.values())},
            ))

    # transcript 那部分：只出形状，而且**只在拿不到平台数据时**才当成估算依据
    for proj, v in shape.get("by_project", {}).items():
        out.append(Event(
            ts_utc=ts, domain="ai", metric="project_tokens",
            value_num=v["tokens"], value_text=proj, source=SOURCE,
            meta={"calls": v["calls"], "cost_estimate": round(v["cost"], 4),
                  "currency": cur, "source": "transcript-estimate",
                  "tool": "claude-code"},
        ))
    for agent, v in shape.get("by_agent", {}).items():
        out.append(Event(
            ts_utc=ts, domain="ai", metric="agent_tokens",
            value_num=v["tokens"], value_text=agent, source=SOURCE,
            meta={"calls": v["calls"], "cost_estimate": round(v["cost"], 4),
                  "currency": cur, "source": "transcript-estimate"},
        ))

    if balance:
        out.append(Event(
            ts_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            domain="ai", metric="balance",
            value_num=balance["balance"], value_text=balance["currency"], source=SOURCE,
            meta={"topped_up": balance["topped_up"], "granted": balance["granted"]},
        ))
    return out


def main() -> None:
    from common import cli_day

    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="today")
    ap.add_argument("--days", type=int, default=1,
                    help="回填多少天（平台接口一次返回整月，回填很便宜）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    day = Day.parse(cli_day(args.date), TZ)
    header(f"Collect AI usage · {day.iso}")

    print("-> DeepSeek platform usage (exact)")
    env = CFG["ai"].get("deepseek_user_token_env", "DEEPSEEK_USER_TOKEN")
    token = os.environ.get(env)
    platform = None
    if not token:
        print(f"  . {env} is not set -- cannot read the exact numbers.")
        print("    See this file's header for how to copy it out of the browser.")
    else:
        platform = read_platform(day, token, args.days)
        if platform:
            for date in sorted(platform):
                models = platform[date]
                total = sum(v["cost"] for v in models.values())
                reqs = sum(v["requests"] for v in models.values())
                print(f"  OK  {date}  {total:8.2f} {CFG['budget'].get('currency','CNY')}"
                      f"  ({reqs} requests)")
                for model, v in sorted(models.items(), key=lambda x: -x[1]["cost"]):
                    print(f"        {model:30s} {v['cost']:8.2f}  "
                          f"hit {v['in_hit']:>12,.0f}  miss {v['in_miss']:>10,.0f}"
                          f"  out {v['out']:>9,.0f}  req {v['requests']:5d}")

    print("-> Claude Code transcripts (shape only, approximate)")
    shape = shape_from_transcripts(day)
    if shape["by_project"]:
        for proj, v in sorted(shape["by_project"].items(), key=lambda x: -x[1]["tokens"]):
            print(f"  ~   {proj:34s} {v['tokens']:>13,} tokens  ({v['calls']} msgs)")
    else:
        print("  . no Claude Code messages today")

    print("-> DeepSeek balance")
    balance = read_balance()
    if balance:
        print(f"  OK  {balance['balance']:.2f} {balance['currency']} left")
    else:
        print("  . unavailable")

    events = events_for(day, platform, shape, balance)
    if not events:
        print("\nNothing to write.")
        return
    if args.dry_run:
        print(json.dumps([e.to_payload() for e in events], ensure_ascii=False, indent=2)[:2500])
        return

    client = EventsClient(CFG)
    if not client.ping():
        print(f"\nFAIL events API is not running ({client.base})")
        return

    # 先删这些天的旧行再写。唯一索引是 (source, domain, metric, ts_utc, value_text)，
    # **不含 value_num** —— 不删的话当天数值一变就写不进去。
    # 平台接口一次返回整月，所以回填时要把它覆盖到的每一天都清一遍。
    for d in sorted({day.iso} | set(platform or {})):
        try:
            httpx.delete(f"{client.base}/day", headers=client.headers,
                         params={"day": d, "source": SOURCE}, timeout=30.0)
        except Exception:  # noqa: BLE001
            pass

    n = client.post_batch(events)
    print(f"\nOK  Written to events table: {n} rows")


if __name__ == "__main__":
    main()
