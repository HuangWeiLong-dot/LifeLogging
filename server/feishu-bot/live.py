#!/usr/bin/env python3
"""服务器侧的实时数据 —— 飞书机器人「随时问我今天」那一半。

**这一层存在的全部理由：别写第二份实现。**

    「手机用了多久」这个数字，日记里已经有一套算法（``normalize.summarize_phone``），
    「今天去过哪」也有一套（``collect_phone.derive_stays``）。如果机器人自己再写
    一份，两份**一定会**在不同的时候给出不同的数 —— 而两边都不报错，你只是
    某天发现机器人说 3 小时、日记说 2 小时，然后不知道该信谁。

    所以这里只做两件事：**调用**已有的函数，和把结果**渲染成人话**。
    一行统计逻辑都不重写。

**「实时」的边界（必须对用户说实话）**

    | 数据 | 在哪 | 能不能即时答 |
    |---|---|---|
    | 位置 / 停留（Dawarich） | 服务器 | ✅ |
    | 手机使用（中继） | 服务器 | ✅ |
    | 手表步数 / 心率 | 服务器（手机定时上传的快照） | 稍后 |
    | **电脑 ActivityWatch / 日记 / vault** | **笔记本** | ❌ **答不了** |

    笔记本一天只醒约 3.4 小时，所以它那部分数据在提问那一刻根本不在服务器上。
    **一个在这里瞎猜的机器人，比一个老实说「没数据」的机器人更糟** ——
    所以 ``答不了`` 是这段代码里的一等公民，不是兜底。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

# 复用 scripts/ 下现成的实现。做法和 close_day.py 一样。
REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

TZ_NAME = "Asia/Shanghai"


# ── 惰性依赖 ────────────────────────────────────────────────────────
_mods: dict[str, Any] = {}


class LiveUnavailable(RuntimeError):
    """拿不到实时数据。**不是**崩溃 —— 调用方应该把它当成一句要转达给用户的话。"""


def deps() -> dict[str, Any]:
    """惰性 import 那几个脚本模块，失败时报清楚。

    ⚠️ **必须惰性，不能放在模块顶层。**

        ``normalize.py`` 和 ``collect_phone.py`` 在 **import 的那一刻**就
        ``CFG = load_config()``，而配置缺失时 ``load_config()`` 是直接
        ``sys.exit(1)`` 的。放在顶层的话，Dawarich 的 key 没填就会让**整个
        机器人起不来** —— 可那是两件事：每天提问那条主链路只依赖中继，
        和 Dawarich 一点关系都没有。

        所以放到函数里，失败只影响「查数据」这一个功能，而且话要说清楚。
    """
    if not _mods:
        try:
            from common import Day, load_config           # noqa: F401
            from normalize import summarize_phone         # noqa: F401
            from collect_phone import (                   # noqa: F401
                derive_stays, fetch_points,
            )
            import geo                                    # noqa: F401
        except SystemExit as e:                           # load_config 的 sys.exit
            raise LiveUnavailable(
                "服务器上的 config/config.yaml 读不了（多半是还没建，或者 "
                f"sources.dawarich 没填）。查数据的功能暂时不可用。（exit {e.code}）"
            ) from None
        except Exception as e:  # noqa: BLE001
            raise LiveUnavailable(f"加载数据模块失败：{type(e).__name__}: {e}") from None

        from common import Day, load_config
        from normalize import summarize_phone
        from collect_phone import derive_stays, fetch_points
        import geo

        cfg = load_config()
        tz = ZoneInfo(cfg.get("timezone") or TZ_NAME)
        _mods.update(cfg=cfg, tz=tz, Day=Day, summarize_phone=summarize_phone,
                     derive_stays=derive_stays, fetch_points=fetch_points, geo=geo)
    return _mods


def warm_geocoding() -> None:
    """后台预热 GeoNames 数据表。

    ⚠️ **第一次**要用到地名时会去下载 30–40 MB（``geo.ensure_dataset``）。
        那一下要是发生在处理用户消息的线程里，机器人会显得「卡死」很久，
        而且期间其它消息全排在后面。

        所以启动时先在一个后台线程里把这件事做掉。失败无所谓 ——
        真用的时候会退回坐标，并且明说。
    """
    try:
        g = deps()["geo"]
    except LiveUnavailable:
        return
    try:
        ok = g.ensure_dataset(quiet=True)
        print(f"[geo] 城市数据表{'就绪' if ok else '不可用（会退回坐标）'}")
    except Exception as e:  # noqa: BLE001
        print(f"[geo] 预热失败（会退回坐标）：{type(e).__name__}: {e}", file=sys.stderr)


# ── 取数 ────────────────────────────────────────────────────────────
def relay_events(day_iso: str, base: str, key: str) -> list[dict]:
    """当天中继上的手机事件。**答案事件也混在里面**（domain=question），
    但 summarize_phone 按 metric 过滤，不会把它们当成手机使用。"""
    r = httpx.get(f"{base}/events", params={"day": day_iso},
                  headers={"X-API-Key": key}, timeout=20.0)
    r.raise_for_status()
    return r.json().get("events") or []


def relay_qa(day_iso: str, base: str, key: str) -> list[dict]:
    """当天作答的全部问答（早晨批 + 实时批 + 收尾批）。

    ⚠️ 旧版中继没有 /qa，返回 404 —— 这时返回空列表，让调用方退回
       summary-only 的老行为，而不是把复盘整条弄挂。
    """
    try:
        r = httpx.get(f"{base}/qa", params={"day": day_iso},
                      headers={"X-API-Key": key}, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        print(f"WARN 读 /qa 失败：{type(e).__name__}: {e}", file=sys.stderr)
        return []
    if r.status_code == 404:
        print("WARN 中继没有 /qa（服务器还是旧版）", file=sys.stderr)
        return []
    try:
        r.raise_for_status()
        qa = r.json().get("qa")
    except Exception as e:  # noqa: BLE001
        print(f"WARN /qa 返回不可用：{type(e).__name__}: {e}", file=sys.stderr)
        return []
    return [x for x in (qa or []) if isinstance(x, dict) and x.get("answer")]


def relay_desktop(base: str, key: str) -> dict | None:
    """笔记本高频推上来的「电脑上今天到目前」快照。没推过返回 None。"""
    r = httpx.get(f"{base}/desktop", headers={"X-API-Key": key}, timeout=20.0)
    r.raise_for_status()
    snap = r.json().get("snapshot")
    return snap if isinstance(snap, dict) else None


def relay_watch(base: str, key: str) -> dict | None:
    """笔记本推来的健康日汇总（步数/心率/睡眠/运动）。没推过返回 None。"""
    r = httpx.get(f"{base}/watch", headers={"X-API-Key": key}, timeout=20.0)
    r.raise_for_status()
    snap = r.json().get("snapshot")
    return snap if isinstance(snap, dict) else None


def location_stays(day_iso: str) -> list[dict]:
    """当天的停留，按时间排序。

    走的是 ``collect_phone`` 那一套（Dawarich 原始点 → 本地聚类 → 离线反查地名），
    和日记用的**完全是同一个函数**，所以数字一定对得上。
    """
    m = deps()
    day = m["Day"].parse(day_iso, m["tz"])

    try:
        pts = m["fetch_points"](day)
    except Exception as e:  # noqa: BLE001
        raise LiveUnavailable(f"拉不到位置数据（Dawarich）：{type(e).__name__}: {e}") from None

    if not pts:
        return []

    try:
        stays = m["derive_stays"](day, pts)
    except Exception as e:  # noqa: BLE001
        raise LiveUnavailable(f"推导停留失败：{type(e).__name__}: {e}") from None

    out: list[dict] = []
    for ev in stays:
        meta = ev.meta or {}
        out.append({
            "place": ev.value_text or "",
            "minutes": float(ev.value_num or 0),
            "start": _hhmm(ev.ts_utc, m["tz"]),
            "end": _hhmm(meta.get("end_utc"), m["tz"]),
            # 反查失败时 value_text 会是 "(39.9042,116.4074)" 这种坐标。
            # 标出来，好在渲染时老实告诉用户地名不可用。
            "is_coord": (ev.value_text or "").startswith("("),
            "points": int(meta.get("points") or 0),
        })
    return out


def _hhmm(iso: Any, zone: ZoneInfo) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(str(iso)).astimezone(zone).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


# ── 渲染 ────────────────────────────────────────────────────────────
def _hm(minutes: float) -> str:
    """分钟 → 「2 小时 14 分」。"""
    total = int(round(minutes))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} 小时 {m} 分"
    if h:
        return f"{h} 小时"
    return f"{m} 分"


def render_location(day_iso: str) -> str:
    stays = location_stays(day_iso)
    if not stays:
        return f"位置 · {day_iso}\n没有位置数据。"

    total = sum(s["minutes"] for s in stays)
    lines = [f"位置 · {day_iso}", ""]
    for s in stays:
        span = f"{s['start']}–{s['end']}" if s["start"] and s["end"] else "—"
        place = s["place"] + ("（坐标）" if s["is_coord"] else "")
        lines.append(f"  {span}  {place}  ·  {_hm(s['minutes'])}")
    lines += ["", f"合计 {_hm(total)}。"]
    return "\n".join(lines)


def render_phone(events: list[dict], day_iso: str | None = None) -> str:
    try:
        s = deps()["summarize_phone"](events)
    except LiveUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise LiveUnavailable(f"汇总手机数据失败：{type(e).__name__}: {e}") from None

    if not s["unlocks"] and not s["app_switches"]:
        return "今天没有手机事件。"

    # ⚠️ 「有屏幕数据」和「屏幕时间恰好是 0」是两回事：没有屏幕事件时
    #    只写「—」，不能写「0 分钟」。
    screen_evs = [e for e in events
                  if e.get("metric") in ("screen_on", "screen_off")]

    lines = [f"手机 · {day_iso}" if day_iso else "手机", ""]
    if screen_evs:
        lines.append(f"  屏幕亮着     {_hm(s['screen_minutes'])}")
        lines.append(f"  前台合计     {_hm(s['foreground_minutes'])}（下界）")
    else:
        lines.append("  屏幕时间     —")
    lines.append(f"  解锁         {s['unlocks']} 次")
    if s["first_unlock"]:
        lines.append(f"  首次 / 末次  {s['first_unlock']} / {s['last_unlock']}")
    lines.append(f"  应用切换     {s['app_switches']} 次")

    top = s["top_apps"][:6]
    if top:
        lines += ["", "  用得最多："]
        for name, mins in top:
            lines.append(f"    {name}  {_hm(mins)}")

    unknown = s["unknown_apps"][:5]
    if unknown:
        # 聊天里不展开；留在日志里给维护者看。
        print("  unknown apps: "
              + " · ".join(f"{p}({_hm(m)})" for p, m in unknown),
              file=sys.stderr)
    return "\n".join(lines)


def data_snapshot(day_iso: str, base: str, key: str) -> list[str]:
    """当天数据的**紧凑**快照（几行）。每一块各自兜错，缺哪块少哪行。"""
    out: list[str] = []

    try:
        events = relay_events(day_iso, base, key)
    except Exception as e:  # noqa: BLE001
        events = []
        print(f"[recap] 手机事件拿不到：{type(e).__name__}: {e}", file=sys.stderr)
    if events:
        try:
            s = deps()["summarize_phone"](events)
            line = f"手机：解锁 {s['unlocks']} 次 · 切换 {s['app_switches']} 次"
            if s.get("screen_minutes"):
                line += f" · 亮屏 {_hm(s['screen_minutes'])}"
            out.append(line)
        except Exception:  # noqa: BLE001
            pass

    # 电脑快照只有「最新一份」，没有历史；查过去某天时不要拿今天那份冒充。
    if day_iso == datetime.now(ZoneInfo(TZ_NAME)).date().isoformat():
        try:
            snap = relay_desktop(base, key) or {}
            today = snap.get("today") or {}
            total = today.get("total_hours")
            if total:
                cats = " / ".join(f"{k} {v}h" for k, v in
                                  (today.get("by_category") or [])[:4])
                out.append(f"电脑：{total} 小时（{cats}）")
        except Exception:  # noqa: BLE001
            pass

    try:
        stays = location_stays(day_iso)
        if stays:
            best = max(stays, key=lambda s: s["minutes"])
            out.append(f"位置：{len(stays)} 处，最久 {best['place']}"
                       f"（{_hm(best['minutes'])}）")
    except Exception:  # noqa: BLE001
        pass

    try:
        snap = relay_watch(base, key) or {}
        health = snap.get("health") or {}
        steps = health.get("steps")
        if steps is not None:
            out.append(f"手表：{int(round(float(steps)))} 步"
                       f"（{snap.get('day') or '—'}）")
    except Exception:  # noqa: BLE001
        pass

    return out


_QA_BUCKETS = ("白天的实时回答", "早晨的问责与常规", "收尾回答", "其他回答")


def _norm_qa(qa: list[Any] | None) -> list[dict]:
    """把旧版 `(问, 答)` 和新版 `/qa` 字典统一成一种形状。

    ⚠️ 保留备注。备注里常常是「为什么没做到」，比 yes/no 本身有价值得多。
    """
    out: list[dict] = []
    for x in qa or []:
        if isinstance(x, dict):
            item = {
                "text": str(x.get("text") or ""),
                "answer": str(x.get("answer") or x.get("value") or ""),
                "note": str(x.get("note") or ""),
                "kind": str(x.get("kind") or ""),
                "source": str(x.get("source") or ""),
                "id": str(x.get("id") or ""),
                "ts_utc": str(x.get("ts_utc") or ""),
            }
        elif isinstance(x, (tuple, list)) and len(x) >= 2:
            item = {
                "text": str(x[0] or ""),
                "answer": str(x[1] or ""),
                "note": str(x[2]) if len(x) > 2 and x[2] else "",
                "kind": "", "source": "", "id": "",
            }
        else:
            continue
        if item["text"].strip() or item["answer"].strip():
            out.append(item)
    return out


def _qa_bucket(x: dict) -> str:
    if x.get("kind") == "summary":
        return "收尾回答"
    if x.get("source") == "laptop":
        return "早晨的问责与常规"
    if x.get("source") == "live":
        return "白天的实时回答"
    return "其他回答"


def _one_line(s: Any, limit: int) -> str:
    return " ".join(str(s or "").split())[:limit]


def _qa_line(x: dict, q_limit: int = 56, a_limit: int = 120) -> str:
    q = _one_line(x.get("text") or "(题号不在问题文件里)", q_limit)
    a = _one_line(x.get("answer") or "", a_limit)
    n = _one_line(x.get("note") or "", a_limit)
    return f"{q} → {a}" + (f"（{n}）" if n else "")


def render_qa(day_iso: str, base: str, key: str) -> str:
    """某一天的全部问答，一条一行。只给事实，不解释。"""
    qa = _norm_qa(relay_qa(day_iso, base, key))
    lines = [f"问答 · {day_iso}"]
    if not qa:
        lines.append("没有回答。")
        return "\n".join(lines)

    lines.append("")
    tz = ZoneInfo(TZ_NAME)
    for x in qa:
        ts = _hhmm(x.get("ts_utc"), tz)
        lines.append((f"{ts}  " if ts else "") + _qa_line(x, q_limit=50, a_limit=100))
    return "\n".join(lines)


def day_recap(day_iso: str, base: str, key: str,
              qa: list[Any], sections: dict | None) -> str:
    """一天的收尾回执：数据 + 你的回答 + 总结 + 今天做了什么 + 明天的建议。

    ⚠️ 数据和回答都是**确定性的**（直接拼），只有后三块走模型。
       这样即使 LLM 挂了、额度用完了，复盘里仍然有今天的事实和你自己说的话 ——
       那才是这份东西的主体。
    """
    lines = [f"今日复盘 · {day_iso}", ""]

    snap = data_snapshot(day_iso, base, key)
    if snap:
        lines.append("【数据】")
        lines += [f"· {x}" for x in snap]
        lines.append("")

    q_all = _norm_qa(qa)
    if q_all:
        lines.append("【你的回答】")
        for label in _QA_BUCKETS:
            group = [x for x in q_all if _qa_bucket(x) == label]
            if not group:
                continue
            lines.append(f"· {label}")
            for x in group:
                lines.append(f"  {_qa_line(x)}")
        lines.append("")

    sec = sections or {}
    summary = str(sec.get("summary") or "").strip()
    did = [str(x).strip() for x in (sec.get("did") or []) if str(x).strip()]
    advice = [str(x).strip() for x in (sec.get("advice") or []) if str(x).strip()]

    if summary:
        lines += ["【总结】", summary, ""]

    if did:
        lines.append("【今天做了什么】")
        lines += [f"· {x}" for x in did]
        lines.append("")

    if advice:
        # 编号而不是圆点 —— 这是**待办**，明天要照着做的。
        lines.append("【明天的建议】")
        lines += [f"{i}. {x}" for i, x in enumerate(advice, 1)]

    return "\n".join(lines).strip()


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else None


def _ago(v: Any, now: datetime) -> tuple[str, float | None]:
    """「多久之前」。解析不出来就返回 ('', None) —— **不猜**。"""
    dt = _parse_dt(v)
    if dt is None:
        return "", None
    mins = (now - dt).total_seconds() / 60.0
    if mins < -1:
        return "", None         # 时间戳跑到未来去了：多半是两边的钟不一样，别推断
    if mins < 2:
        return "刚刚", mins
    if mins < 60:
        return f"{int(mins)} 分钟前", mins
    h, m = divmod(int(mins), 60)
    if h < 24:
        return (f"{h} 小时 {m} 分前" if m else f"{h} 小时前"), mins
    return f"{h // 24} 天前", mins


DESKTOP_NO_DATA = "还没有电脑快照。笔记本上跑一次 scripts/push_desktop.py。"


def render_desktop(day_iso: str, base: str, key: str) -> str:
    """电脑上今天到目前。日期与「多久之前」放在标题行，正文不再解释。"""
    try:
        snap = relay_desktop(base, key)
    except Exception as e:  # noqa: BLE001
        raise LiveUnavailable(f"拉不到电脑快照：{type(e).__name__}: {e}") from None
    if not snap:
        return DESKTOP_NO_DATA

    # 用快照自己的时区算「多久之前」—— 不依赖 deps()。
    pushed = _parse_dt(snap.get("pushed_at"))
    now = datetime.now(pushed.tzinfo) if pushed else None
    ev_txt, _ = _ago(snap.get("last_event"), now) if now else ("", None)
    push_txt, _ = _ago(snap.get("pushed_at"), now) if now else ("", None)

    day = str(snap.get("day") or "")
    head = f"电脑 · {day or day_iso}"
    fresh = ev_txt or push_txt
    if fresh:
        head += f" · {fresh}"
    lines: list[str] = [head, ""]

    cur = snap.get("current")
    if isinstance(cur, dict) and cur.get("app"):
        lines.append(f"现在在：{cur.get('app')}")
        title = str(cur.get("title") or "").strip()
        if title:
            lines.append(f"  {title[:70]}")
        lines.append(f"  分类 {cur.get('category')} · 已持续 "
                     f"{int(cur.get('minutes') or 0)} 分钟")
    elif snap.get("afk") is True:
        lines.append("现在：AFK")
    else:
        lines.append("现在：没有窗口活动")

    today = snap.get("today") or {}
    label = "今天合计" if (not day or day == day_iso) else "当天合计"
    lines += ["", f"{label} {today.get('total_hours')} 小时"]
    cats = today.get("by_category") or []
    if cats:
        lines.append("  " + " · ".join(f"{k} {v}h" for k, v in cats))
    apps = today.get("top_apps") or []
    if apps:
        lines.append("  用得最多 " + " · ".join(f"{a} {v}h" for a, v in apps[:5]))

    return "\n".join(lines)


WATCH_NO_DATA = "还没有手表汇总。笔记本上跑一次 scripts/push_health.py。"


def render_watch(day_iso: str, base: str, key: str) -> str:
    """手表日汇总：步数 / 心率 / 睡眠 / 最近运动。

    ⚠️ 「这是哪一天的」写在标题行。Gadgetbridge 通常隔天才导出，
       所以最常见的正确回答是「这是昨天的」而不是「今天没走」。
    """
    try:
        snap = relay_watch(base, key)
    except Exception as e:  # noqa: BLE001
        raise LiveUnavailable(f"拉不到健康汇总：{type(e).__name__}: {e}") from None
    if not snap:
        return WATCH_NO_DATA

    pushed = _parse_dt(snap.get("pushed_at"))
    now = datetime.now(pushed.tzinfo) if pushed else None
    push_txt, _ = _ago(snap.get("pushed_at"), now) if now else ("", None)

    day = str(snap.get("day") or "")
    head = f"手表 · {day or day_iso}"
    if push_txt:
        head += f" · {push_txt}"
    health = snap.get("health") if isinstance(snap.get("health"), dict) else {}
    lines = [head, ""]
    shown = False

    steps = health.get("steps")
    if steps is not None:
        lines.append(f"  步数        {int(round(float(steps)))} 步")
        shown = True
    hr_avg = health.get("heart_rate_avg")
    if hr_avg is not None:
        lines.append(f"  心率        平均 {round(float(hr_avg))} bpm")
        shown = True
    hr_min = health.get("heart_rate_min")
    if hr_min is not None:
        lines.append(f"              最低 {round(float(hr_min))} bpm")
    sleep = health.get("sleep_hours")
    if sleep is not None:
        lines.append(f"  睡眠        {_hm(float(sleep) * 60)}")
        shown = True

    if not shown:
        lines.append("  没有步数 / 心率记录")

    if sleep is None:
        # 不能说「睡眠 0 小时」：这台表的导出里没有睡眠样本。
        lines.append("  睡眠        —")

    workouts = [w for w in (snap.get("workouts") or []) if isinstance(w, dict)]
    if workouts:
        lines += ["", "  最近运动："]
        for w in workouts[-3:]:
            text = _one_workout(w)
            if text:
                lines.append(f"    {text}")

    return "\n".join(lines)


def _one_workout(w: dict) -> str:
    """一次运动压成一行。缺的字段直接不写 —— 不编。"""
    bits: list[str] = []
    kind = str(w.get("kind") or "").strip()
    if kind:
        bits.append(kind)
    minutes = w.get("minutes")
    if minutes is not None:
        bits.append(_hm(float(minutes)))
    km = w.get("distance_km")
    if km:
        bits.append(f"{float(km):.1f} km")
    hr = w.get("avg_hr")
    if hr:
        bits.append(f"平均 {int(hr)} bpm")
    return " ".join(bits)


# 电脑**活动**：现在答得了了 —— 笔记本会高频把一份小快照推给服务器
# （scripts/push_desktop.py）。所以这一组不再指向「答不了」。
_LAPTOP_WORDS = ("电脑", "笔记本", "activitywatch", "vscode", "窗口", "写代码",
                 "打代码", "浏览器", "网页", "在忙什么", "在干嘛", "在做什么")

# 日记 / vault：**仍然答不了**，而且原因和上面那条不一样 ——
# 它们不是「笔记本睡着所以没有」，而是**压根没往服务器推**。
# 推上去的只是「电脑活动」那一份小快照，不是整个 Obsidian 库。
_JOURNAL_WORDS = ("日记", "vault", "obsidian")

_HEALTH_WORDS = ("步数", "心率", "睡眠", "运动", "跑步", "手表", "卡路里", "走了多少")

# 明确在问**过去**的话。模板只会答今天，拿今天的数据冒充过去是最坏的一种
# 答错 —— 所以这里直接交给「检索 + 模型」，而不是往下走模板。
# ⚠️ 仍然要过「像在问」那道门（见 _QUESTION_RE）。
_HISTORY_WORDS = ("上次", "上一次", "以前", "之前", "上个月", "那几天",
                  "过去几天", "历史上", "前阵子", "前段时间")
_LOCATION_WORDS = ("去哪", "去了哪", "在哪儿", "在哪", "去过", "行踪", "位置",
                   "停留", "待在哪", "到哪")
_PHONE_WORDS = ("手机", "屏幕", "解锁", "切应用", "应用", "app", "刷了", "玩了多久",
                "用了多久", "用了多长时间")

# 一句话**像不像在问**。`/ask` 里只有像在问才走模板；不像的再去问模型。
#
# ⚠️ 关键词里有一堆日常名词（电脑 / 网页 / 浏览器 / 窗口 / 应用 / 位置…）。
#    只靠名词判断的话，「今天用电脑画了张图」也会被当成查询，拉出一整份
#    电脑报表。能分开它们的只有疑问语气。
_QUESTION_RE = re.compile(
    r"[?？]"                       # 问号
    r"|吗|呢|嘛|啥|什么|怎么|咋"    # 疑问语气词
    r"|多少|多久|多长时间|几次|几个|几天|几小时|几分钟"
    r"|哪(?!怕)"                   # 「哪怕」不是问句
    r"|几(?!乎)"                   # 「几乎」不是问句
    r"|是不是|有没有"
)


def _looks_like_question(text: str) -> bool:
    return bool(_QUESTION_RE.search(text))


def _hits(text: str, words: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(w in low for w in words)


JOURNAL_ANSWER = (
    "日记和 vault 在笔记本的 Obsidian 库里，没有推到服务器。"
)

HEALTH_ANSWER = "手表的健康汇总拉不到（中继出错）。"

UNKNOWN_ANSWER = "没认出这个问题。用 /help 看可用命令。"


def cfg() -> dict:
    """只加载配置，**不碰** deps() 里那两个脚本模块。

    ⚠️ 和 deps() 分开是有原因的：deps() 会因为「Dawarich 没配好」整体失败，
       而自由问答只用得到 budget / ai 那一段，和 Dawarich 毫无关系。
       捆在一起的话，一个 Dawarich 的小问题会连带把自由问答也弄哑。
    """
    if "_cfg" not in _mods:
        try:
            from common import load_config
        except Exception as e:  # noqa: BLE001
            raise LiveUnavailable(f"加载 common 失败：{type(e).__name__}: {e}") from None
        try:
            _mods["_cfg"] = load_config()
        except SystemExit:            # load_config 找不到文件时是 sys.exit(1)
            raise LiveUnavailable(
                "服务器上的 config/config.yaml 读不了，自由问答暂时不可用。"
            ) from None
    return _mods["_cfg"]


# 自由问答每天最多几次。**这个上限不是客套**：它每次都是一次真金白银的
# LLM 调用，而这条通道是「随手问问」，很容易问得比想象中多。
# 10 次 × 单次几分钱 ≈ 一天几毛，和日记那笔（约 ¥0.02/天）同一量级。
LLM_DAILY_CAP = int(os.environ.get("FEISHU_LLM_DAILY_CAP", "10"))

# 自由问答的提示词。**核心要求只有一条：只准用给它的数据，不知道就说不知道。**
# ⚠️ 这里不是「让它聪明一点」的地方 —— 一个把「大概是在开会吧」编得像真事的
#    机器人，比一个说「数据里没有这条」的机器人糟得多。
#    行踪是有隐私含义的数据，编错了会真的误导人。
_LLM_SYSTEM = """你在回答用户关于**他自己**的问题（今天的数据，以及过去写下的日记）。

