"""把 LifeLogging 的日汇总**脱敏**后发布到个人站点（liveinpassion.me/life）。

数据流：
  state/events.db ──(本脚本)──> POST /api/life/ingest ──> Cloudflare D1
                                                    └─> GET /api/life

只发聚合数字：
  · 分类时长、追踪小时数、常用桌面应用（友好名）
  · 步数 / 心率 / 跑步汇总 / VO2max
  · 城市 + 区级停留时长、移动分钟、跨度、轨迹点数（**不含坐标/轨迹**）
  · 手机解锁 / 亮灭屏 / 亮屏分钟 / 应用切换 / 常用应用（显示名）
  · AI 花费（按来源解析，**不含余额**、项目名与请求明细）

绝不发送：lat/lon、track_path、blocks.title/url、窗口标题、原始包名、
日记正文、账号余额。

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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import header, load_config  # noqa: E402
from normalize import summarize_phone  # noqa: E402
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

    top = sorted(apps.items(), key=lambda kv: -kv[1])[:8]
    return (
        {k: round(v, 2) for k, v in sorted(categories.items())},
        round(total, 2),
        [[k, round(v, 2)] for k, v in top],
    )


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

    steps = value("steps")
    if steps is not None and steps < 0:
        steps = None

    calls = {
        "steps": None if steps is None else int(round(steps)),
        "hrAvg": num(value("heart_rate_avg"), 1),
        "hrMin": num(value("heart_rate_min"), 1),
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


def build_location(rows: list[sqlite3.Row]) -> dict | None:
    if not rows:
        return None

    visits: list[tuple[float, str | None, str | None]] = []
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
        elif metric == "transport":
            minutes = num(row["value_num"], 1) or 0.0
            if minutes > 0:
                moving += minutes
        elif metric == "tracked_points":
            p = num(row["value_num"], 0)
            if p is not None:
                points = int(p)
            span = meta_of(row).get("span_km")
            span_km = num(span, 1)

    if not visits and moving == 0 and points is None:
        return None

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

    return {
        "city": main_city,
        "districts": districts,
        "movingMinutes": round(moving, 1),
        "stayMinutes": round(stay, 1),
        "spanKm": span_km,
        "points": points,
    }


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
        if len(apps) >= 8:
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
    projects = projects[:8]

    models = []
    for row in model_rows:
        cost = num(row["value_num"], 4)
        name = clean_name(row["value_text"], 40)
        if cost is not None and name:
            models.append([name, cost])
    models.sort(key=lambda p: -p[1])
    models = models[:8]

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


def build_day(conn: sqlite3.Connection, day: str, categories_cfg: list[str]) -> dict | None:
    blocks = conn.execute(
        """SELECT category, app, SUM(hours) AS hours
           FROM blocks WHERE day_local = ?
           GROUP BY category, app""",
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
    return {
        "v": 1,
        "day": day,
        "categories": categories,
        "trackedHours": tracked,
        "topApps": top_apps,
        "health": build_health(by_domain.get("health", [])),
        "location": build_location(by_domain.get("location", [])),
        "phone": build_phone(by_domain.get("phone", [])),
        "ai": build_ai(by_domain.get("ai", [])),
    }


def has_content(day_obj: dict) -> bool:
    return any(
        day_obj.get(k) is not None
        for k in ("categories", "trackedHours", "topApps", "health", "location", "phone", "ai")
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

    header(f"publish life · {days[0]} → {days[-1]}")
    for day in days:
        try:
            obj = build_day(conn, day, categories_cfg)
            if not has_content(obj):
                skipped.append(day)
                continue
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
