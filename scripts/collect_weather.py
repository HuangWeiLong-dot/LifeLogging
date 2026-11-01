#!/usr/bin/env python3
"""天气 —— 当天的温度 / 降水 / 日照 / 风速，作为日记和睡眠的旁证。

**为什么要它**

    一天写不出来，很多时候不是「没发生什么」，是缺背景：
    下雨所以没出门、闷热所以睡得差、日照少所以情绪低。
    这些事你自己未必会写进日记，但天气是现成的。

**这是一个外发请求，默认关闭**

    和仓库里其它采集不同，天气**必须**联网，而且是发给第三方
    （Open-Meteo，免费无需 key）。坐标会离开这台机器 —— 所以
    `sources.weather.enabled` 默认 false，要你自己打开。

    打开之后坐标会先**粗化到小数点后 2 位**（约 1.1 km）再发。
    天气根本不需要比这更准，粗化只是别把门牌号送出去。

**坐标从哪来**

    优先级：
      1. config.yaml 的 sources.weather.lat / lon（固定一个地方时最省事）
      2. 当天的位置轨迹中点（`location/tracked_points` 的 meta）——
         出门在外时用的是**当地**天气，不是家里的
      3. 都没有 → 跳过，别瞎猜

用法：
  python scripts/collect_weather.py --date today
  python scripts/collect_weather.py --date 2026-10-20 --dry-run
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Day, Event, EventsClient, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])

SOURCE = "open-meteo"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DAILY_FIELDS = ",".join([
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
    "sunshine_duration",
    "wind_speed_10m_max",
    "weather_code",
])

# Open-Meteo 的历史库有 5 天左右的滞后。这几天之内用 forecast 的
# `past_days`（模式再分析），更早的走 archive。两边字段名一致。
ARCHIVE_LAG_DAYS = 6

# WMO 天气代码 → 短标签。日记是英文的，所以标签也用英文。
WMO = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "slight rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "slight snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "slight rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def config() -> dict:
    return (CFG.get("sources") or {}).get("weather") or {}


def resolve_coords(client: EventsClient, day: Day) -> tuple[float, float, str] | None:
    """返回 (lat, lon, 来源)。找不到就 None —— 不拿本机位置瞎猜。"""
    w = config()
    lat, lon = w.get("lat"), w.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
        return float(lat), float(lon), "config"

    try:
        rows = client.query(domain="location", metric="tracked_points",
                            start=day.start_utc, end=day.end_utc, limit=10)
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        meta = r.get("meta") or {}
        a, b = meta.get("lat_center"), meta.get("lon_center")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return float(a), float(b), "location"
    return None


def fetch(lat: float, lon: float, day: Day, today: date) -> dict | None:
    """取某一天的日汇总。返回 Open-Meteo 的 daily 数组（已定位到那一天）。"""
    age = (today - day.d).days
    if age >= ARCHIVE_LAG_DAYS:
        url, params = ARCHIVE_URL, {
            "latitude": lat, "longitude": lon,
            "start_date": day.iso, "end_date": day.iso,
            "daily": DAILY_FIELDS, "timezone": CFG["timezone"],
        }
    else:
        url, params = FORECAST_URL, {
            "latitude": lat, "longitude": lon,
            # 今天 age=0 → past_days 至少给 1，否则取不到当天的日汇总
            "past_days": max(1, age + 1), "forecast_days": 1,
            "daily": DAILY_FIELDS, "timezone": CFG["timezone"],
        }

    try:
        r = httpx.get(url, params=params, timeout=30.0)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  取天气失败（{type(e).__name__}: {e}）")
        print("      Open-Meteo 在境外；如果本机要走代理，确认 HTTP(S)_PROXY 已设置。")
        return None

    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    if day.iso not in times:
        print(f"  ⚠️  返回里没有 {day.iso}（拿到的是 {times[:1]}…{times[-1:]}）")
        return None
    i = times.index(day.iso)
    return {k: (v[i] if isinstance(v, list) and i < len(v) else None)
            for k, v in daily.items() if k != "time"}


def to_events(d: dict, day: Day, how: str, lat: float, lon: float) -> list[Event]:
    ts = day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {"lat": lat, "lon": lon, "coords_from": how}

    def ev(metric: str, value, text: str = "") -> Event:
        return Event(ts_utc=ts, domain="weather", metric=metric,
                     value_num=value, value_text=text, source=SOURCE, meta=meta)

    out: list[Event] = []

    code = d.get("weather_code")
    if code is not None:
        try:
            code_i = int(round(float(code)))
            out.append(ev("condition", code_i, WMO.get(code_i, f"code {code_i}")))
        except (TypeError, ValueError):
            pass

    for metric, key, div, digits in (
        ("temp_max", "temperature_2m_max", 1.0, 1),
        ("temp_min", "temperature_2m_min", 1.0, 1),
        ("temp_mean", "temperature_2m_mean", 1.0, 1),
        ("precip_mm", "precipitation_sum", 1.0, 1),
        ("wind_max_kmh", "wind_speed_10m_max", 1.0, 1),
        # sunshine_duration 是**秒**。换成分钟，和项目里其它时长单位一致。
        ("sunshine_min", "sunshine_duration", 60.0, 0),
    ):
        v = d.get(key)
        if v is None:
            continue
        try:
            out.append(ev(metric, round(float(v) / div, digits)))
        except (TypeError, ValueError):
            continue
    return out


def main() -> int:
    from common import cli_day

    ap = argparse.ArgumentParser(description="采集当天天气")
    ap.add_argument("--date", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    day = Day.parse(args.date or cli_day(), TZ)
    header(f"Collect weather · {day.iso}")

    if not config().get("enabled"):
        print("-> 天气采集关闭（config.yaml -> sources.weather.enabled）")
        print("   注意：打开后会把坐标发给 Open-Meteo（免费的第三方服务）。")
        return 0

    client = EventsClient(CFG)
    if not client.ping():
        print(f"⚠️  events API 没在跑（{client.base}）")
        return 1

    coords = resolve_coords(client, day)
    if coords is None:
        print("  . 拿不到坐标：config 里没填 sources.weather.lat/lon，当天也没有位置轨迹")
        return 0

    lat, lon, how = coords
    # 粗化到 2 位小数（约 1.1 km）。天气不需要更准，粗化只是别把住址送出去。
    lat, lon = round(lat, 2), round(lon, 2)
    print(f"  · 坐标 {lat}, {lon}（来自 {how}，已粗化）")

    today = datetime.now(TZ).date()
    daily = fetch(lat, lon, day, today)
    if daily is None:
        return 1

    events = to_events(daily, day, how, lat, lon)
    if not events:
        print("  . 天气接口没返回任何可用字段")
        return 0

    for e in events:
        unit = {"temp_max": "°C", "temp_min": "°C", "temp_mean": "°C",
                "precip_mm": "mm", "wind_max_kmh": "km/h", "sunshine_min": "min"}.get(e.metric, "")
        print(f"    {e.metric:14s} {e.value_num}{unit} {e.value_text}")

    if args.dry_run:
        print("\n(--dry-run，不写库)")
        return 0

    # 先删当天这个 source 的旧行：天气会修正（预报 → 实测），
    # 唯一索引不含 value_num，不删的话改过的温度写不进去。
    try:
        httpx.delete(f"{client.base}/day", headers=client.headers,
                     params={"day": day.iso, "source": SOURCE}, timeout=30.0)
    except Exception:  # noqa: BLE001
        pass

    n = client.post_batch(events)
    print(f"\nOK  写入 {n} 行（source={SOURCE}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