规则（必须遵守）：
1. 只使用下面【今天的原始数据】和【过去的相关记录】里出现的事实。
   两处都没有的，直接说「记录里没有」，**不要推测、不要补充常识、不要编造
   地点、时间或原因**。
2. 引用【过去的相关记录】时，要说明是哪一天（例如「10 月 18 日那天…」）。
   没找到相关记录就直说没找到，不要拿今天的数据冒充过去。
3. 数据可能不完整或过时（尤其电脑/手表那份）—— 如果数据里有「最后上报」
   或「这是哪一天的」，要把它说出来。
4. 回答用中文，两三句话讲完，不要复述整份数据。
5. 如果问的是这台系统根本没在采集的东西，就说那类数据没有接进来。"""


def _digest(day_iso: str, base: str, key: str) -> str:
    """把今天能拿到的数据凑成一段给模型看。**各部分各自兜错** ——
    一块拿不到不该让整次问答失败。"""
    parts: list[str] = []
    for fn in (lambda: render_location(day_iso),
               lambda: render_phone(relay_events(day_iso, base, key)),
               lambda: render_desktop(day_iso, base, key),
               lambda: render_watch(day_iso, base, key)):
        try:
            parts.append(fn())
        except Exception as e:  # noqa: BLE001
            parts.append(f"（这部分拿不到：{type(e).__name__}: {e}）")
    return "\n\n".join(parts)


def _llm_counter(state_dir: Path, day_iso: str) -> Path:
    return state_dir / f"feishu-llm-{day_iso}.json"


def _llm_used(counter: Path) -> int:
    try:
        return int(json.loads(counter.read_text(encoding="utf-8"))["calls"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return 0


def _llm_bump(counter: Path, day_iso: str, used: int) -> None:
    """记一次调用。⚠️ **成功之后**才调 —— 失败也计的话，一次网络抖动就吃掉一次额度。"""
    try:
        counter.parent.mkdir(parents=True, exist_ok=True)
        tmp = counter.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": day_iso, "calls": used + 1}),
                       encoding="utf-8")
        tmp.replace(counter)
    except OSError as e:
        # 记不上只是额度约束失效，不该把已经拿到的答案丢掉。
        print(f"WARN 自由问答计数写不进去：{e}", file=sys.stderr)


def _run_llm_turn(conf: dict, counter: Path, day_iso: str, *,
                  system: str, user: str, stage: str) -> tuple[str | None, str]:
    """跑一次受每日额度约束的 LLM 调用。返回 (文本, 给人看的失败原因)。

    ⚠️ **每次新建一个 LLM 实例。** 它的 `_calls` 是按实例计的，而且那个计数
       是按「单次流水线运行最多几次」设的（默认 8）。常驻机器人复用同一个
       实例的话，那个上限迟早被撑满，然后**从此再也不回话** —— 而且表现为
       「突然不说话了」，日志上看不出和调用次数有关。

    ⚠️ **成功之后**才计数。失败也计的话，一次网络抖动就会吃掉一次额度。
    """
    from common import BudgetExceeded, CostLedger, LLM, new_run_id

    used = _llm_used(counter)
    if used >= LLM_DAILY_CAP:
        return None, (f"今天的 AI 额度用完了（{LLM_DAILY_CAP} 次）。\n"
                      f"模板那几类（去哪了 / 手机 / 电脑）不受这个限制，照常能问。")

    ledger = CostLedger(conf["budget"]["ledger_path"])
    llm = LLM(cfg=conf, ledger=ledger)
    try:
        out = llm.complete(system=system, user=user, stage=stage,
                           run_id=new_run_id())
    except BudgetExceeded as e:
        return None, f"今天的 AI 预算用完了。（{e}）"
    except Exception as e:  # noqa: BLE001
        print(f"ERROR LLM 调用失败：{type(e).__name__}: {e}", file=sys.stderr)
        return None, f"{type(e).__name__}: {e}"

    _llm_bump(counter, day_iso, used)
    return out.strip(), ""


def answer_freeform(text: str, *, day_iso: str, base: str, key: str) -> str | None:
    """没有现成模板时的自由问答。**绝不用来回答已经能算出来的数字。**

    那些（去哪了、手机多久、解锁几次）走模板 —— 确定性、零成本、不会漂移。
    这里接的是「今天有没有出门三次」这类模板答不了、但数据里其实有答案的问题。
    """
    try:
        conf = cfg()
    except LiveUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise LiveUnavailable(f"自由问答不可用：{type(e).__name__}: {e}") from None

    counter = _llm_counter(Path(conf.get("state_dir") or "."), day_iso)
    digest = _digest(day_iso, base, key)

    # 过去的相关记录：服务器上有 close_day 写好的日记正文，FTS5 检索 top-k。
    # ⚠️ 检索不到就**不附**这一节，让模型看到「没有」而不是一段空标题 ——
    #    空标题会被它读成「有一节政策要求我引用历史」。
    history = ""
    try:
        import recall
        hits = recall.search(text, 5)
        if hits:
            history = "\n\n【过去的相关记录（按相关度排序）】\n" + "\n".join(
                f"- [{h['day']}] {h['excerpt']}" for h in hits)
    except Exception as e:  # noqa: BLE001
        print(f"WARN 历史检索不可用：{type(e).__name__}: {e}", file=sys.stderr)

    out, err = _run_llm_turn(
        conf, counter, day_iso,
        system=_LLM_SYSTEM,
        user=(f"【今天的原始数据】\n{digest}{history}\n\n"
              f"【用户的问题】\n{text}"),
        stage="feishu_ask")
    if out is None:
        return f"问不了：{err}"
    return out


_SUMMARY_SYSTEM = """你在给用户写**一天的收尾复盘**。

