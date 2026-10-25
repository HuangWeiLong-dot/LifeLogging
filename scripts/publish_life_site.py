"""把 LifeLogging 的日汇总**脱敏**后发布到个人站点（liveinpassion.me/life）。

数据流：
  state/events.db ──(本脚本)──> POST /api/life/ingest ──> Cloudflare D1
                                                    └─> GET /api/life

只发聚合数字：
  · 分类时长、追踪小时数、常用桌面应用（友好名）
  · 常用浏览站点（**只到域名**，按当天分钟数排序）
  · 步数 / 心率 / 跑步汇总 / VO2max
  · 城市 + 区级停留时长、移动分钟、跨度、轨迹点数（**不含坐标/轨迹**）
  · 手机解锁 / 亮灭屏 / 亮屏分钟 / 应用切换 / 常用应用（显示名）
  · AI 花费（按来源解析，**不含余额**、项目名与请求明细）

绝不发送：lat/lon、track_path、blocks.title、完整 URL、页面标题、窗口标题、
原始包名、日记正文、账号余额。

用法：
  python scripts/publish_life_site.py                 # 近 30 天
  python scripts/publish_life_site.py --days 90       # 回填
  python scripts/publish_life_site.py --date 2026-10-19
  python scripts/publish_life_site.py --dry-run --print-json
  python scripts/publish_life_site.py --api-base http://127.0.0.1:8787

退出码：0 成功（或未启用）；1 部分/全部发布失败；2 配置或鉴权缺失。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import header, load_config  # noqa: E402
from normalize import phone_category_intervals, summarize_phone  # noqa: E402
from translate import Translator, has_cjk  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAX_DAY_BYTES = 32 * 1024

# 桌面系统进程：不是「我在用这个应用」
SYSTEM_APPS = {
    "rundll32.exe", "applicationframehost.exe", "explorer.exe",
    "msiexec.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "shellexperiencehost.exe", "lockapp.exe",
    "dwm.exe", "csrss.exe", "svchost.exe", "systemsettings.exe",
    "securityhealthsystray.exe", "widgetservice.exe", "searchapp.exe",
    "backgroundtaskhost.exe",
}

DESKTOP_APP_NAMES = {
    "code.exe": "VS Code",
    "msedge.exe": "Edge",
    "chrome.exe": "Chrome",
    "weixin.exe": "WeChat",
    "windowsterminal.exe": "Terminal",
    "powershell.exe": "PowerShell",
    "photoshop.exe": "Photoshop",
    "obs64.exe": "OBS",
    "antigravity.exe": "Antigravity",
    "workbuddyai.exe": "WorkBuddy",
    "clash-verge.exe": "Clash",
    "spotify.exe": "Spotify",
    "discord.exe": "Discord",
    "steam.exe": "Steam",
    "notion.exe": "Notion",
    "obsidian.exe": "Obsidian",
}

# Dawarich 可能返回中文或英文地名；统一成站点使用的英文
DISTRICT_NAMES = {
    "黄浦": "Huangpu", "黄浦区": "Huangpu",
    "徐汇": "Xuhui", "徐汇区": "Xuhui",
    "长宁": "Changning", "长宁区": "Changning",
    "静安": "Jing'an", "静安区": "Jing'an",
    "普陀": "Putuo", "普陀区": "Putuo",
    "虹口": "Hongkou", "虹口区": "Hongkou",
    "杨浦": "Yangpu", "杨浦区": "Yangpu",
    "闵行": "Minhang", "闵行区": "Minhang",
    "宝山": "Baoshan", "宝山区": "Baoshan",
    "嘉定": "Jiading", "嘉定区": "Jiading",
    "浦东": "Pudong", "浦东新区": "Pudong",
    "金山": "Jinshan", "金山区": "Jinshan",
    "松江": "Songjiang", "松江区": "Songjiang",
    "青浦": "Qingpu", "青浦区": "Qingpu",
    "奉贤": "Fengxian", "奉贤区": "Fengxian",
    "崇明": "Chongming", "崇明区": "Chongming",
    "闸北": "Zhabei",
}
CITY_NAMES = {
    "上海": "Shanghai", "上海市": "Shanghai",
    "北京": "Beijing", "北京市": "Beijing",
    "杭州": "Hangzhou", "杭州市": "Hangzhou",
    "苏州": "Suzhou", "苏州市": "Suzhou",
    "南京": "Nanjing", "南京市": "Nanjing",
    "深圳": "Shenzhen", "深圳市": "Shenzhen",
    "广州": "Guangzhou", "广州市": "Guangzhou",
}

# 残留的 Android 包名（summarize_phone 对未知包名会原样保留）
PKG_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

# 手机应用怎么分到七维，统一在 normalize.PHONE_APP_CATEGORIES 里定义
# （那边同时是手机应用名表和「没有 Other，每一分钟都有归属」的口径）。
# 这里不再维护自己的 Social / AI 名单，避免两套表漂移。
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


# ── 基础工具 ──────────────────────────────────────────────────

def clean_name(value: Any, limit: int = 64) -> str | None:
    """去掉控制字符、限长；空串返回 None。"""
    if not isinstance(value, str):
        return None
    s = CTRL_RE.sub("", value).strip()
    if not s:
        return None
    return s[:limit]


def num(value: Any, digits: int = 2) -> float | None:
    """有限数字才返回；否则 None。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, digits)


