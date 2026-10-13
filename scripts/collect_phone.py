"""手机端采集：Dawarich（位置 / 停留 / 交通）→ 统一事件表。

Dawarich 的 REST API 在 /api/v1/，鉴权二选一：
  · query 参数  ?api_key=...
  · header      Authorization: Bearer <API_KEY>

数据来源链路（手机侧，本脚本不负责，只负责拉）：
  Android GPSLogger ──POST──> Dawarich /api/v1/owntracks/points?api_key=...
  Android OwnTracks ──HTTP──> 同上
  Android Tasker ───POST───> 本系统的 /ingest/phone（App 切换、锁屏事件）

⚠️ GPSLogger v134 起「忽略电池优化」是强制项，不设会被系统杀。
⚠️ 手机在外网能否直连 Dawarich，取决于服务器有无公网；没有就走 Tailscale。

用法：
  python scripts/collect_phone.py --date today
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Day, Event, EventsClient, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])


def _client() -> tuple[str, dict[str, str]]:
    d = CFG["sources"]["dawarich"]
    base = d["api_base"].rstrip("/")
    return base, {
        "Authorization": f"Bearer {d['api_key']}",
        "Accept": "application/json",
    }


def _get(base: str, headers: dict[str, str], path: str, **params: Any) -> Any:
    r = httpx.get(f"{base}/api/v1{path}", headers=headers, params=params, timeout=60.0)
    if r.status_code == 401:
        raise RuntimeError(
            "Dawarich returned 401: wrong API key.\n"
            "  Get it on the server with:\n"
            "    docker compose exec -T dawarich_app bin/rails runner 'puts User.first.api_key'\n"
            "  then put it in config.yaml -> sources.dawarich.api_key"
        )
    r.raise_for_status()
    return r.json()


def collect_visits(day: Day) -> list[Event]:
    """停留点。Dawarich 1.8.0+ 会自动做 stay-point 检测并给置信度。"""
    base, headers = _client()
    out: list[Event] = []

    try:
        data = _get(
            base, headers, "/visits",
            start_at=day.start_local.isoformat(),
            end_at=day.end_local.isoformat(),
            per_page=500,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Failed to fetch visits: {e}")
        return out

    visits = data.get("visits", data) if isinstance(data, dict) else data
    if not isinstance(visits, list):
        print(f"  ⚠️  Unrecognised visits response shape: {str(data)[:300]}")
        return out

    for v in visits:
        try:
            start = _parse(v.get("started_at") or v.get("start_at"))
            end = _parse(v.get("ended_at") or v.get("end_at"))
        except Exception:  # noqa: BLE001
            continue
        if not start:
            continue

        minutes = ((end - start).total_seconds() / 60.0) if end else 0.0
        place = v.get("place") or {}
        name = (
            v.get("name")
            or place.get("name")
            or v.get("area")
            or f"({v.get('latitude')},{v.get('longitude')})"
        )

        out.append(
            Event(
                ts_utc=start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                domain="location",
                metric="visit",
                value_num=round(minutes, 1),
                value_text=str(name)[:200],
                source="dawarich",
                meta={
                    "lat": v.get("latitude"),
                    "lon": v.get("longitude"),
                    "confidence": v.get("confidence"),
                    "status": v.get("status"),
                },
            )
        )
    print(f"  OK  Dawarich visits: {len(out)} stays")
    return out


def collect_tracks(day: Day) -> list[Event]:
    """行程 + 交通方式。Dawarich 按速度/加速度分类。"""
    base, headers = _client()
    out: list[Event] = []

    try:
        data = _get(
            base, headers, "/tracks",
            start_at=day.start_local.isoformat(),
            end_at=day.end_local.isoformat(),
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Failed to fetch tracks (this version may not have that endpoint): {e}")
        return out

    tracks = data.get("tracks", data) if isinstance(data, dict) else data
    if not isinstance(tracks, list):
        return out

    for t in tracks:
        start = _parse(t.get("start_at") or t.get("started_at"))
        if not start:
            continue
        distance_km = t.get("distance") or 0
        try:
            distance_km = float(distance_km) / 1000.0 if distance_km > 1000 else float(distance_km)
        except (TypeError, ValueError):
            distance_km = 0.0
        minutes = float(t.get("duration") or 0) / 60.0

        out.append(
            Event(
                ts_utc=start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                domain="location",
                metric="transport",
                value_num=round(minutes, 1),
                value_text=str(t.get("mode") or t.get("transportation_mode") or "unknown"),
                source="dawarich",
                meta={"distance_km": round(distance_km, 2)},
            )
        )
    print(f"  OK  Dawarich trips: {len(out)} segments")
    return out


def collect_daily_stats(day: Day) -> list[Event]:
    """Dawarich 自带的当日统计（距离、访问国家等）。"""
    base, headers = _client()
    try:
        data = _get(base, headers, "/stats", start_at=day.start_local.isoformat(),
                    end_at=day.end_local.isoformat())
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Failed to fetch stats: {e}")
        return []

    out: list[Event] = []
    for key in ("distance", "total_distance", "points_count"):
        if key in data and isinstance(data[key], (int, float)):
            out.append(
                Event(
                    ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    domain="location",
                    metric=f"daily_{key}",
                    value_num=float(data[key]),
                    source="dawarich",
                )
            )
    return out


def collect_points_summary(day: Day) -> list[Event]:
    """原始轨迹点 —— 汇总成一条事件。

    为什么需要它：`visits` 是**停留检测推导出来的**，需要在一个地方待够时间
    才会生成。出门买个菜、或者刚上线测试时，visits 是空的，但点已经采到了。
    没有这个兜底，日记里位置那块会永远空白 —— 而实际上你确实出去过。

    ⚠️ 字段名是实测确认的，不是猜的：
         latitude / longitude 是**字符串**（"39.9042"），必须 float() 转换，
         直接拿去算距离会得到字符串拼接的荒谬结果。
         timestamp 是 unix 秒。

    只记汇总不记每个点：原始点在 Dawarich 自己的库里，可视化直接查那边，
    再往 events 表灌一份是重复存储（一天最多 1440 个点）。
    """
    base, headers = _client()
    try:
        data = _get(
            base, headers, "/points",
            start_at=day.start_local.isoformat(),
            end_at=day.end_local.isoformat(),
            per_page=5000,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Failed to fetch points: {e}")
        return []

    pts = data.get("points", data) if isinstance(data, dict) else data
    if not isinstance(pts, list) or not pts:
        return []

    lats: list[float] = []
    lons: list[float] = []
    cities: dict[str, int] = {}

    for p in pts:
        try:
            lat = float(p.get("latitude"))
            lon = float(p.get("longitude"))
        except (TypeError, ValueError):
            continue          # 字段缺失或不是数字，跳过而不是崩
        lats.append(lat)
        lons.append(lon)
        place = (p.get("city") or p.get("country_name") or "").strip()
        if place:
            cities[place] = cities.get(place, 0) + 1

    if not lats:
        return []

    # 活动范围：经纬跨度取大的那个 × 111km/度（粗略，够日记用）
    span_km = max(max(lats) - min(lats), max(lons) - min(lons)) * 111.0
    top_place = max(cities, key=lambda k: cities[k]) if cities else ""

    # Dawarich 的 city 字段实测是空的 —— 它的反向地理编码**默认关闭**，
    # 开启需要配 provider，而那几个 provider（chibigeo/Geoapify/Photon/Nominatim）
    # 基本都是托管服务，等于把坐标发给第三方。
    # 所以这里用本地的 GeoNames 城市表兜底，全程离线。
    if not top_place and lats:
        try:
            from geo import describe as geo_describe, ensure_dataset

            if ensure_dataset(quiet=True):
                top_place = geo_describe(sum(lats) / len(lats), sum(lons) / len(lons))
        except Exception as e:  # noqa: BLE001
            print(f"  · local geocoding unavailable ({e}); using coordinates only")

    print(f"  OK  Dawarich track points: {len(lats)} points, span about {span_km:.1f} km"
          + (f", main place {top_place}" if top_place else "(no reverse-geocoded place)"))

    return [
        Event(
            ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            domain="location",
            metric="tracked_points",
            value_num=len(lats),
            value_text=top_place,
            source="dawarich",
            meta={
                "lat_center": round(sum(lats) / len(lats), 4),
                "lon_center": round(sum(lons) / len(lons), 4),
                "span_km": round(span_km, 1),
                "first_point_utc": _ts_to_iso(min(
                    (p.get("timestamp") for p in pts
                     if isinstance(p.get("timestamp"), (int, float))), default=None)),
                "last_point_utc": _ts_to_iso(max(
                    (p.get("timestamp") for p in pts
                     if isinstance(p.get("timestamp"), (int, float))), default=None)),
            },
        )
    ]


def _ts_to_iso(ts: Any) -> str | None:
    """unix 秒 → ISO8601 UTC。"""
    if not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (OSError, OverflowError, ValueError):
        return None


def _parse(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def pull_phone_events(day: Day) -> list[Event]:
    """从事件表里取出手机端直接上报的事件（Tasker 的 App 切换 / 锁屏），
    只做计数统计，本体已经在表里了，不重复写回。"""
    client = EventsClient(CFG)
    if not client.ping():
        return []
    try:
        evs = client.query(
            domain="phone", start=day.start_utc, end=day.end_utc, limit=50_000
        )
    except Exception:  # noqa: BLE001
        return []
    if evs:
        unlocks = sum(1 for e in evs if e.get("metric") == "unlock")
        switches = sum(1 for e in evs if e.get("metric") == "app_switch")
        print(f"  OK  Phone-reported events: {unlocks} unlocks, {switches} app switches")
    return []


def main() -> None:
    from common import cli_day

    day = Day.parse(cli_day(), TZ)
    header(f"Collecting phone · {day.iso}")

    if not CFG["sources"]["dawarich"]["enabled"]:
        print("-> Dawarich disabled, skipping")
        return

    events: list[Event] = []
    print("→ Dawarich")
    events += collect_visits(day)
    events += collect_tracks(day)
    events += collect_points_summary(day)   # visits 为空时的兜底
    events += collect_daily_stats(day)
    pull_phone_events(day)  # 只打印统计

    if not events:
        print("\n⚠️  No location data. This almost always means the phone has not sent a single point.")
        print("   Check in this order:")
        print("   1. Does the Dawarich URL open a login page on the phone? (No -> Tailscale is not connected)")
        print("   2. Is 'ignore battery optimisation' set for GPSLogger / OwnTracks? (Without it the OS kills it)")
        print("   3. Is location permission 'Allow all the time'? (not 'While using the app')")
        print("   4. Is auto-send / HTTP reporting enabled?")
        print("   5. Are the URL and the body template correct?")
        print()
        print("   To confirm on the server side, run this there:")
        print("     docker compose exec dawarich_db psql -U postgres \\")
        print("       -d dawarich_development -c \"SELECT COUNT(*) FROM points;\"")
        return

    client = EventsClient(CFG)
    if not client.ping():
        print(f"\nFAIL events API is not running ({client.base}）")
        return

    try:
        httpx.delete(f"{client.base}/day", headers=client.headers,
                     params={"day": day.iso, "source": "dawarich"}, timeout=30.0)
    except Exception:  # noqa: BLE001
        pass

    n = client.post_batch(events)
    print(f"\nOK  Written to events table: {n} rows")


if __name__ == "__main__":
    main()
