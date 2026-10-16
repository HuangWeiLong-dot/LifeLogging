#!/usr/bin/env python3
"""把「电脑上今天到目前」高频推到服务器中继 —— 让飞书机器人答得了电脑的事。

**为什么要有它**

    机器人以前对「我今天在电脑上干了什么」只能说「答不了」，理由是那部分数据
    在笔记本上，而笔记本一天只醒约 3.4 小时。**那个理由是对的** —— 它确实
    答不了提问那一刻正在发生的事。

    但「答不了」有两种，值得分开：

      · 笔记本**醒着** → 数据就在手边，答不了是设计缺陷
      · 笔记本**睡着** → 真的没有新数据，但**最后知道的样子**是有的

    这个脚本解决第二种里能解决的那部分：笔记本醒着的时候，每隔几分钟把
    「现在在干什么 + 今天到目前的分布」推一份到服务器。这样等它睡了，
    服务器上还留着最后的样子，机器人可以答
    「电脑上最后记录是 14:32，在写 feishu_bot.py（3 小时前）」——
    而不是干巴巴一句「不知道」。**并且会把「这是几小时前的」说出来。**

**它不重写任何规则**

    时间切片、分类、短块折叠、黑洞判定，全部调 ``normalize`` 里现成的函数 ——
    和写日记用的是**同一条流水线**。自己再拼一套的话，机器人报的「今天在
    Work 上花了 2.1 小时」会和日记里的数字对不上，而两边都不报错。

**它不是采集器**

    原始数据仍在 ActivityWatch 里、仍由 ``collect_desktop.py`` 进事件表。
    这里只做「把已经算好的东西抄一份给服务器」，所以推的是一份**小快照**，
    不是事件流。中继侧也只留最新一份（见 ``save_desktop()``）。

用法：
  python scripts/push_desktop.py                # 推今天到目前
  python scripts/push_desktop.py --dry-run      # 只打印，不推
  python scripts/push_desktop.py --date 2026-10-15
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import Day, header, load_config  # noqa: E402
from normalize import (  # noqa: E402
    attach_web_urls,
    build_blocks,
    classify,
    fetch_web_slices,
    fetch_window_slices,
    fold_short_blocks,
    mark_blackholes,
)

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])

# 推上去最多留几个。前几个才有信息量，后面的是长尾；
# 中继那边还会再截一次（DESKTOP_MAX_APPS）。
TOP_APPS = 15
TOP_TITLES = 8


def relay() -> tuple[str, str] | None:
    """中继地址和 key。

    ⚠️ 占位符要**两个都查**：配置模板里写的是 `REPLACE_ME`，而几个老脚本
       只查 `REPLACE_WITH` —— 于是 `api_key: "REPLACE_ME"` 会一路通过检查，
       最后以一个 401 的形式出现，看不出是「你忘了填」。
    """
    cfg = (CFG.get("sources") or {}).get("phone_relay") or {}
    if not cfg.get("enabled"):
        print("-> phone relay disabled (config.yaml -> sources.phone_relay.enabled)")
        return None
    base = (cfg.get("api_base") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    if (not base or "REPLACE_WITH" in base or "REPLACE_ME" in base
            or not key or "REPLACE_WITH" in key or "REPLACE_ME" in key):
        print("  ⚠️  phone_relay 开着，但 config.yaml 里还是占位符（没填服务器地址或 key）")
        return None
    return base, key


def _hhmm(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")


def current_activity(slices: list[dict], blocks: list) -> dict | None:
    """现在在干嘛：最后一个切片的应用/标题，外加它这一段持续了多久。

    ⚠️ 「持续多久」用**同一个应用的连续区间**，不是最后一个块的长度 ——
       块是按分类合并过的，可能把几段不同应用并成一个，拿它当「这一段」
       会明显偏长，看着像在同一个窗口里坐了两小时。
    """
    if not slices:
        return None
    last = slices[-1]
    app = str(last.get("app") or "")
    title = str(last.get("title") or "")

    # 往回走，直到应用变了 —— 那就是这一段的起点。
    since = last["ts"]
    for s in reversed(slices[:-1]):
        if str(s.get("app") or "") != app:
            break
        since = s["ts"]

    cat, conf = classify(app, title)
    # 块里的分类可能被后一步（LLM 复核）改过，能对上就用它的。
    for b in blocks:
        if b.start <= last["ts"] < b.end:
            cat, conf = b.category, b.confidence
            break

    if cat == "__drop__":
        # 最后一条是锁屏 / 屏保这种 —— 那不算「在干什么」。
        # 返回 None，让机器人老实说「最后记录在 N 分钟前」，而不是报
        # 「你正在用 LockApp.exe」。
        return None

    return {
        "app": app,
        "title": title,
        "category": cat,
        "confidence": conf,
        "since": _hhmm(since),
        "minutes": round((last["ts"] - since).total_seconds() / 60.0, 1),
    }


def afk_now(base: str, now: datetime) -> bool | None:
    """现在在不在电脑前。取最近 10 分钟的 AFK 事件里的最后一条。

    ⚠️ ``collect_desktop.py`` 只算**当天 AFK 总时长**，没有「此刻」这个概念。
       所以这里直接问 ActivityWatch —— 用的是它现成的 ``aw_events``（本来就是
       任意时间区间的），不是另写一套查询。

       没装 afk watcher、或者拿不到 → 返回 None，**不猜**。
    """
    from collect_desktop import aw_buckets, aw_events

    try:
        buckets = aw_buckets(base)
    except Exception:  # noqa: BLE001
        return None
    names = sorted(b for b in buckets if b.startswith("aw-watcher-afk_"))
    if not names:
        return None
    try:
        evs = aw_events(base, names[-1], now - timedelta(minutes=10), now)
    except Exception:  # noqa: BLE001
        return None
    evs = [e for e in evs if isinstance(e, dict)]
    if not evs:
        return None
    last = max(evs, key=lambda e: str(e.get("timestamp") or ""))
    status = str((last.get("data") or {}).get("status") or "").lower()
    if status == "afk":
        return True
    if status == "not-afk":
        return False
    return None


def build_payload(day: Day) -> dict:
    slices = fetch_window_slices(day)
    web = fetch_web_slices(day)
    if web and slices:
        attach_web_urls(slices, web)

    blocks, unmatched = build_blocks(slices)
    blocks = fold_short_blocks(blocks)
    mark_blackholes(blocks, CFG.get("blackhole") or {})

    by_cat: dict[str, float] = {}
    for b in blocks:
        by_cat[b.category] = by_cat.get(b.category, 0.0) + b.hours

    # ⚠️ 应用榜从**块**里算，不是从原始切片。切片里混着 LockApp.exe / 屏保
    #    这类噪音（classify 给它们 `__drop__`，build_blocks 会丢掉），
    #    从切片直接统计的话榜首会是一条「你今天在锁屏上花了 1.2 小时」。
    #    从块里算 = 和日记报的同一层数据。
    app_secs: dict[str, float] = {}
    for b in blocks:
        if b.app:
            app_secs[b.app] = app_secs.get(b.app, 0.0) + b.seconds

    # 标题没法从块里拆（block.seconds 是整块的，不分标题），所以还是走切片，
    # 但用**同一个 classify** 把噪音滤掉 —— 不另立一份噪音名单。
    title_secs: dict[str, float] = {}
    for s in slices:
        t = " ".join(str(s.get("title") or "").split())[:120]
        if not t:
            continue
        if classify(str(s.get("app") or ""), t)[0] == "__drop__":
            continue
        title_secs[t] = title_secs.get(t, 0.0) + float(s.get("dur") or 0.0)

    now = datetime.now(TZ)
    aw_base = (CFG.get("sources") or {}).get("activitywatch", {}).get("api_base", "")

    return {
        "day": day.iso,
        "pushed_at": now.isoformat(timespec="seconds"),
        # ⚠️ 这个字段是整份快照里**最重要**的一个：它让机器人能说出
        #    「这是 3 小时前的」。没有它，一份陈旧的快照会被当成「现在」——
        #    那是这个功能最坏的失败方式（安静地答错）。
        "last_event": (slices[-1]["ts"].isoformat(timespec="seconds")
                       if slices else None),
        "afk": afk_now(aw_base, now) if aw_base else None,
        "current": current_activity(slices, blocks),
        "today": {
            "total_hours": round(sum(by_cat.values()), 2),
            "by_category": [[k, round(v, 2)] for k, v in
                            sorted(by_cat.items(), key=lambda x: -x[1])],
            "top_apps": [[k, round(v / 3600.0, 2)] for k, v in
                         sorted(app_secs.items(), key=lambda x: -x[1])[:TOP_APPS]],
            "top_titles": [[k, round(v / 3600.0, 2)] for k, v in
                           sorted(title_secs.items(), key=lambda x: -x[1])[:TOP_TITLES]],
            "slices": len(slices),
            "unmatched_apps": [k for k, _ in
                               sorted(unmatched.items(), key=lambda x: -x[1])[:10]],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="把电脑当天到目前的快照推给服务器中继")
    ap.add_argument("--date", default="today")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    day = Day.parse(args.date, TZ)
    header(f"Push desktop · {day.iso}")

    if not (CFG.get("sources") or {}).get("activitywatch", {}).get("enabled", True):
        print("  . ActivityWatch 关着（config.yaml -> sources.activitywatch.enabled）")
        return 0

    try:
        payload = build_payload(day)
    except httpx.HTTPError as e:
        # ⚠️ 采集源缺席时**优雅降级**，和仓库里其它采集脚本一致：
        #    笔记本刚开机、ActivityWatch 还没起来，是这个脚本最正常的失败。
        #    报一句能操作的话就走，不要抛栈 —— 它每 10 分钟跑一次。
        print(f"  ⚠️  连不上 ActivityWatch（{e}）。它没在跑？还是刚开机还没起来？")
        return 0

    n = payload["today"]["slices"]
    cur = payload["current"]
    if not n:
        print("  . 今天还没有窗口切片（ActivityWatch 没采到，或者今天还没用电脑）")
    else:
        print(f"  ✓ {n} 个切片，最后一条 {payload['last_event']}")
        if cur:
            print(f"  ✓ 现在：{cur['app']} · {cur['title'][:60]}")
            print(f"      已持续 {cur['minutes']} 分钟，分类 {cur['category']}"
                  f"（{cur['confidence']}）")
        print(f"  ✓ 今天合计 {payload['today']['total_hours']} 小时："
              + " · ".join(f"{k} {v}h" for k, v in payload["today"]["by_category"]))

    if args.dry_run:
        import json
        print("\n--dry-run，不推。payload：")
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
        return 0

    endpoint = relay()
    if not endpoint:
        return 1
    base, key = endpoint
    try:
        r = httpx.post(f"{base}/desktop", json=payload,
                       headers={"X-API-Key": key}, timeout=20.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  连不上中继 {base}：{e}")
        return 1
    if r.status_code != 200:
        print(f"  ⚠️  中继返回 {r.status_code}：{r.text[:200]}")
        return 1
    print(f"  OK  已推到中继（{r.json().get('detail')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
