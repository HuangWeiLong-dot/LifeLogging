"""把每天的数据写进 vault 的各个分区。

设计原则：**不重复日记的内容**，而是各放各该放的东西。

  05-Journal/     AI 精炼过的叙事 + 复盘         ← 人读的
  03-Time-Log/    规则引擎的**原始**分类结果      ← 用来核对和调规则，AI 没碰过
  01-Location/    停留点 / 行程 / 活动范围
  02-Phone/       解锁次数 / 应用切换
  04-Tasks/       滚动任务清单（单文件，不按天）
  07-Reviews/     周报（Dataview 自动聚合，不再调一次 LLM）

⚠️ 只有**有数据时**才写对应文件。空目录比没有目录更让人困惑，
   但每天生成 4 个空文件也是噪音。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config  # noqa: E402

CFG = load_config()
VAULT = Path(CFG["vault_path"])
TZ = ZoneInfo(CFG["timezone"])


def _fm(fields: dict[str, Any]) -> str:
    """生成 frontmatter。列表用 YAML 块格式，方便 Dataview 读。"""
    lines = ["---"]
    for k, v in fields.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {str(v).lower()}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f'{k}: "{v}"')
    lines.append("---")
    return "\n".join(lines)


def write_location(day_iso: str, tl: dict) -> Path | None:
    """01-Location —— 停留点、行程、活动范围。

    ⚠️ visits 和 track 是两种东西，缺一不可：
         visits  由 Dawarich 的停留检测**推导**出来，需要在一个地方待够时间
         track   原始点的日汇总，只要手机在采就有
       只判 visits 的话，出门但没形成停留的一天会写成空文件 —— 而你确实出去过。
    """
    loc = tl.get("location") or []
    track = tl.get("track") or {}
    if not loc and not track:
        return None

    visits = [x for x in loc if x.get("minutes", 0) > 0]
    p = VAULT / "01-Location" / f"{day_iso}.md"
    p.parent.mkdir(parents=True, exist_ok=True)

    body = [
        _fm({
            "date": day_iso,
            "tags": ["location"],
            "stops": len(visits),
            "points": track.get("points", 0),
            "span_km": track.get("span_km") if track.get("span_km") is not None else 0,
            "main_place": track.get("main_place", "") or (visits[0]["place"] if visits else ""),
        }),
        "",
        f"# Location · {day_iso}",
        "",
        "## Stays",
        "",
    ]
    if visits:
        body += ["| Place | Minutes |", "|---|---|"]
        for v in visits[:20]:
            body.append(f"| {v['place'] or '(unnamed)'} | {v['minutes']:.0f} |")
    else:
        body.append("_No stays detected. Dawarich needs several points in one place to form a visit._")
    body.append("")

    body += ["## Raw tracking", ""]
    if track:
        body.append(f"- Points: **{track.get('points', 0)}**")
        if track.get("span_km") is not None:
            body.append(f"- Span: **{track['span_km']} km**")
        if track.get("lat_center") is not None:
            body.append(f"- Centre: `{track['lat_center']}, {track['lon_center']}`")
            body.append(f"  · [open in OpenStreetMap]"
                        f"(https://www.openstreetmap.org/?mlat={track['lat_center']}&mlon={track['lon_center']}#map=15)")
        if track.get("first_utc") and track.get("last_utc"):
            body.append(f"- First point: {track['first_utc']}")
            body.append(f"- Last point: {track['last_utc']}")
        if not track.get("main_place"):
            body.append("")
            body.append("> No place name — reverse geocoding is not filling `city` yet, "
                        "so this can only report coordinates.")
    else:
        body.append("_No raw points recorded._")
    body += [""]

    p.write_text("\n".join(body), encoding="utf-8")
    return p


def write_phone(day_iso: str, tl: dict) -> Path | None:
    """02-Phone —— 解锁次数、应用切换、用得最多的应用。

    数据来自 Tasker/MacroDroid 往 `/ingest/phone` 上报的两类事件；
    手机上没配好的话这里是空的（返回 None，不生成文件）。

    ⚠️ 「前台时长」是**推导**的，不是上报的：两条相邻 App 切换之间的时间差
       就是前一个应用在前台待了多久（见 `normalize.summarize_phone`）。
       第一个和最后一个应用没有下一跳，测不出时长，所以这个数字是**下界**，
       不能当成「手机总使用时长」。页面上也这么标。
    """
    phone = tl.get("phone") or {}
    if not phone:
        return None

    p = VAULT / "02-Phone" / f"{day_iso}.md"
    p.parent.mkdir(parents=True, exist_ok=True)

    fm = {
        "date": day_iso,
        "tags": ["phone"],
        "unlocks": phone.get("unlocks", 0),
        "app_switches": phone.get("app_switches", 0),
    }
    if phone.get("screen_minutes"):
        fm["screen_minutes"] = phone["screen_minutes"]
    if phone.get("first_unlock"):
        fm["first_unlock"] = phone["first_unlock"]
        fm["last_unlock"] = phone["last_unlock"]
    if phone.get("foreground_minutes"):
        fm["foreground_minutes_lower_bound"] = phone["foreground_minutes"]

    body = [
        _fm(fm),
        "",
        f"# Phone · {day_iso}",
        "",
        f"- Unlocks: **{phone.get('unlocks', 0)}**"
        + (f" (first {phone['first_unlock']}, last {phone['last_unlock']})"
           if phone.get("first_unlock") else ""),
        f"- App switches: **{phone.get('app_switches', 0)}**",
    ]
    if phone.get("screen_minutes"):
        body.append(
            f"- Screen-on time: **{phone['screen_minutes']:.0f} min**"
            " — measured directly from screen on/off events."
        )
    if phone.get("foreground_minutes"):
        body.append(
            f"- Foreground time (summed over apps): **{phone['foreground_minutes']:.0f} min**"
            " — a **lower bound**: missed app switches and unpaired screen events are"
            " not counted, so it sits below the screen-on time."
        )
    body.append("")

    top = phone.get("top_apps") or []
    if top:
        body += ["## Most used apps", "", "| App | Foreground |", "|---|---|"]
        for name, mins in top:
            body.append(f"| {name} | {mins:.0f} min |")
        body.append("")
    else:
        body += ["> No app-switch events. Check that the Tasker `App Changed` profile is"
                 " enabled and that Usage Access is granted.", ""]

    p.write_text("\n".join(body), encoding="utf-8")
    return p


def write_time_log(day_iso: str, tl: dict) -> Path | None:
    """03-Time-Log —— **规则引擎的原始输出**，AI 没碰过。

    和 05-Journal 的区别：
      日记里是 AI 复核、合并、重写过的版本（人读友好）
      这里是机器按规则切的原始块（用来核对 AI 有没有改坏、
      以及看哪些规则块被标了 ambiguous 需要补规则）

    两边对不上时，就是需要调规则或调提示词的信号。
    """
    blocks = tl.get("blocks") or []
    if not blocks:
        return None

    p = VAULT / "03-Time-Log" / f"{day_iso}.md"
    p.parent.mkdir(parents=True, exist_ok=True)

    total = sum(b.get("hours", 0) for b in blocks)
    by_cat: dict[str, float] = {}
    for b in blocks:
        by_cat[b["category"]] = by_cat.get(b["category"], 0) + b.get("hours", 0)

    body = [
        _fm({
            "date": day_iso,
            "tags": ["time-log"],
            "blocks": len(blocks),
            "total_hours": round(total, 2),
        }),
        "",
        f"# Time log (raw rule engine) · {day_iso}",
        "",
        f"{len(blocks)} blocks · {total:.1f} h",
        "",
        "## By category",
        "",
        "| Category | Hours |",
        "|---|---|",
    ]
    for c, h in sorted(by_cat.items(), key=lambda x: -x[1]):
        body.append(f"| {c} | {h:.2f} |")
    body += [
        "",
        "## Blocks",
        "",
        "| Time | Hours | Category | Confidence | App | Titles |",
        "|---|---|---|---|---|---|",
    ]
    for b in blocks:
        titles = " / ".join(b.get("titles", [])[:2])[:60].replace("|", "/")
        body.append(
            f"| {b.get('time','')} | {b.get('hours',0):.2f} | {b.get('category','')} "
            f"| {b.get('confidence','')} | {b.get('app','')[:24]} | {titles} |"
        )
    body += [
        "",
        "> `confidence: ambiguous` = 规则判不准，交给了 AI。"
        "这一类如果反复出现，说明该往 `APP_RULES` / `TITLE_HINTS` 里加规则。",
        "",
    ]
    p.write_text("\n".join(body), encoding="utf-8")
    return p


def write_tasks(day_iso: str, carryover: list[str]) -> Path:
    """04-Tasks —— 滚动任务清单，单文件不按天。

    先做最简单的形态：一个 Open / Done 两段的手写清单，
    外加一段 Dataview 把各天日记里未勾选的项汇总进来。
    """
    p = VAULT / "04-Tasks" / "Tasks.md"
    p.parent.mkdir(parents=True, exist_ok=True)

    existing_open: list[str] = []
    if p.exists():
        in_open = False
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("## Open"):
                in_open = True
                continue
            if in_open and line.strip().startswith("## "):
                break
            if in_open and line.strip().startswith("- [ ]"):
                existing_open.append(line.rstrip())

    # 昨天没做完的，补进 Open（去重）
    merged = list(existing_open)
    for t in carryover:
        entry = f"- [ ] {t}"
        if entry not in merged and not any(t in m for m in merged):
            merged.append(entry)

    body = [
        _fm({"tags": ["tasks"], "updated": day_iso}),
        "",
        "# Tasks",
        "",
        "## Open",
        "",
        *(merged or ["- [ ] (nothing pending)"]),
        "",
        "## Done",
        "",
        "<!-- 完成的直接移到这里，或在下面手动勾掉 -->",
        "",
        "## Rolled up from journals",
        "",
        "```dataview",
        "TASK",
        'FROM "05-Journal"',
        "WHERE !completed",
        "SORT file.day DESC",
        "LIMIT 30",
        "```",
        "",
    ]
    p.write_text("\n".join(body), encoding="utf-8")
    return p


def write_weekly_review(day_iso: str) -> Path | None:
    """07-Reviews —— 周报。

    刻意**不再调一次 LLM**：那要多花钱，而且日记里已经有每天的复盘了。
    这里用 Dataview 把一周的数据聚合出来，你肉眼看即可。
    真想要 AI 写的周报，再单独加一个 stage。
    """
    d = datetime.fromisoformat(day_iso).date()
    if d.weekday() != 6:      # 只在周日生成
        return None

    iso = d.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    start = d - timedelta(days=6)

    p = VAULT / "07-Reviews" / f"{week}.md"
    p.parent.mkdir(parents=True, exist_ok=True)

    body = [
        _fm({"week": week, "start": start.isoformat(), "end": day_iso, "tags": ["review", "weekly"]}),
        "",
        f"# Week {week}",
        "",
        f"{start.isoformat()} → {day_iso}",
        "",
        "## Hours by category",
        "",
        "```dataview",
        "TABLE WITHOUT ID",
        '  L.category AS "Category",',
        '  round(sum(L.duration.hours), 1) AS "Hours"',
        'FROM "05-Journal"',
        f'WHERE file.day >= date("{start.isoformat()}") AND file.day <= date("{day_iso}")',
        "FLATTEN file.lists AS L",
        "WHERE L.category",
        "GROUP BY L.category",
        "SORT sum(L.duration.hours) DESC",
        "```",
        "",
        "## Daily totals",
        "",
        "```dataview",
        "TABLE WITHOUT ID",
        '  file.name AS "Date",',
        '  round(sum(L.duration.hours), 1) AS "Hours",',
        '  round(ai_cost, 4) AS "AI cost"',
        'FROM "05-Journal"',
        f'WHERE file.day >= date("{start.isoformat()}") AND file.day <= date("{day_iso}")',
        "FLATTEN file.lists AS L",
        "WHERE L.category",
        "GROUP BY file.name",
        "SORT file.name ASC",
        "```",
        "",
        "## Time sinks",
        "",
        "```dataview",
        "TABLE WITHOUT ID",
        '  file.name AS "Date", L.time AS "Time", L.duration AS "Duration", L.text AS "What"',
        'FROM "05-Journal"',
        f'WHERE file.day >= date("{start.isoformat()}") AND file.day <= date("{day_iso}")',
        "FLATTEN file.lists AS L",
        "WHERE L.blackhole = true",
        "SORT file.day DESC",
        "```",
        "",
        "## Write your own review here",
        "",
        "<!-- 数据在上面，下面归你。周报的价值在于「跨天看」，不是重复每天的结论 -->",
        "",
    ]
    p.write_text("\n".join(body), encoding="utf-8")
    return p


def write_all(day_iso: str, timeline: dict, carryover: list[str]) -> list[str]:
    """写全部副笔记，返回实际写出的文件列表。失败不抛异常。"""
    written: list[str] = []
    for fn, args in [
        (write_location, (day_iso, timeline)),
        (write_phone, (day_iso, timeline)),
        (write_time_log, (day_iso, timeline)),
    ]:
        try:
            r = fn(*args)
            if r:
                written.append(str(r.relative_to(VAULT)))
        except Exception as e:  # noqa: BLE001
            print(f"  WARN {fn.__name__} failed: {e}")

    try:
        r = write_tasks(day_iso, carryover)
        written.append(str(r.relative_to(VAULT)))
    except Exception as e:  # noqa: BLE001
        print(f"  WARN write_tasks failed: {e}")

    try:
        r = write_weekly_review(day_iso)
        if r:
            written.append(str(r.relative_to(VAULT)))
    except Exception as e:  # noqa: BLE001
        print(f"  WARN write_weekly_review failed: {e}")

    return written
