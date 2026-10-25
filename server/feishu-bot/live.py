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
                "config/config.yaml on the server cannot be read (probably missing, "
                f"or sources.dawarich is not filled in). Data queries are unavailable. (exit {e.code})"
            ) from None
        except Exception as e:  # noqa: BLE001
            raise LiveUnavailable(f"Failed to load data modules: {type(e).__name__}: {e}") from None

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
        raise LiveUnavailable(f"Cannot reach location data (Dawarich): {type(e).__name__}: {e}") from None

    if not pts:
        return []

    try:
        stays = m["derive_stays"](day, pts)
    except Exception as e:  # noqa: BLE001
        raise LiveUnavailable(f"Stay detection failed: {type(e).__name__}: {e}") from None

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
    """分钟 → 「2h 14m」。"""
    total = int(round(minutes))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def render_location(day_iso: str) -> str:
    stays = location_stays(day_iso)
    if not stays:
        return f"Location · {day_iso}\nNo location data."

    total = sum(s["minutes"] for s in stays)
    lines = [f"Location · {day_iso}", ""]
    for s in stays:
        span = f"{s['start']}–{s['end']}" if s["start"] and s["end"] else "—"
        place = s["place"] + (" (coords)" if s["is_coord"] else "")
        lines.append(f"  {span}  {place}  ·  {_hm(s['minutes'])}")
    lines += ["", f"Total {_hm(total)}."]
    return "\n".join(lines)


def render_phone(events: list[dict], day_iso: str | None = None) -> str:
    try:
        s = deps()["summarize_phone"](events)
    except LiveUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise LiveUnavailable(f"Phone summary failed: {type(e).__name__}: {e}") from None

    if not s["unlocks"] and not s["app_switches"]:
        return "No phone events today."

    # ⚠️ 「有屏幕数据」和「屏幕时间恰好是 0」是两回事：没有屏幕事件时
    #    只写「—」，不能写「0 分钟」。
    screen_evs = [e for e in events
                  if e.get("metric") in ("screen_on", "screen_off")]

    lines = [f"Phone · {day_iso}" if day_iso else "Phone", ""]
    if screen_evs:
        lines.append(f"  screen on    {_hm(s['screen_minutes'])}")
        lines.append(f"  foreground   {_hm(s['foreground_minutes'])} (lower bound)")
    else:
        lines.append("  screen time  —")
    lines.append(f"  unlocks      {s['unlocks']}")
    if s["first_unlock"]:
        lines.append(f"  first/last   {s['first_unlock']} / {s['last_unlock']}")
    lines.append(f"  switches     {s['app_switches']}")

    top = s["top_apps"][:6]
    if top:
        lines += ["", "  top apps:"]
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
            line = f"Phone: {s['unlocks']} unlocks · {s['app_switches']} switches"
            if s.get("screen_minutes"):
                line += f" · screen {_hm(s['screen_minutes'])}"
            out.append(line)
        except Exception:  # noqa: BLE001
            pass

    # 电脑快照只有「最新一份」，没有历史；查过去某天时不要拿今天那份冒充。
    #
    # ⚠️ 光判「查的是不是今天」**不够**。快照自己带 `day` 和 `pushed_at`，
    #    而笔记本睡着时中继上那份不会消失，它只是停住了。实测出过一次：
    #    10-23 的复盘里写「今天电脑合计 3.52 小时」，而那份快照是 10-22 的 ——
    #    同一份复盘的【总结】里模型自己都说「快照还停在 10-22」。
    #    代码没发现，模型发现了。所以这里必须两个都对上：日子对得上，而且够新。
    if day_iso == datetime.now(ZoneInfo(TZ_NAME)).date().isoformat():
        try:
            snap = relay_desktop(base, key) or {}
            snap_day = str(snap.get("day") or "")
            age = _snap_age_minutes(snap)
            if (snap_day and snap_day != day_iso) or (
                    age is not None and age > DAILY_SNAP_MAX_AGE_MIN):
                print(f"[recap] 电脑快照过期，不用它：day={snap_day!r} "
                      f"age={age if age is None else round(age)}min",
                      file=sys.stderr)
            else:
                today = snap.get("today") or {}
                total = today.get("total_hours")
                if total:
                    cats = " / ".join(f"{k} {v}h" for k, v in
                                      (today.get("by_category") or [])[:4])
                    out.append(f"Computer: {total}h ({cats})")
        except Exception:  # noqa: BLE001
            pass

    try:
        stays = location_stays(day_iso)
        if stays:
            best = max(stays, key=lambda s: s["minutes"])
            out.append(f"Location: {len(stays)} places, longest {best['place']} "
                       f"({_hm(best['minutes'])})")
    except Exception:  # noqa: BLE001
        pass

    try:
        snap = relay_watch(base, key) or {}
        health = snap.get("health") or {}
        steps = health.get("steps")
        if steps is not None:
            out.append(f"Watch: {int(round(float(steps)))} steps "
                       f"({snap.get('day') or '—'})")
    except Exception:  # noqa: BLE001
        pass

    return out