输出**一个 JSON 对象**，不要任何代码块之外的文字：

{
  "summary": "3–5 句话，回顾今天",
  "did": ["今天具体做了什么", "..."],
  "advice": ["明天可以执行的一件事", "..."]
}

规则（必须遵守）：

1. 只用下面给的数据，以及用户自己写下的东西。数据里没有的不要说，不要推测。
2. `summary` 三到五句话。不分点、不加标题、不罗列整张表。语气平实，像朋友
   回顾一天 —— 不要打气、不要评价好坏。
3. `did` 是**事实清单**，3–6 条，每条一件具体的事：做了什么、去了哪、推进了什么。
   不要写成「工作 / 生活」这种分类，要写「把飞书机器人的收尾题改成七个维度」。
4. `advice` 是**明天可以执行的具体动作**，1–3 条。不要写「多休息」「保持专注」
   这种空话 —— 要能直接照着做：比如「明天 15:00 前把 AE 的安装验证掉」、
   「把学习通的网课刷完，别拖到周末」。优先参考他回答里「没做成、想留到明天」的那条。
5. 用户自己写的笔记和回答**优先于任何推断**。两者冲突时以用户说的为准。
6. 如果今天记录很少，照实说，不要编。
7. `【白天的实时回答】` 是**当时发生的事实**（每小时问的），`【收尾回答】` 是
   晚上回顾。两者都要用：不要只看收尾那几条，也不要用收尾的概括覆盖掉
   白天更具体的细节。冲突时以时间更近、更具体的那条为准。"""


def _extract_json_obj(raw: str) -> dict | None:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_day_summary(raw: str) -> dict:
    """把模型输出解析成 {summary, did, advice}。

    ⚠️ 解析不出来就**把原文当 summary**，不丢内容 —— 模型偶尔会忘了输出 JSON，
       而那段话本身仍然是有用的。丢掉的代价比格式不规范大得多。
    """
    obj = _extract_json_obj(raw)
    if obj is None:
        return {"summary": (raw or "").strip(), "did": [], "advice": []}

    def _list(v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()]

    return {
        "summary": str(obj.get("summary") or "").strip(),
        "did": _list(obj.get("did"))[:8],
        "advice": _list(obj.get("advice"))[:5],
    }


def write_day_summary(day_iso: str, base: str, key: str,
                      qa: list[Any]) -> tuple[dict | None, str]:
    """晚上收尾：把今天的数据 + 刚答的那些，写成总结 / 做了什么 / 明天建议。

    返回 (三段内容, 给人看的话)。为 None 表示没写成，第二个值是原因。

    ⚠️ 整份复盘会**同时存成一条笔记**（调用方负责），所以它也是「记忆」的一部分。
    """
    try:
        conf = cfg()
    except LiveUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise LiveUnavailable(f"总结不可用：{type(e).__name__}: {e}") from None

    digest = _digest(day_iso, base, key)
    q_all = _norm_qa(qa)
    if q_all:
        chunks: list[str] = []
        for label in _QA_BUCKETS:
            group = [x for x in q_all if _qa_bucket(x) == label]
            if not group:
                continue
            chunks.append(f"【{label}】")
            for x in group:
                qt = _one_line(x.get("text") or "(题号不在问题文件里)", 200)
                at = _one_line(x.get("answer") or "", 300)
                nt = _one_line(x.get("note") or "", 200)
                chunks.append(f"- 问：{qt}\n  答：{at}"
                              + (f"\n  备注：{nt}" if nt else ""))
        answers = "\n".join(chunks)
    else:
        answers = "（今天没有回答记录）"

    counter = _llm_counter(Path(conf.get("state_dir") or "."), day_iso)
    out, err = _run_llm_turn(
        conf, counter, day_iso,
        system=_SUMMARY_SYSTEM,
        user=(f"【今天的原始数据】\n{digest}\n\n"
              f"【今天我问你、你回答的】\n{answers}"),
        stage="feishu_summary")
    if out is None:
        return None, err
    return parse_day_summary(out), ""


def answer_query(text: str, *, day_iso: str, base: str, key: str) -> str | None:
    """认出来就返回答案；认不出来返回 None（/ask 再决定要不要交给模型）。

    ⚠️ 判别的顺序是有意的：**先从窄到宽，把「答不了」和更具体的那几条放前面**。
       一句话里往往同时命中好几组词：
         · 「我今天在电脑上写的日记」→ 日记（vault 没推）比电脑活动更贴题
         · 「我在电脑上手机用了多久」→ 电脑，不是手机
       顺序反了不会报错，只会给出一个**看着像答对了**的答案 —— 最难发现的那种。
    """
    t = text.strip()
    if not t:
        return None
    # ⚠️ 光一个「?」是「给我题」，不是「认不出的查询」—— /help 里就是这么写的。
    #    不在这里挡掉的话，它会掉进下面「带问号 → 明说我能答什么」那条，
    #    于是你回一个问号，收到的是一段能力说明。文档和代码在这儿打过一次架。
    if not t.strip("?？!！。.、,， \t　"):
        return None

    # ⚠️ **不像在问的一律不查。** 这一步必须排在所有关键词判断**前面**：
    #    关键词再准也没用，因为「今天用电脑画了张图」和「我今天在电脑上干了
    #    什么」里都有「电脑」，能分开它们的只有疑问语气，不是名词。
    if not _looks_like_question(t):
        return None

    # 问的是「过去」→ 交给自由问答（它会去检索服务器上的日记）。
    # 不能往下走模板：模板答的是今天，而「上次」和「今天」是两个问题。
    if _hits(t, _HISTORY_WORDS):
        return UNKNOWN_ANSWER

    # ⚠️ 顺序是有意的：**先判答不了的**。
    #    「我今天在电脑上写的日记」里既有「电脑」也有「日记」；日记那条更具体，
    #    而且它给的答案（vault 没推上来）比电脑活动快照更贴题。
    #    顺序反了就会给一个看着像答对了的答案。
    if _hits(t, _JOURNAL_WORDS):
        return JOURNAL_ANSWER
    if _hits(t, _HEALTH_WORDS):
        # ⚠️ 手表数据现在**有**了（笔记本推的日汇总）。以前这里直接返回
        #    「还没接」，那句已经过期了 —— 一条过期的假话比一句「拉不到」糟。
        try:
            return render_watch(day_iso, base, key)
        except LiveUnavailable:
            return HEALTH_ANSWER
    if _hits(t, _LAPTOP_WORDS):
        return render_desktop(day_iso, base, key)

    wants_location = _hits(t, _LOCATION_WORDS)
    wants_phone = _hits(t, _PHONE_WORDS)

    if wants_location and wants_phone:
        # 两个都提到了：都答，别猜他想问哪个。
        #
        # ⚠️ 两边**各自**兜错。写成一整句串联的话，Dawarich 挂了会让手机那部分
        #    也一起拿不到 —— 而那是两份完全独立的数据、两条独立的链路。
        parts: list[str] = []
        for part in (lambda: render_location(day_iso),
                     lambda: render_phone(relay_events(day_iso, base, key),
                                          day_iso=day_iso)):
            try:
                parts.append(part())
            except LiveUnavailable as e:
                parts.append(f"（这部分没拿到：{e}）")
        return "\n\n".join(parts)

    if wants_location:
        return render_location(day_iso)

    if wants_phone:
        return render_phone(relay_events(day_iso, base, key), day_iso=day_iso)

    # 带问号但没命中任何一类 → 明说能答什么，而不是默默去出题。
    if t.endswith(("?", "？")):
        return UNKNOWN_ANSWER
    return None