def non_negative(value: Any, digits: int = 0) -> float | int | None:
    v = num(value, digits)
    if v is None or v < 0:
        return None
    return v


def meta_of(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["meta_json"] or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def open_db(state_dir: Path) -> sqlite3.Connection:
    path = state_dir / "events.db"
    if not path.exists():
        raise FileNotFoundError(f"events.db not found: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = 1")
    conn.row_factory = sqlite3.Row
    return conn


# ── 各区块聚合 ────────────────────────────────────────────────

def desktop_name(raw: str | None) -> str | None:
    if not raw:
        return None
    name = raw.strip()
    lower = name.lower()
    if not name or lower in SYSTEM_APPS:
        return None
    if lower in DESKTOP_APP_NAMES:
        return DESKTOP_APP_NAMES[lower]
    stem = re.sub(r"\.exe$", "", name, flags=re.I)
    return clean_name(stem, 40)


def build_desktop(blocks: list[sqlite3.Row], categories_cfg: list[str]) -> tuple[dict | None, float | None, list | None]:
    if not blocks:
        return None, None, None

    categories = {c: 0.0 for c in categories_cfg}
    total = 0.0
    apps: dict[str, float] = {}

    for row in blocks:
        hours = float(row["hours"] or 0)
        if not math.isfinite(hours) or hours <= 0:
            continue
        cat = clean_name(row["category"], 24) or "Other"
        categories[cat] = categories.get(cat, 0.0) + hours
        total += hours
        name = desktop_name(row["app"])
        if name:
            apps[name] = apps.get(name, 0.0) + hours

    # 不截到前 8：单日详情和「今日」板块要能展开完整列表。真实一天通常
    # 只有十几到几十个应用，单天 payload 远低于 32 KB 上限。
    top = sorted(apps.items(), key=lambda kv: -kv[1])[:60]
    return (
        {k: round(v, 2) for k, v in sorted(categories.items())},
        round(total, 2),
        [[k, round(v, 2)] for k, v in top],
    )


def build_sites(rows: list[sqlite3.Row]) -> list | None:
    """当天浏览器扩展记录的站点时长（分钟），只到域名一级。

    ActivityWatch 的 aw-watcher-web 按站点、按页面标题各记一套；这里只取
    site_usage。页面标题和完整 URL 仍然只在原始归档里 —— 聚合接口只发域名，
    和 topApps 一样是“我在哪花了时间”，不是“我看了什么”。
    """
    if not rows:
        return None

    minutes: dict[str, float] = {}
    for row in rows:
        if row["metric"] != "site_usage":
            continue
        sec = num(row["value_num"], 1) or 0.0
        if sec <= 0:
            continue
        host = (clean_name(row["value_text"], 64) or "").strip().lower().lstrip(".")
        if host.startswith("www."):
            host = host[4:]
        if not host:
            continue
        minutes[host] = minutes.get(host, 0.0) + sec / 60.0

    top = sorted(minutes.items(), key=lambda kv: -kv[1])[:60]
    return [[k, round(v, 1)] for k, v in top] or None


def pick_metric(rows: list[sqlite3.Row], metric: str) -> sqlite3.Row | None:
    """同一天同一指标取最后一条（ts_utc 升序读入）。"""
    found = None
    for row in rows:
        if row["metric"] == metric:
            found = row
    return found


def build_health(rows: list[sqlite3.Row]) -> dict | None:
    if not rows:
        return None

    def value(metric: str) -> float | None:
        row = pick_metric(rows, metric)
        return None if row is None else num(row["value_num"], 3)

    def meta_value(metric: str, key: str) -> float | None:
        row = pick_metric(rows, metric)
        return None if row is None else num(meta_of(row).get(key), 1)

    steps = value("steps")
    if steps is not None and steps < 0:
        steps = None

    # 旧数据没有独立的 heart_rate_max 事件；heart_rate_avg 的 meta 里已经有
    # 当天的最高心率，所以历史日期也能补出 hrMax，不需要重跑 ingest。
    hr_max = num(value("heart_rate_max"), 1)
    if hr_max is None:
        hr_max = meta_value("heart_rate_avg", "max")

    calls = {
        "steps": None if steps is None else int(round(steps)),
        "hrAvg": num(value("heart_rate_avg"), 1),
        "hrMin": num(value("heart_rate_min"), 1),
        "hrMax": hr_max,
        "runs": None if value("run_count") is None else int(round(value("run_count") or 0)),
        "runKm": num(value("run_km"), 2),
        "runMinutes": num(value("run_minutes"), 1),
        "runPaceS": None if value("run_pace_s_per_km") is None else int(round(value("run_pace_s_per_km") or 0)),
        "vo2max": num(value("vo2max"), 1),
    }
    if all(v is None for v in calls.values()):
        return None
    return calls


def split_place(text: str | None) -> tuple[str | None, str | None]:
    """'Yingpu, Qingpu, Shanghai, CN' → ('Shanghai', 'Qingpu')。"""
    if not text:
        return None, None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if parts and len(parts[-1]) == 2 and parts[-1].isalpha():
        parts.pop()  # 国别代码 CN
    if parts and parts[-1].lower() in ("china", "中国", "中华人民共和国"):
        parts.pop()
    if not parts:
        return None, None

    city_raw = parts[-1]
    district_raw = parts[-2] if len(parts) >= 2 else None
    city = CITY_NAMES.get(city_raw, city_raw)
    district = DISTRICT_NAMES.get(district_raw, district_raw) if district_raw else None
    return clean_name(city, 64), clean_name(district, 64)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _clip_intervals(
    intervals: list[tuple[datetime, datetime]],
    lo: datetime, hi: datetime,
) -> list[tuple[datetime, datetime]]:
    """把区间裁进 [lo, hi) —— 块可能跨过午夜。"""
    out = []
    for a, b in intervals:
        if a is None or b is None:
            continue
        start, end = max(a, lo), min(b, hi)
        if end > start:
            out.append((start, end))
    return out


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """排序后合并重叠区间 —— collapsedHours 的核心就是「并集」。"""
    out: list[tuple[datetime, datetime]] = []
    for a, b in sorted(intervals, key=lambda x: x[0]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _interval_minutes(intervals: list[tuple[datetime, datetime]]) -> float:
    return sum((b - a).total_seconds() for a, b in _merge_intervals(intervals)) / 60.0


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def build_location(
    rows: list[sqlite3.Row],
) -> tuple[dict | None, list[tuple[datetime, datetime]]]:
    """位置聚合 + 交通的真实区间（后者只给 collapsedHours 用，不发布）。"""
    if not rows:
        return None, []

    visits: list[tuple[float, str | None, str | None]] = []
    visit_details: list[dict[str, Any]] = []
    transport_details: list[dict[str, Any]] = []
    moving = 0.0
    points = None
    span_km = None

    for row in rows:
        metric = row["metric"]
        if metric == "visit":
            minutes = num(row["value_num"], 1) or 0.0
            if minutes > 0:
                city, district = split_place(row["value_text"])
                visits.append((minutes, city, district))
                meta = meta_of(row)
                start = _parse_ts(row["ts_utc"])
                end = _parse_ts(meta.get("end_utc"))
                if end is None and start is not None:
                    end = start + timedelta(minutes=minutes)
                visit_details.append({
                    "start": start,
                    "end": end,
                    "lat": num(meta.get("lat"), 6),
                    "lon": num(meta.get("lon"), 6),
                })
        elif metric == "transport":
            minutes = num(row["value_num"], 1) or 0.0
            if minutes > 0:
                moving += minutes
                meta = meta_of(row)
                start = _parse_ts(row["ts_utc"])
                end = _parse_ts(meta.get("end_utc"))
                if end is None and start is not None:
                    end = start + timedelta(minutes=minutes)
                transport_details.append({"start": start, "end": end})
        elif metric == "tracked_points":
            p = num(row["value_num"], 0)
            if p is not None:
                points = int(p)
            span = meta_of(row).get("span_km")
            span_km = num(span, 1)

    if not visits and moving == 0 and points is None:
        return None, []

    # 当天主城市：停留分钟最多。跨城出行时不把两个城市的区混在一起。
    city_minutes: dict[str, float] = {}
    for minutes, city, _ in visits:
        if city:
            city_minutes[city] = city_minutes.get(city, 0.0) + minutes
    main_city = max(city_minutes.items(), key=lambda kv: kv[1])[0] if city_minutes else None

    district_minutes: dict[str, float] = {}
    stay = 0.0
    for minutes, city, district in visits:
        stay += minutes
        if main_city and city == main_city and district:
            district_minutes[district] = district_minutes.get(district, 0.0) + minutes

    districts = [
        [name, round(mins / 60.0, 2)]
        for name, mins in sorted(district_minutes.items(), key=lambda kv: -kv[1])[:8]
    ]

    # 实测 transport 只覆盖走路/骑行/开车那几段；地铁/地下/无 GPS 的通勤
    # 不会产生 track，于是“HOURS MOVING”会明显偏低（青浦↔闸北约 40km，
    # 单程 100–120min，但只有最后一段步行/骑行进账）。
    # 这里用两次停留之间的空档补：间隔 4–180min、两点相距 ≥1km，
    # 扣掉落在空档里的实测 transport 段，剩下的就是没被识别到的通勤时间。
    inferred = 0.0
    inferred_intervals: list[tuple[datetime, datetime]] = []
    ordered = sorted(
        (v for v in visit_details if v["start"] is not None and v["end"] is not None),
        key=lambda v: v["start"],
    )
    for a, b in zip(ordered, ordered[1:]):
        if a["lat"] is None or a["lon"] is None or b["lat"] is None or b["lon"] is None:
            continue
        gap = (b["start"] - a["end"]).total_seconds() / 60.0
        if not (4.0 <= gap <= 180.0):
            continue
        if _haversine_km((a["lat"], a["lon"]), (b["lat"], b["lon"])) < 1.0:
            continue
        covered = 0.0
        for t in transport_details:
            if t["start"] is None or t["end"] is None:
                continue
            lo = max(a["end"], t["start"])
            hi = min(b["start"], t["end"])
            if hi > lo:
                covered += (hi - lo).total_seconds() / 60.0
        left = max(0.0, gap - covered)
        inferred += left
        if left > 0:
            inferred_intervals.append((a["end"], a["end"] + timedelta(minutes=left)))

    intervals = [
        (t["start"], t["end"]) for t in transport_details
        if t["start"] is not None and t["end"] is not None
    ] + inferred_intervals

    return {
        "city": main_city,
        "districts": districts,
        "movingMinutes": round(moving, 1),
        "inferredMinutes": round(inferred, 1),
        "transitMinutes": round(moving + inferred, 1),
        "stayMinutes": round(stay, 1),
        "spanKm": span_km,
        "points": points,
    }, intervals


def build_phone(rows: list[sqlite3.Row]) -> dict | None:
    if not rows:
        return None

    raw = [
        {"ts_utc": row["ts_utc"], "metric": row["metric"], "value_text": row["value_text"]}
        for row in rows
        if row["metric"] in ("unlock", "app_switch", "screen_on", "screen_off")
    ]
    if not raw:
        return None

    summary = summarize_phone(raw)
    apps = []
    for name, minutes in summary.get("top_apps", []):
        if PKG_RE.match(name or "") or (name or "").lower() == "unknown":
            continue
        apps.append([name, round(float(minutes), 1)])
        if len(apps) >= 40:
            break

    return {
        "unlocks": int(summary.get("unlocks") or 0),
        "screenOn": sum(1 for r in raw if r["metric"] == "screen_on"),
        "screenOff": sum(1 for r in raw if r["metric"] == "screen_off"),
        "screenMinutes": round(float(summary.get("screen_minutes") or 0.0), 1),
        "appSwitches": int(summary.get("app_switches") or 0),
        "topApps": apps,
    }


def build_ai(rows: list[sqlite3.Row]) -> dict | None:
    if not rows:
        return None

    daily = pick_metric(rows, "daily_cost")
    spend = pick_metric(rows, "spend")
    model_rows = [r for r in rows if r["metric"] == "model_cost"]
    project_rows = [r for r in rows if r["metric"] == "project_tokens"]

    daily_cost = num(daily["value_num"], 4) if daily is not None else None
    spend_cost = num(spend["value_num"], 4) if spend is not None else None

    projects = []
    estimated = None
    for row in project_rows:
        cost = num(meta_of(row).get("cost_estimate"), 4)
        if cost is None:
            continue
        name = clean_name(row["value_text"], 40)
        if name:
            projects.append([name, cost])
        estimated = (estimated or 0.0) + cost
    projects.sort(key=lambda p: -p[1])
    projects = projects[:30]

    models = []
    for row in model_rows:
        cost = num(row["value_num"], 4)
        name = clean_name(row["value_text"], 40)
        if cost is not None and name:
            models.append([name, cost])
    models.sort(key=lambda p: -p[1])
    models = models[:30]

    # 一个数字，来源必须说清楚：平台真实支出 > Claude Code 估算 > 本流水线台账
    if spend_cost is not None:
        cost, cost_source = spend_cost, "platform"
    elif estimated is not None:
        cost, cost_source = round(estimated, 4), "claude-estimate"
    elif daily_cost is not None:
        cost, cost_source = daily_cost, "ledger"
    else:
        cost, cost_source = None, None

    if spend is not None:
        requests = non_negative(meta_of(spend).get("requests"), 0)
        calls = None if requests is None else int(requests)
        calls_source = "platform" if calls is not None else None
    elif project_rows:
        total_calls = sum(
            (non_negative(meta_of(r).get("calls"), 0) or 0) for r in project_rows
        )
        calls = int(total_calls) if total_calls > 0 else None
        calls_source = "claude-estimate" if calls is not None else None
    elif daily is not None:
        meta_calls = non_negative(meta_of(daily).get("calls"), 0)
        calls = None if meta_calls is None else int(meta_calls)
        calls_source = "ledger" if calls is not None else None
    else:
        calls, calls_source = None, None

    if cost is None and calls is None and not models and not projects:
        return None

    return {
        "cost": cost,
        "costSource": cost_source,
        "dailyCost": daily_cost,
        "spend": spend_cost,
        "estimatedCost": None if estimated is None else round(estimated, 4),
        "calls": calls,
        "callsSource": calls_source,
        "models": models,
        "projects": projects,
    }


def build_dimensions(categories: dict | None, health: dict | None,
                     location: dict | None,
                     phone_dims: dict[str, float] | None) -> dict[str, float]:
    """把桌面分类 + 手表 + GPS + 手机合成神经系统用的七维（单位：小时）。

    - Work / Study / Life / Leisure：桌面分类 **+ 手机上归到同一维的应用**
    - Social：手机社交/通讯应用（桌面端的微信/QQ 走 Leisure 规则）
    - Transport：Dawarich 的实测 + 推断通勤分钟
    - Health：手表跑步分钟（当前导出没有睡眠/其他可计时活动）

    桌面的 ``Other`` 不进七维（它是“没认出来”，不是维度）；手机没有这一档，
    ``normalize.PHONE_APP_CATEGORIES`` 保证手机时间一定有归属。

    ``phone_dims`` 是手机各维的小时数，由 ``phone_category_intervals`` 按**区间**
    算出来（不是 topApps 的分钟和）—— 和 collapsedHours 的并集同一个来源，
    这样 OVERLAP 才不会把「同一个应用的两段时间」算成两份。

    ⚠️ 这是**各来源读数之和**，不是时间并集：手机在电脑前刷 B 站时，
       Leisure 会把桌面和手机各算一次。真实的去重总时长见 ``collapsedHours``。
    """
    cats = categories or {}
    phone = phone_dims or {}

    dims = {
        name: max(0.0, float(cats.get(name) or 0.0)) + max(0.0, float(phone.get(name) or 0.0))
        for name in ("Work", "Study", "Life", "Leisure")
    }
    transit_min = max(0.0, float((location or {}).get("transitMinutes") or 0.0))
    health_min = max(0.0, float((health or {}).get("runMinutes") or 0.0))

    return {
        "Work": round(dims["Work"], 2),
        "Study": round(dims["Study"], 2),
        "Transport": round(transit_min / 60.0, 2),
        "Leisure": round(dims["Leisure"], 2),
        "Health": round(health_min / 60.0, 2),
        "Life": round(dims["Life"], 2),
        "Social": round(max(0.0, float(phone.get("Social") or 0.0)), 2),
    }


def build_workouts(rows: list[sqlite3.Row]) -> list[dict[str, Any]] | None:
    """每次锻炼的明细（目前主要是跑步）。给 MOTION 的 RUNS 列表用。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["metric"] != "workout":
            continue
        start = str(row["ts_utc"] or "")
        if not start:
            continue
        meta = meta_of(row)

        def num_meta(key: str, digits: int, integer: bool = False):
            v = num(meta.get(key), digits)
            if v is None:
                return None
            return int(round(v)) if integer else round(v, digits)

        out.append({
            "start": start,
            "end": str(meta.get("end_utc") or ""),
            "name": clean_name(row["value_text"], 40) or "Workout",
            "kind": clean_name(meta.get("kind"), 40),
            "minutes": round(num(row["value_num"], 1) or 0.0, 1),
            "km": num(meta.get("distance_km"), 2),
            "avgHr": num_meta("avg_hr", 0, True),
            "maxHr": num_meta("max_hr", 0, True),
            "paceS": num_meta("pace_s_per_km", 0, True),
            "cadence": num_meta("avg_cadence", 0, True),
            "ascentM": num_meta("ascent_m", 0, True),
            "calories": num_meta("calories", 0, True),
            "vo2max": num_meta("vo2max", 1),
            "trainingEffect": num_meta("training_effect", 1),
            "recoveryHours": num_meta("recovery_hours", 1),
        })
    out.sort(key=lambda w: w["start"])
    return out or None


def build_all_time(cfg: dict, tz: ZoneInfo) -> dict | None:
    """手表数据库里的**全历史**统计（不是 30 天窗口）。

    Gadgetbridge 的 BASE_ACTIVITY_SUMMARY 保存了所有锻炼；这里直接读它，
    所以 Best Pace / Longest 可以来自很早以前，而不是从站点搭好之后才有。
    """
    gb = (cfg.get("sources") or {}).get("gadgetbridge") or {}
    db_path = gb.get("db_path")
    if not db_path:
        return None
    path = Path(str(db_path))
    if not path.is_file():
        return None

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT NAME, START_TIME, SUMMARY_DATA FROM "BASE_ACTIVITY_SUMMARY"'
        ).fetchall()
        conn.close()
    except (sqlite3.Error, OSError) as exc:
        print(f"  ⚠️  all-time watch stats unavailable: {exc}", file=sys.stderr)
        return None

    def val(summary: dict, key: str):
        v = summary.get(key)
        return v.get("value") if isinstance(v, dict) else v

    workouts = []
    for row in rows:
        start_ms = row["START_TIME"]
        if not isinstance(start_ms, (int, float)) or start_ms <= 0:
            continue
        try:
            summary = json.loads(row["SUMMARY_DATA"] or "{}")
        except (json.JSONDecodeError, TypeError):
            summary = {}
        try:
            km = float(val(summary, "distanceMeters") or 0) / 1000.0
        except (TypeError, ValueError):
            km = 0.0
        try:
            pace = float(val(summary, "averageKMPaceSeconds") or 0)
        except (TypeError, ValueError):
            pace = 0.0
        if km <= 0:
            continue
        start = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc).astimezone(tz)
        workouts.append({
            "km": km,
            "pace": pace,
            "start": start,
            "name": (row["NAME"] or "").strip() or "Workout",
            "day": start.date().isoformat(),
        })

    if not workouts:
        return None

    paced = [w for w in workouts if w["pace"] > 0 and w["km"] >= 0.1]
    if not paced:
        paced = [w for w in workouts if w["pace"] > 0]
    best = min(paced, key=lambda w: w["pace"]) if paced else None
    longest = max(workouts, key=lambda w: w["km"])

    def iso(w) -> str | None:
        return (w["start"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if w else None)

    return {
        "runs": len(workouts),
        "km": round(sum(w["km"] for w in workouts), 1),
        "bestPaceS": int(round(best["pace"])) if best else None,
        "bestPaceKm": round(best["km"], 2) if best else None,
        "bestPaceName": clean_name(best["name"], 40) if best else None,
        "bestPaceDay": best["day"] if best else None,
        "bestPaceStart": iso(best),
        "longestKm": round(longest["km"], 2),
        "longestName": clean_name(longest["name"], 40),
        "longestDay": longest["day"],
        "longestStart": iso(longest),
    }


def build_day(
    conn: sqlite3.Connection, day: str, categories_cfg: list[str], tz: ZoneInfo,
) -> dict | None:
    blocks = conn.execute(
        """SELECT category, app, SUM(hours) AS hours
           FROM blocks WHERE day_local = ?
           GROUP BY category, app""",
        (day,),
    ).fetchall()

    # 时间区间：collapsedHours 要求并集，聚合过的 SUM(hours) 不够用。
    block_rows = conn.execute(
        "SELECT start_utc, end_utc, hours FROM blocks WHERE day_local = ?",
        (day,),
    ).fetchall()

    events = conn.execute(
        """SELECT domain, metric, ts_utc, value_num, value_text, meta_json
           FROM events WHERE day_local = ?
           ORDER BY ts_utc""",
        (day,),
    ).fetchall()

    by_domain: dict[str, list[sqlite3.Row]] = {}
    for row in events:
        by_domain.setdefault(row["domain"], []).append(row)

    categories, tracked, top_apps = build_desktop(blocks, categories_cfg)
    health = build_health(by_domain.get("health", []))
    location, transport_intervals = build_location(by_domain.get("location", []))
    phone = build_phone(by_domain.get("phone", []))

    # 手机：**所有**非系统应用都按七维分类（没有 Other 这一档）。
    # 分类结果同时用于两处，必须同源，否则 OVERLAP 会算出不存在的重叠：
    #   · dimensions 里手机贡献的小时数
    #   · collapsedHours 的并集
    phone_rows = by_domain.get("phone", [])
    phone_category_ivs = phone_category_intervals([
        {"ts_utc": r["ts_utc"], "metric": r["metric"], "value_text": r["value_text"]}
        for r in phone_rows
        if r["metric"] in ("unlock", "app_switch", "screen_on", "screen_off")
    ])
    phone_dims = {
        category: round(_interval_minutes(ivs) / 60.0, 2)
        for category, ivs in phone_category_ivs.items()
    }
    phone_intervals = [iv for ivs in phone_category_ivs.values() for iv in ivs]

    # 锻炼：手表记录的每一段 workout 都是真实的“在活动”。
    workout_intervals: list[tuple[datetime, datetime]] = []
    for r in by_domain.get("fitness", []):
        if r["metric"] != "workout":
            continue
        start = _parse_ts(r["ts_utc"])
        meta = meta_of(r)
        end = _parse_ts(meta.get("end_utc"))
        if end is None and start is not None:
            end = start + timedelta(minutes=num(r["value_num"], 1) or 0.0)
        if start is not None and end is not None and end > start:
            workout_intervals.append((start, end))

    # collapsedHours：把四个来源的**区间**并起来，重叠部分只算一次。
    # 桌面块用首尾跨度（块内部的空档 ≤ MERGE_GAP，量很小）；跨午夜的部分裁掉。
    day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    desktop_intervals = [
        (_parse_ts(r["start_utc"]), _parse_ts(r["end_utc"]))
        for r in block_rows
        if (num(r["hours"], 3) or 0) > 0
    ]
    desktop_intervals = [(a, b) for a, b in desktop_intervals if a and b]

    intervals: list[tuple[datetime, datetime]] = []
    for group in (desktop_intervals, transport_intervals,
                  workout_intervals, phone_intervals):
        intervals.extend(_clip_intervals(group, day_start, day_end))
    collapsed = round(_interval_minutes(intervals) / 60.0, 2) if intervals else None

    return {
        "v": 1,
        "day": day,
        "categories": categories,
        "trackedHours": tracked,
        "topApps": top_apps,
        "topSites": build_sites(by_domain.get("web", [])),
        "health": health,
        "location": location,
        "phone": phone,
        "ai": build_ai(by_domain.get("ai", [])),
        "workouts": build_workouts(by_domain.get("fitness", [])),
        "dimensions": build_dimensions(categories, health, location, phone_dims),
        # 七个维度是各来源读数的**总和**，同一分钟可能被两个来源各算一次
        # （最典型：电脑前刷手机 → Social 和桌面分类同时计时）。
        # 这个字段是同一天所有来源区间的**并集**，所以它 ≤ 七维之和。
        "collapsedHours": collapsed,
    }


def has_content(day_obj: dict) -> bool:
    # dimensions 是从下面这些源派生的；它总是 dict，不能单独拿来判断“有没有数据”，
    # 否则空的 30 天会被整批发上去。
    return any(
        day_obj.get(k) is not None
        for k in ("categories", "trackedHours", "topApps", "topSites", "health",
                  "location", "phone", "ai", "workouts")
    )


def encode_day(day_obj: dict) -> str:
    raw = json.dumps(day_obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    size = len(raw.encode("utf-8"))
    if size > MAX_DAY_BYTES:
        raise ValueError(f"day {day_obj['day']} payload is {size} bytes (max {MAX_DAY_BYTES})")
    return raw


# ── 发送 ──────────────────────────────────────────────────────

def post_days(url: str, token: str, days: list[dict], chunk_size: int, timeout: float) -> int:
    sent = 0
    for start in range(0, len(days), chunk_size):
        chunk = days[start:start + chunk_size]
        body = {"v": 1, "days": chunk}
        last_error = None
        for attempt in range(4):
            try:
                res = httpx.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(2 ** attempt)
                continue

            if res.status_code == 200:
                sent += len(chunk)
                break

            last_error = RuntimeError(f"HTTP {res.status_code}: {res.text[:200]}")
            # 兼容旧 Worker：它的 DAY_KEYS 还没有 allTime。
            if (res.status_code == 400
                    and any("allTime" in d for d in chunk)):
                for d in chunk:
                    d.pop("allTime", None)
                print("  ⚠️  Worker 还不认识 allTime；这批先去掉它再发。")
                continue
            # 兼容旧 Worker：它的 DAY_KEYS 还没有 workouts。部署新 Worker 之前，
            # 当天 23:00 的流水线不能因为一个新字段整批失败。
            if (res.status_code == 400
                    and any("workouts" in d for d in chunk)):
                for d in chunk:
                    d.pop("workouts", None)
                print("  ⚠️  Worker 还不认识 workouts；这批先去掉它再发。")
                continue
            # 兼容旧 Worker：它的 DAY_KEYS 还没有 topSites。实时流水线每 2 分钟
            # 跑一次，不能因为一个新字段整批失败 —— 剥掉它重试一次；部署新
            # Worker 后再跑 --days 30 就能把历史站点补回来。
            if (res.status_code == 400 and "topSites" in res.text
                    and any("topSites" in d for d in chunk)):
                for d in chunk:
                    d.pop("topSites", None)
                print("  ⚠️  Worker 还不认识 topSites；这批先去掉它再发。")
                continue
            # 兼容旧 Worker：它的 DAY_KEYS 还没有 dimensions。部署新 Worker 之前，
            # 当天 23:00 的流水线不能因为一个新字段整批失败 —— 剥掉 dimensions
            # 重试一次；部署后再跑 --days 30 就能把历史补回来。
            if (res.status_code == 400
                    and any("dimensions" in d for d in chunk)):
                for d in chunk:
                    d.pop("dimensions", None)
                print("  ⚠️  Worker 还不认识 dimensions；这批先去掉它再发。")
                continue
            # 兼容旧 Worker：它的 HEALTH_KEYS 还没有 hrMax。部署新 Worker 之前，
            # 当天 23:00 的流水线不能因为一个新字段整批失败 —— 剥掉 hrMax
            # 重试一次；部署后再跑 --days 30 就能把历史补回来。
            if (res.status_code == 400 and "hrMax" in res.text
                    and any(isinstance(d.get("health"), dict) and "hrMax" in d["health"]
                            for d in chunk)):
                for d in chunk:
                    if isinstance(d.get("health"), dict):
                        d["health"].pop("hrMax", None)
                print("  ⚠️  Worker 还不认识 health.hrMax；这批先去掉它再发。")
                continue
            if res.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                retry_after = res.headers.get("Retry-After", "")
                wait = min(float(retry_after), 10.0) if retry_after.isdigit() else float(2 ** attempt)
                time.sleep(wait)
                continue
            break

        if sent < start + len(chunk):
            raise RuntimeError(f"publish failed for {chunk[0]['day']}..{chunk[-1]['day']}: {last_error}")
    return sent


def resolve_ingest_url(site_cfg: dict, override: str | None) -> str:
    base = (override or site_cfg.get("ingest_url") or "https://cms.liveinpassion.me/api/life/ingest").strip()
    if base.endswith("/api/life/ingest"):
        return base
    return base.rstrip("/") + "/api/life/ingest"


def resolve_days(args: argparse.Namespace, site_cfg: dict, tz: ZoneInfo) -> list[str]:
    if args.date:
        out = []
        for value in args.date:
            try:
                out.append(date.fromisoformat(value).isoformat())
            except ValueError:
                raise SystemExit(f"✗ --date 需要 YYYY-MM-DD，收到 {value!r}")
        return sorted(set(out))

    end = date.fromisoformat(args.end) if args.end else datetime.now(tz).date()
    days = int(args.days or site_cfg.get("days") or 30)
    days = max(1, min(90, days))
    return [(end - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


# ── 主流程 ────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="发布脱敏的 LifeLogging 日汇总到个人站点")
    parser.add_argument("--days", type=int, default=None, help="发布最近 N 天（默认取配置，30）")
    parser.add_argument("--end", default=None, help="截止日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--date", action="append", default=None, help="只发布这些日期，可重复")
    parser.add_argument("--api-base", default=None, help="覆盖 ingest 地址；可给完整 URL 或域名")
    parser.add_argument("--chunk-size", type=int, default=None, help="每批发送天数")
    parser.add_argument("--dry-run", action="store_true", help="只聚合不发送")
    parser.add_argument("--print-json", action="store_true", help="与 --dry-run 一起，打印完整 JSON")
    parser.add_argument("--no-translate", action="store_true", help="不翻译（调试用；会发布中文标签）")
    args = parser.parse_args()

    cfg = load_config()
    site = cfg.get("life_site") or {}
    if not site.get("enabled", False):
        print("· life_site 未启用，跳过发布")
        return 0

    tz = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    state_dir = Path(cfg.get("state_dir") or (ROOT / "state"))
    categories_cfg = list(cfg.get("categories") or [])

    try:
        conn = open_db(state_dir)
    except FileNotFoundError as exc:
        print(f"✗ {exc}")
        return 2

    days = resolve_days(args, site, tz)
    payloads: list[dict] = []
    skipped: list[str] = []

    all_time = build_all_time(cfg, tz)

    header(f"publish life · {days[0]} → {days[-1]}")
    for day in days:
        try:
            obj = build_day(conn, day, categories_cfg, tz)
            if not has_content(obj):
                skipped.append(day)
                continue
            if all_time:
                obj["allTime"] = all_time
            encode_day(obj)  # 先验证体积
            payloads.append(obj)
            print(
                f"  ✓ {day}  "
                f"{obj['trackedHours'] or 0:.1f}h  "
                f"steps={obj['health']['steps'] if obj['health'] else '—'}  "
                f"unlocks={obj['phone']['unlocks'] if obj['phone'] else '—'}  "
                f"ai={obj['ai']['cost'] if obj['ai'] else '—'}"
            )
        except Exception as exc:  # noqa: BLE001 — 单日失败不影响其它天
            print(f"  ⚠️  {day} 聚合失败：{exc}")
            skipped.append(day)

    conn.close()

    if skipped:
        print(f"  · 跳过 {len(skipped)} 天（无数据或失败）")
    if not payloads:
        print("· 没有可发布的数据")
        return 0

    translate_enabled = bool(site.get("translate_raw", True)) and not args.no_translate
    translation_failed = 0
    if translate_enabled:
        try:
            translator = Translator(
                cfg,
                state_dir,
                max_calls=int(site.get("translate_max_calls") or 30),
            )
        except Exception as exc:  # noqa: BLE001 — 配置/台账问题
            print(f"✗ 翻译器初始化失败：{exc}")
            return 1
        print(f"  · 翻译聚合标签（缓存 {len(translator.cache)} 条）…")
        try:
            payloads = [translator.translate_object(p) for p in payloads]
            for p in payloads:
                encode_day(p)
        except Exception as exc:  # noqa: BLE001
            print(f"✗ 翻译失败：{exc}")
            return 1
        translation_failed = translator.failed
        if has_cjk(json.dumps(payloads, ensure_ascii=False)):
            translation_failed += 1
            print("  ⚠️  聚合 payload 仍含中文")
        print(
            f"  · 翻译完成（缓存 {len(translator.cache)} 条，"
            f"失败 {translation_failed} 段）"
        )
    else:
        print("  · 翻译已跳过（--no-translate 或 life_site.translate_raw=false）")

    total_bytes = len(json.dumps({"v": 1, "days": payloads}, ensure_ascii=False).encode("utf-8"))
    print(f"  · {len(payloads)} 天，共 {total_bytes / 1024:.1f} KiB")

    if args.print_json:
        print(json.dumps({"v": 1, "days": payloads}, ensure_ascii=False, indent=2))

    if translation_failed:
        print(
            f"✗ {translation_failed} 段翻译失败；本轮不发布，"
            f"保留线上英文版（下一轮重试）"
        )
        return 1

    if args.dry_run:
        print("· dry-run：未发送")
        return 0

    token_env = site.get("token_env") or "LIFE_INGEST_TOKEN"
    token = os.environ.get(token_env, "").strip()
    if not token:
        print(f"✗ 环境变量 {token_env} 未设置（应与 Worker secret 相同）")
        return 2

    url = resolve_ingest_url(site, args.api_base)
    chunk_size = int(args.chunk_size or site.get("chunk_size") or 14)
    timeout = float(site.get("timeout_s") or 30)

    try:
        sent = post_days(url, token, payloads, chunk_size, timeout)
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 1

    print(f"✓ 已发布 {sent} 天 → {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
