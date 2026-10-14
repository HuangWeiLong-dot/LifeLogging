"""手机使用事件：服务器中继 → 本机事件表。

手机（Tasker）把「解锁 / 应用切换」随时 POST 到**服务器**上的中继
（`server/phone-relay/`），本脚本在流水线里把当天那一份拉过来。

**为什么绕这一圈，不直连**

    本机是笔记本，一天只醒几小时（实测 24 小时里只有 3.4 小时是醒的）。
    Tasker 的 HTTP 请求没有队列，电脑睡着时那一发直接失败、数据静默丢失。
    服务器是 24 小时开着的，所以让手机只对服务器说话，
    本机醒着的时候主动来拉 —— 跟位置数据（Dawarich）是同一套形状。

    位置：collect_phone.py         ← Dawarich
    使用：collect_phone_events.py  ← 服务器中继

**幂等**

    本地事件表在 `(source, domain, metric, ts_utc, value_text)` 上有唯一索引，
    重复拉取会被静默忽略。所以放心重跑，中继那份原始文件也不必删。

用法：
  python scripts/collect_phone_events.py --date today
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Day, Event, EventsClient, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])


def fetch_day(day: Day) -> list[dict[str, Any]] | None:
    """从中继拿某一天的事件。

    返回：
      · list —— 拿到了（可能是空的：那天手机没上报）
      · None —— **拿不到**（没配、连不上、鉴权失败）。

    ⚠️ 这两种必须分开。原来失败也返回 `[]`，于是「服务器没起来」和
       「今天手机没用过」在输出上长得一模一样，只看得到一句
       「Nothing to pull」。跟这个项目里踩过的其它坑是同一类：
       **失败静默，最后表现为「没数据」，没人知道该去修什么。**
    """
    cfg = (CFG.get("sources") or {}).get("phone_relay") or {}
    if not cfg.get("enabled"):
        print("-> phone relay disabled (config.yaml -> sources.phone_relay.enabled)")
        return None

    base = (cfg.get("api_base") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    if not base or "REPLACE_WITH" in base or "REPLACE_WITH" in key:
        print("  ⚠️  phone_relay is enabled but still has placeholder values in config.yaml.")
        print("      Fill in api_base and api_key — see server/phone-relay/README.md")
        return None

    try:
        r = httpx.get(f"{base}/events", params={"day": day.iso},
                      headers={"X-API-Key": key}, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Could not reach the phone relay at {base}: {e}")
        print("      Is this machine on the tailnet? Is the service running on the server?")
        print("      On the server:  sudo systemctl status lifelogging-relay")
        return None

    if r.status_code == 401:
        print("  ⚠️  phone relay rejected the API key (401).")
        print("      Compare sources.phone_relay.api_key with /etc/lifelogging/relay.env")
        return None
    if r.status_code != 200:
        print(f"  ⚠️  phone relay returned {r.status_code}: {r.text[:200]}")
        return None

    data = r.json()
    events = data.get("events") if isinstance(data, dict) else data
    if not isinstance(events, list):
        print(f"  ⚠️  Unrecognised relay response: {str(data)[:200]}")
        return None
    return events


def to_events(raw: list[dict[str, Any]], day: Day) -> list[Event]:
    """中继里的一行 → 事件表的一条。

    ⚠️ `value_text` 是应用标识（包名或应用名），原样保留：
       翻译成可读名是 normalize 的事（`PHONE_APPS`），不是在采集阶段。
       采集层存原始值，展示层做解释 —— 反过来的话，以后想改映射就得回头改历史数据。
    """
    out: list[Event] = []
    for e in raw:
        ts = e.get("ts_utc")
        met = e.get("metric")
        if not ts or not met:
            continue                      # 中继已经滤过一轮，这里再兜一次
        out.append(
            Event(
                ts_utc=str(ts),
                domain=e.get("domain") or "phone",
                metric=str(met),
                value_num=e.get("value_num"),
                value_text=e.get("value_text"),
                source=e.get("source") or "tasker",
                meta=e.get("meta") or {},
            )
        )
    return out


def main() -> None:
    from common import cli_day

    day = Day.parse(cli_day(), TZ)
    header(f"Collect phone events · {day.iso}")

    raw = fetch_day(day)
    if raw is None:
        # 连不上 / 没配好 —— 上面已经打印了具体原因，这里只收尾
        print("  ✗ Could not read from the phone relay (see the warning above).")
        return
    if not raw:
        print("  . Relay reached, but it has no events for this day.")
        print("    Normal before Tasker is set up on the phone.")
        return

    unlocks = sum(1 for e in raw if e.get("metric") == "unlock")
    switches = sum(1 for e in raw if e.get("metric") == "app_switch")
    others = len(raw) - unlocks - switches
    print(f"  OK  Relay has {len(raw)} events for {day.iso}: "
          f"{unlocks} unlocks, {switches} app switches"
          + (f", {others} other" if others else ""))

    events = to_events(raw, day)
    if not events:
        print("  ⚠️  Nothing usable after filtering (missing ts_utc or metric).")
        return

    client = EventsClient(CFG)
    if not client.ping():
        print(f"\nFAIL events API is not running ({client.base})")
        return

    n = client.post_batch(events)
    dup = len(events) - n
    print(f"\nOK  Written to events table: {n} rows"
          + (f" ({dup} were already there — the table dedups, this is expected "
             f"when re-pulling a day)" if dup else ""))


if __name__ == "__main__":
    main()
