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

import math
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


# mode_timeline 里的分段只带 emoji，没有模式名 —— 得自己映射。
# 📍 是「没在移动」，不是一种交通方式，单独处理。
MODE_EMOJI = {
    "🚗": "driving",
    "🚕": "driving",
    "🚌": "bus",
    "🚇": "subway",
    "🚆": "train",
    "🚂": "train",
    "🚄": "train",
    "🚲": "cycling",
    "🚴": "cycling",
    "🛵": "motorcycle",
    "🏍️": "motorcycle",
    "🚶": "walking",
    "🏃": "running",
    "✈️": "flying",
    "🚢": "boat",
    "⛵": "boat",
}
STATIONARY_EMOJI = "📍"


def collect_tracks(day: Day) -> list[Event]:
    """行程 + 交通方式。Dawarich 按速度/加速度分类。

    ⚠️ 这个函数曾经**一直静默返回空**，日志里连一行提示都没有。

    实测（Dawarich 1.9）的返回形状是 **GeoJSON FeatureCollection**：

        {"type": "FeatureCollection", "features": [
           {"type": "Feature", "geometry": {"type": "LineString", ...},
            "properties": {"start_at": ..., "end_at": ...,
                           "distance": 107848,     # 米
                           "duration": 49986,      # 秒
                           "avg_speed": 7.77,
                           "dominant_mode": "driving",
                           "mode_timeline": [{"start_time": .., "end_time": .., "emoji": "🚗"}, ...]}}]}

    旧代码写的是 `data.get("tracks", data)` 然后 `isinstance(tracks, list)`，
    dict 进不了那个分支，直接 return [] —— 既不报错也不打印。
    有用的字段全在 `feature["properties"]` 里，外面那层没有。

    ⚠️ 另外**不要把整条 track 当成一次通勤**：Dawarich 会把一整天归成一条
    track（实测 00:00→13:53、13.9 小时），`dominant_mode` 是全天的主导模式。
    直接写「开车 13.9 小时」是错的 —— 那 13.9 小时里大部分时间人在原地。
    有意义的粒度是 `mode_timeline`：它把一天切成「📍原地 / 🚴骑行 / 🚗驾车」等段。
    所以这里只把**真正在移动**的段写进事件表，📍的段跳过。
    """
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

    # 三种形状都认：GeoJSON（实测）、旧版 {"tracks": [...]}、裸数组。
    feats: list[Any] | None = None
    if isinstance(data, dict) and isinstance(data.get("features"), list):
        feats = data["features"]
    elif isinstance(data, dict) and isinstance(data.get("tracks"), list):
        feats = data["tracks"]
    elif isinstance(data, list):
        feats = data

    if feats is None:
        print(f"  ⚠️  Unrecognised tracks response shape: {str(data)[:200]}")
        return out

    for t in feats:
        if not isinstance(t, dict):
            continue
        props = t.get("properties") if isinstance(t.get("properties"), dict) else t
        start = _parse(props.get("start_at") or props.get("started_at"))
        if not start:
            continue

        # distance 单位是**米**（实测 107848 = 107.8km）。
        # 旧代码「>1000 就当米，否则当公里」的猜测在两个形状下都站不住。
        try:
            distance_km = float(props.get("distance") or 0) / 1000.0
        except (TypeError, ValueError):
            distance_km = 0.0

        mode_timeline = props.get("mode_timeline")
        if isinstance(mode_timeline, list) and mode_timeline:
            for seg in mode_timeline:
                if not isinstance(seg, dict):
                    continue
                emoji = str(seg.get("emoji") or "")
                if emoji == STATIONARY_EMOJI:
                    continue                      # 原地不算交通
                s_ts, e_ts = seg.get("start_time"), seg.get("end_time")
                if not isinstance(s_ts, (int, float)):
                    continue
                try:
                    minutes = (float(e_ts) - float(s_ts)) / 60.0
                except (TypeError, ValueError):
                    continue
                if minutes < 1.0:
                    continue                      # 几十秒的碎片没信息量
                out.append(
                    Event(
                        ts_utc=_ts_to_iso(s_ts) or "",
                        domain="location",
                        metric="transport",
                        value_num=round(minutes, 1),
                        value_text=MODE_EMOJI.get(emoji, f"moving({emoji})"),
                        source="dawarich",
                        meta={"end_utc": _ts_to_iso(e_ts), "track_id": props.get("id")},
                    )
                )
            continue

        # 没有 mode_timeline 的旧版：退回「整条 track 一个事件」，
        # 但用 duration 是秒、distance 是米这两个实测事实。
        try:
            minutes = float(props.get("duration") or 0) / 60.0
        except (TypeError, ValueError):
            minutes = 0.0
        mode = (props.get("dominant_mode") or props.get("mode")
                or props.get("transportation_mode") or "unknown")
        out.append(
            Event(
                ts_utc=start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                domain="location",
                metric="transport",
                value_num=round(minutes, 1),
                value_text=str(mode),
                source="dawarich",
                meta={"distance_km": round(distance_km, 2),
                      "avg_speed_kmh": props.get("avg_speed")},
            )
        )

    # ⚠️ mode_timeline 的分段**不带距离**，只有整条 track 带。
    #    所以这里不能无脑打印 km —— 会打出「51 min, 0.0 km」这种假数据。
    moving_min = sum(e.value_num or 0 for e in out)
    km = sum((e.meta or {}).get("distance_km", 0) for e in out)
    extra = ""
    if out:
        extra = f", {moving_min:.0f} min" + (f", {km:.1f} km" if km else "")
    print(f"  OK  Dawarich trips: {len(out)} moving segments{extra}")
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