_QA_BUCKETS = ("Live answers", "Morning questions", "Wrap-up answers", "Other answers")


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
        return "Wrap-up answers"
    if x.get("source") == "laptop":
        return "Morning questions"
    if x.get("source") == "live":
        return "Live answers"
    return "Other answers"


# 日报快照最多能旧多久。比小时题宽松（一天的总量两三个小时里变化不大），
# 但**跨天绝对不行** —— 那正是「今天 3.52 小时」变成谎话的地方。
DAILY_SNAP_MAX_AGE_MIN = 180.0

# 截断时按这个**优先级**找断点：先找句末，再找逗号，最后才找空格。
# 每一组内部取最靠后的那一个。
_CUT_GROUPS = (
    ("\n", "。", "！", "？", ". ", "! ", "? ", "；", "; "),
    ("，", ", "),
    (" ",),
)


def _one_line(s: Any, limit: int) -> str:
    """压成一行，并且**在句子边界上**截断。

    ⚠️ 原来是 `[:limit]` 硬切。实测切出来的是
       「Something happened today that drained me completely, but also made me
        see myself more clearly than ever. In the morning,」
    —— 停在一个逗号后面，读起来像是被谁掐断了。宁可少显示半句，
    也要让它停在一个像结尾的地方。
    """
    text = " ".join(str(s or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    for seps in _CUT_GROUPS:
        best = -1
        for sep in seps:
            i = head.rfind(sep)
            if i > best:
                best = i + len(sep)
        if best >= limit * 0.55:
            return head[:best].rstrip() + "…"
    return head.rstrip() + "…"


def _snap_age_minutes(snap: dict | None) -> float | None:
    """快照是多久以前推的（分钟）。没有/解析不出来返回 None。"""
    dt = _parse_dt((snap or {}).get("pushed_at"))
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def _qa_line(x: dict, q_limit: int = 56, a_limit: int = 120,
             dup_of: str = "") -> str:
    """一行问答。`dup_of` 非空表示这段答案今天已经出现过一次 —— 只指向它，
    不把同一段长文贴第二遍。"""
    q = _one_line(x.get("text") or "(question not found)", q_limit)
    if dup_of:
        return f"{q} → same as “{_one_line(dup_of, 26)}”"
    a = _one_line(x.get("answer") or "", a_limit)
    n = _one_line(x.get("note") or "", a_limit)
    return f"{q} → {a}" + (f" ({n})" if n else "")


def _answer_key(x: dict) -> str:
    """判重用的规范化答案。太短的（「一致」「4」）不参与判重 ——
    它们本来就会重复，而且重复本身不带信息。"""
    raw = " ".join(str(x.get("answer") or "").split()).lower()
    return raw if len(raw) >= 40 else ""


def _dedupe_qa(qa: list[dict]) -> list[tuple[dict, str]]:
    """把「同一段话答了两道题」标出来，返回 [(qa, 第一道题的题干)]。

    ⚠️ 实测发生过：一次收尾里，「今天最值得记住的一件事」和「今天看的东西里
       哪一条最值得留下来」收到了**完全相同**的几百字。系统照单全收，
       然后复盘里那段长文出现了两遍 —— 既占屏幕，也让「今天到底记住了什么」
       变得不清楚。
    """
    seen: dict[str, str] = {}
    out: list[tuple[dict, str]] = []
    for x in qa:
        key = _answer_key(x)
        first = seen.get(key) if key else None
        if key and first is None:
            seen[key] = str(x.get("text") or "(the earlier question)")
        out.append((x, first or ""))
    return out


def render_qa(day_iso: str, base: str, key: str) -> str:
    """某一天的全部问答，一条一行。只给事实，不解释。"""
    qa = _norm_qa(relay_qa(day_iso, base, key))
    lines = [f"Q&A · {day_iso}"]
    if not qa:
        lines.append("No answers.")
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
    lines = [f"Daily recap · {day_iso}", ""]

    snap = data_snapshot(day_iso, base, key)
    if snap:
        lines.append("[Data]")
        lines += [f"· {x}" for x in snap]
        lines.append("")

    q_all = _norm_qa(qa)
    if q_all:
        lines.append("[Your answers]")
        marked = _dedupe_qa(q_all)
        for label in _QA_BUCKETS:
            group = [(x, dup) for x, dup in marked if _qa_bucket(x) == label]
            if not group:
                continue
            lines.append(f"· {label}")
            for x, dup in group:
                lines.append(f"  {_qa_line(x, dup_of=dup)}")
        lines.append("")

    sec = sections or {}
    summary = str(sec.get("summary") or "").strip()
    did = [str(x).strip() for x in (sec.get("did") or []) if str(x).strip()]
    advice = [str(x).strip() for x in (sec.get("advice") or []) if str(x).strip()]

    if summary:
        lines += ["[Summary]", summary, ""]

    if did:
        lines.append("[What you did]")
        lines += [f"· {x}" for x in did]
        lines.append("")

    if advice:
        # 编号而不是圆点 —— 这是**待办**，明天要照着做的。
        lines.append("[Suggestions for tomorrow]")
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
        return "just now", mins
    if mins < 60:
        return f"{int(mins)} min ago", mins
    h, m = divmod(int(mins), 60)
    if h < 24:
        return (f"{h}h {m}m ago" if m else f"{h}h ago"), mins
    return f"{h // 24}d ago", mins


DESKTOP_NO_DATA = "No desktop snapshot yet. Run scripts/push_desktop.py on the laptop."


def render_desktop(day_iso: str, base: str, key: str) -> str:
    """电脑上今天到目前。日期与「多久之前」放在标题行，正文不再解释。"""
    try:
        snap = relay_desktop(base, key)
    except Exception as e:  # noqa: BLE001
        raise LiveUnavailable(f"Cannot read the desktop snapshot: {type(e).__name__}: {e}") from None
    if not snap:
        return DESKTOP_NO_DATA

    # 用快照自己的时区算「多久之前」—— 不依赖 deps()。
    pushed = _parse_dt(snap.get("pushed_at"))
    now = datetime.now(pushed.tzinfo) if pushed else None
    ev_txt, _ = _ago(snap.get("last_event"), now) if now else ("", None)
    push_txt, _ = _ago(snap.get("pushed_at"), now) if now else ("", None)

    day = str(snap.get("day") or "")
    head = f"Computer · {day or day_iso}"
    fresh = ev_txt or push_txt
    if fresh:
        head += f" · {fresh}"
    lines: list[str] = [head, ""]

    cur = snap.get("current")
    if isinstance(cur, dict) and cur.get("app"):
        lines.append(f"Now: {cur.get('app')}")
        title = str(cur.get("title") or "").strip()
        if title:
            lines.append(f"  {title[:70]}")
        lines.append(f"  {cur.get('category')} · for "
                     f"{int(cur.get('minutes') or 0)} min")
    elif snap.get("afk") is True:
        lines.append("Now: AFK")
    else:
        lines.append("Now: no window activity")

    today = snap.get("today") or {}
    label = "Today total" if (not day or day == day_iso) else "That day total"
    lines += ["", f"{label} {today.get('total_hours')}h"]
    cats = today.get("by_category") or []
    if cats:
        lines.append("  " + " · ".join(f"{k} {v}h" for k, v in cats))
    apps = today.get("top_apps") or []
    if apps:
        lines.append("  top apps: " + " · ".join(f"{a} {v}h" for a, v in apps[:5]))

    return "\n".join(lines)


WATCH_NO_DATA = "No watch summary yet. Run scripts/push_health.py on the laptop."


def render_watch(day_iso: str, base: str, key: str) -> str:
    """手表日汇总：步数 / 心率 / 睡眠 / 最近运动。

    ⚠️ 「这是哪一天的」写在标题行。Gadgetbridge 通常隔天才导出，
       所以最常见的正确回答是「这是昨天的」而不是「今天没走」。
    """
    try:
        snap = relay_watch(base, key)
    except Exception as e:  # noqa: BLE001
        raise LiveUnavailable(f"Cannot read the health summary: {type(e).__name__}: {e}") from None
    if not snap:
        return WATCH_NO_DATA

    pushed = _parse_dt(snap.get("pushed_at"))
    now = datetime.now(pushed.tzinfo) if pushed else None
    push_txt, _ = _ago(snap.get("pushed_at"), now) if now else ("", None)

    day = str(snap.get("day") or "")
    head = f"Watch · {day or day_iso}"
    if push_txt:
        head += f" · {push_txt}"
    health = snap.get("health") if isinstance(snap.get("health"), dict) else {}
    lines = [head, ""]
    shown = False

    steps = health.get("steps")
    if steps is not None:
        lines.append(f"  steps        {int(round(float(steps)))}")
        shown = True
    hr_avg = health.get("heart_rate_avg")
    if hr_avg is not None:
        lines.append(f"  heart rate   avg {round(float(hr_avg))} bpm")
        shown = True
    hr_min = health.get("heart_rate_min")
    if hr_min is not None:
        lines.append(f"               min {round(float(hr_min))} bpm")
    sleep = health.get("sleep_hours")
    if sleep is not None:
        lines.append(f"  sleep        {_hm(float(sleep) * 60)}")
        shown = True

    if not shown:
        lines.append("  no steps / heart rate recorded")

    if sleep is None:
        # 不能说「睡眠 0 小时」：这台表的导出里没有睡眠样本。
        lines.append("  sleep        —")

    workouts = [w for w in (snap.get("workouts") or []) if isinstance(w, dict)]
    if workouts:
        lines += ["", "  recent workouts:"]
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
        bits.append(f"avg {int(hr)} bpm")
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
    "The journal and vault live in the Obsidian library on the laptop, "
    "and are not pushed to the server."
)

HEALTH_ANSWER = "Cannot fetch the watch summary (the relay returned an error)."

UNKNOWN_ANSWER = "Did not recognise that question. See /help for the commands."


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
            raise LiveUnavailable(f"Failed to load common: {type(e).__name__}: {e}") from None
        try:
            _mods["_cfg"] = load_config()
        except SystemExit:            # load_config 找不到文件时是 sys.exit(1)
            raise LiveUnavailable(
                "Cannot read config/config.yaml on the server — "
                "free-form questions are unavailable for now."
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
_LLM_SYSTEM = """You are answering the user's questions about **their own** day
(today's data, and their past journal entries).

Rules (all mandatory):
1. Use only facts present in [Today's raw data] and [Relevant past records].
   If neither has it, say "not in the records" — **do not speculate, do not fill
   in common sense, do not invent places, times or causes**.
2. When citing [Relevant past records], say which day it was (e.g. "on Oct 18").
   If nothing relevant was found, say so; never pass today's data off as the past.
3. The data may be incomplete or stale (especially the computer and watch parts).
   If it says "last report" or "which day this is", state that.
4. Answer in English, in two or three sentences. Do not restate the whole dataset.
5. If the question is about something this system does not collect at all, say
   that data is not wired in."""



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
            parts.append(f"(that part is unavailable: {type(e).__name__}: {e})")
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
        return None, (f"Today's AI quota is used up ({LLM_DAILY_CAP} calls).\n"
                      "The template answers (location / phone / computer) are "
                      "not affected — those still work.")

    ledger = CostLedger(conf["budget"]["ledger_path"])
    llm = LLM(cfg=conf, ledger=ledger)
    try:
        out = llm.complete(system=system, user=user, stage=stage,
                           run_id=new_run_id())
    except BudgetExceeded as e:
        return None, f"Today's AI budget is used up. ({e})"
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
        raise LiveUnavailable(f"Free-form questions unavailable: {type(e).__name__}: {e}") from None

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
            history = "\n\n[Relevant past records, most relevant first]\n" + "\n".join(
                f"- [{h['day']}] {h['excerpt']}" for h in hits)
    except Exception as e:  # noqa: BLE001
        print(f"WARN 历史检索不可用：{type(e).__name__}: {e}", file=sys.stderr)

    out, err = _run_llm_turn(
        conf, counter, day_iso,
        system=_LLM_SYSTEM,
        user=(f"[Today's raw data]\n{digest}{history}\n\n"
              f"[The user's question]\n{text}"),
        stage="feishu_ask")
    if out is None:
        return f"Cannot answer: {err}"
    return out


_SUMMARY_SYSTEM = """You are writing the user's **end-of-day recap**.

Output **one JSON object**, nothing outside it:

{
  "summary": "3-5 sentences reviewing the day",
  "did": ["something concrete they did", "..."],
  "advice": ["one thing they can do tomorrow", "..."]
}

Rules (all mandatory):

1. Use only the data below and what the user wrote themselves. Do not invent,
   do not speculate.
2. `summary` is 3-5 sentences. No bullets, no headings, no re-listing the table.
   Plain tone, like a friend looking back on the day — no pep talk, no judging.
3. `did` is a **list of facts**, 3-6 items, one concrete thing each: what they
   did, where they went, what moved forward. Not "work / life" categories —
   write "rewrote the bot's wrap-up questions into seven dimensions".
4. `advice` is **concrete actions for tomorrow**, 1-3 items. No "rest more" or
   "stay focused" — it has to be directly executable: "verify the AE install
   before 15:00 tomorrow", "finish the remaining course videos, don't push it
   to the weekend". Prefer the item from their answer to "what did not get done".
5. What the user wrote themselves **outranks any inference**. If they conflict,
   their words win.
6. If there is very little recorded today, say so plainly. Do not pad.
7. `[Live answers]` are facts recorded **at the time** (asked hourly);
   `[Wrap-up answers]` are the evening review. Use both: do not read only the
   wrap-up, and do not let the wrap-up's summary overwrite the more specific
   details from the day. On conflict, prefer the nearer, more specific one.

Three more, all about not stating uncertain things as certain:

8. **Data marked stale cannot be used as today's.** If the raw data says "last
   report X hours ago" or "data stops at <date>", that block belongs to that
   day, not today. You may mention "the computer side did not report today" in
   `summary`, but do not put it in `did`.
9. **A window title is not a behaviour.** "life-pipeline.drawio open in VS Code"
   only says what was on screen, not that they worked on it. Every item in
   `did` must be supported by their answers or by a clear activity duration;
   an item supported only by a window title should be dropped or phrased as
   "the window stayed on X".
10. **The same answer never appears twice.** If an answer reads "same as
    “...”", it means they answered two questions with one passage —
    treat it as **one event**, do not split it into two `did` items."""



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
        raise LiveUnavailable(f"Summary unavailable: {type(e).__name__}: {e}") from None

    digest = _digest(day_iso, base, key)
    q_all = _norm_qa(qa)
    if q_all:
        chunks: list[str] = []
        marked = _dedupe_qa(q_all)
        for label in _QA_BUCKETS:
            group = [(x, dup) for x, dup in marked if _qa_bucket(x) == label]
            if not group:
                continue
            chunks.append(f"[{label}]")
            for x, dup in group:
                qt = _one_line(x.get("text") or "(question not in the question file)", 200)
                if dup:
                    # ⚠️ 同一段话答了两道题时只给模型一遍，并明说是同一段。
                    #    贴两遍会让它以为今天有两件同样重要的事。
                    chunks.append(f"- Q: {qt}\n  A: (same answer, see “"
                                  f"{_one_line(dup, 40)}”)")
                    continue
                at = _one_line(x.get("answer") or "", 300)
                nt = _one_line(x.get("note") or "", 200)
                chunks.append(f"- Q: {qt}\n  A: {at}"
                              + (f"\n  Note: {nt}" if nt else ""))
        answers = "\n".join(chunks)
    else:
        answers = "(no answers recorded today)"

    counter = _llm_counter(Path(conf.get("state_dir") or "."), day_iso)
    out, err = _run_llm_turn(
        conf, counter, day_iso,
        system=_SUMMARY_SYSTEM,
        user=(f"[Today's raw data]\n{digest}\n\n"
              f"[What I asked you today, and what you answered]\n{answers}"),
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
                parts.append(f"(that part is unavailable: {e})")
        return "\n\n".join(parts)

    if wants_location:
        return render_location(day_iso)

    if wants_phone:
        return render_phone(relay_events(day_iso, base, key), day_iso=day_iso)

    # 带问号但没命中任何一类 → 明说能答什么，而不是默默去出题。
    if t.endswith(("?", "？")):
        return UNKNOWN_ANSWER
    return None