def fetch_points(day: Day) -> list[tuple[float, float, float]]:
    """拉当天的原始轨迹点，归一化成 ``[(unix_ts, lat, lon)]``，按时间升序。

    ⚠️ 字段名是实测确认的，不是猜的：
         latitude / longitude 是**字符串**（"39.9042"），必须 float() 转换，
         直接拿去算距离会得到字符串拼接的荒谬结果。
         timestamp 是 unix 秒。

    实测返回是**裸数组**（不是 ``{"points": [...]}``），单页就够 ——
    但 per_page 还是给大一点，别在点数多的一天被默认分页悄悄截断。
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

    raw = data.get("points", data) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []

    out: list[tuple[float, float, float]] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        try:
            lat = float(p.get("latitude"))
            lon = float(p.get("longitude"))
        except (TypeError, ValueError):
            continue          # 字段缺失或不是数字，跳过而不是崩
        ts = p.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue
        out.append((float(ts), lat, lon))

    out.sort(key=lambda r: r[0])
    return out


# 停留判据。见 _cluster_stays 的说明。
STAY_RADIUS_M = 200.0
STAY_GAP_MIN = 10.0
STAY_MIN_MINUTES = 15.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _cluster_stays(
    pts: list[tuple[float, float, float]],
) -> list[tuple[float, float, float, float, int]]:
    """把轨迹点切成停留簇。返回 ``[(start_ts, end_ts, lat, lon, n_points)]``。

    **为什么自己做，而不是等 Dawarich 的 /visits**

      实测 /visits 恒为 0：一整天开车跑了 107km、在两地各待了 4–6 小时，
      它依然返回 ``[]``。而 visit 正是 normalize.py 填日记 Location 分区的
      唯一来源 —— 所以只要它不产出，那一段就永远是空的。

    **判据**（够用，且不会把「在移动」误判成「在停留」）

      · 新点到**当前簇质心**的距离 <= STAY_RADIUS_M
      · 且与上一个点的时间间隔 <= STAY_GAP_MIN

    用质心而不是「与上一个点比较」：手机躺着不动时 GPS 抖动常有上百米，
    链式两两比较会被一次抖动打断，把一段连续的停留切成好几块碎片。
    反过来，真在移动时（骑车 5km/h 也才 83m/min）几十秒就离开 200m 圆了，
    不会被并进前一簇。
    """
    stays: list[tuple[float, float, float, float, int]] = []
    cur: list[tuple[float, float, float]] = []
    sx = sy = 0.0

    for ts, lat, lon in pts:
        if not cur:
            cur, sx, sy = [(ts, lat, lon)], lat, lon
            continue

        clat, clon = sx / len(cur), sy / len(cur)
        gap_min = (ts - cur[-1][0]) / 60.0
        if gap_min <= STAY_GAP_MIN and _haversine_m(clat, clon, lat, lon) <= STAY_RADIUS_M:
            cur.append((ts, lat, lon))
            sx += lat
            sy += lon
        else:
            stays.append((cur[0][0], cur[-1][0], clat, clon, len(cur)))
            cur, sx, sy = [(ts, lat, lon)], lat, lon

    if cur:
        stays.append((cur[0][0], cur[-1][0], sx / len(cur), sy / len(cur), len(cur)))
    return stays


def _describe(lat: float, lon: float) -> str:
    """本地离线反向地理编码；失败就退回坐标，不抛异常。"""
    fallback = f"({lat:.4f},{lon:.4f})"
    try:
        from geo import describe, ensure_dataset

        if ensure_dataset(quiet=True):
            return describe(lat, lon) or fallback
    except Exception as e:  # noqa: BLE001
        print(f"  · local geocoding unavailable ({e}); using coordinates only")
    return fallback


def derive_stays(day: Day, pts: list[tuple[float, float, float]]) -> list[Event]:
    """从原始点推导停留，产出 ``location/visit`` 事件。

    Dawarich 的 /visits 为空时的兜底（实测它一直是空的）。
    每个够久的停留簇写一条 visit，位置用本地 GeoNames 反查 ——
    normalize.py 正是靠这些行填日记的 Location 分区。
    """
    out: list[Event] = []
    for start_ts, end_ts, lat, lon, n in _cluster_stays(pts):
        minutes = (end_ts - start_ts) / 60.0
        if minutes < STAY_MIN_MINUTES:
            continue                      # 路过、等个红灯不算停留
        out.append(
            Event(
                ts_utc=_ts_to_iso(start_ts) or "",
                domain="location",
                metric="visit",
                value_num=round(minutes, 1),
                value_text=_describe(lat, lon),
                source="dawarich",
                meta={
                    "lat": round(lat, 5),
                    "lon": round(lon, 5),
                    "points": n,
                    "end_utc": _ts_to_iso(end_ts),
                    "derived": "local-stay",   # 标出来源，方便和 Dawarich 自己的 visit 区分
                },
            )
        )
    return out


def collect_points_summary(
    day: Day,
    pts: list[tuple[float, float, float]],
    stays: list[tuple[float, float, float, float, int]],
) -> list[Event]:
    """原始轨迹点 —— 汇总成一条事件。

    只记汇总不记每个点：原始点在 Dawarich 自己的库里，可视化直接查那边，
    再往 events 表灌一份是重复存储（一天最多 1440 个点）。

    ⚠️ ``main_place`` 取**停留最久的那个点**，不是所有点的质心。

    质心看着合理，实际会把结果扔到没去过的地方：实测一天「家(青浦) →
    北上海 → 家」，800 个点的质心落在两地之间的华漕镇 —— 于是日记里
    写「主要活动地点：Huacao」，而用户那天根本没到过华漕。
    停留最久的点才是「今天主要在哪」。
    """
    if not pts:
        return []

    lats = [p[1] for p in pts]
    lons = [p[2] for p in pts]

    # 活动范围：经纬跨度取大的那个 × 111km/度（粗略，够日记用）
    span_km = max(max(lats) - min(lats), max(lons) - min(lons)) * 111.0

    # 主要地点 = 停留最久那一簇的位置；没有**够久**的停留就退回质心。
    # ⚠️ 判据必须和 derive_stays 一致（同一套阈值）：纯粹「点在路上」的一天
    #    也会切出很多时长为 0 的簇，把那种簇当「主要地点」等于随机挑一个点。
    best = None
    if stays:
        cand = max(stays, key=lambda s: s[1] - s[0])
        if cand[1] - cand[0] >= STAY_MIN_MINUTES * 60:
            best = cand
    if best:
        _, _, mlat, mlon, _ = best
    else:
        mlat, mlon = sum(lats) / len(lats), sum(lons) / len(lons)
    top_place = _describe(mlat, mlon)

    print(f"  OK  Dawarich track points: {len(pts)} points, span about {span_km:.1f} km"
          + (f", main place {top_place}" if top_place else "(no reverse-geocoded place)"))

    return [
        Event(
            ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            domain="location",
            metric="tracked_points",
            value_num=len(pts),
            value_text=top_place,
            source="dawarich",
            meta={
                "lat_center": round(sum(lats) / len(lats), 4),
                "lon_center": round(sum(lons) / len(lons), 4),
                "span_km": round(span_km, 1),
                "first_point_utc": _ts_to_iso(pts[0][0]),
                "last_point_utc": _ts_to_iso(pts[-1][0]),
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

    # 原始点只拉一次：停留检测和日汇总都要用同一份。
    pts = fetch_points(day)
    stays = _cluster_stays(pts) if pts else []

    visits = collect_visits(day)
    if not visits and pts:
        # Dawarich 的 /visits 实测恒为 0（开一天车、两个地方各待几小时也还是 0）。
        # 它是 normalize.py 填日记 Location 分区的唯一来源 —— 不自己推就是空文件。
        visits = derive_stays(day, pts)
        print(f"  OK  Derived stays from raw points: {len(visits)} visit(s)")

    events += visits
    events += collect_tracks(day)
    events += collect_points_summary(day, pts, stays)
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
