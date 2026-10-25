#!/usr/bin/env python3
"""把 Gadgetbridge 导出的 FIT 锻炼文件接进 events.db。

和 import_gpx.py 一样，最终写成：

    domain="fitness" / metric="watch_track"
    meta.path = [[lat, lon], ...]

FIT 是二进制，需要 ``fitdecode``：

    .venv\\Scripts\\pip.exe install fitdecode

没有安装时会明确提示并跳过，不会影响其它流水线。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Event, EventsClient, load_config  # noqa: E402
from track_samples import build_samples  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "state" / "fit"
STATE_PATH = ROOT / "state" / "fit-imported.json"
MAX_POINTS = 500
SEMICIRCLE = 180.0 / (2 ** 31)


def _val(frame, *names):
    """FIT 记录里取一个可选字段。字段不在定义里时 fitdecode 抛 KeyError。"""
    for name in names:
        try:
            v = frame.get_value(name)
        except (KeyError, ValueError):
            continue
        if v is not None:
            return v
    return None


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(h)))


def parse_fit(path: Path) -> dict[str, Any] | None:
    try:
        from fitdecode import FitDataMessage, FitReader
    except ImportError:
        print("  ⚠️  fitdecode 未安装：.venv\\Scripts\\pip.exe install fitdecode", file=sys.stderr)
        return None

    points: list[tuple[float, float, datetime | None]] = []
    # 轨迹点之外，FIT 每帧还带心率 / 速度 / 海拔。这些是 Gadgetbridge 数据库里
    # 没有的（DB 只有整次的平均值），丢掉就再也拿不回来了。
    series: list[tuple] = []
    try:
        with FitReader(str(path)) as fit:
            for frame in fit:
                # ⚠️ 不能用 frame_type 判断。fitdecode 给的是 IntEnum
                #    （FitFrameType.data == 4），拿它跟字符串 "data" 比永远为真，
                #    结果一条 record 都进不来 —— FIT 全都"解析成功但 0 个点"，
                #    而且不报错。
                # ⚠️ 也不能只按 name 过滤：定义帧（FitDefinitionMessage）的 name
                #    同样是 "record"，但它没有 get_value()，会抛 AttributeError。
                if not isinstance(frame, FitDataMessage):
                    continue
                if getattr(frame, "name", "") != "record":
                    continue
                when = _val(frame, "timestamp")
                if not isinstance(when, datetime):
                    when = None
                lat = _val(frame, "position_lat")
                lon = _val(frame, "position_long")
                if lat is not None and lon is not None:
                    points.append((float(lat) * SEMICIRCLE, float(lon) * SEMICIRCLE, when))
                if when is not None:
                    hr = _val(frame, "heart_rate")
                    speed = _val(frame, "enhanced_speed", "speed")
                    alt = _val(frame, "enhanced_altitude", "altitude")
                    # record 里的 distance 是**设备自己累计的**米数，分段配速和坡度
                    # 都以它为准；拿经纬度累加会在 GPS 抖动上多算一截。
                    dist = _val(frame, "distance")
                    temp = _val(frame, "temperature")
                    series.append((
                        when,
                        float(hr) if hr is not None else None,
                        float(speed) if speed is not None else None,
                        float(alt) if alt is not None else None,
                        float(dist) if dist is not None else None,
                        float(temp) if temp is not None else None,
                    ))
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  {path.name}: FIT parse failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None

    if len(points) < 2:
        print(f"  ⚠️  {path.name}: fewer than 2 GPS records", file=sys.stderr)
        return None

    timed = [p for p in points if p[2] is not None]
    start = min((p[2] for p in timed), default=None)
    end = max((p[2] for p in timed), default=None)
    if start is None or end is None:
        print(f"  ⚠️  {path.name}: no record timestamps", file=sys.stderr)
        return None

    step = max(1, math.ceil(len(points) / MAX_POINTS))
    sampled = points[::step]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    coords = [(round(p[0], 6), round(p[1], 6)) for p in sampled]
    distance = sum(
        _haversine_km(coords[i], coords[i + 1])
        for i in range(len(coords) - 1)
    )
    minutes = max(0.0, (end - start).total_seconds() / 60.0)
    series.sort(key=lambda r: r[0])
    return {
        "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "minutes": round(minutes, 1),
        "distance_km": round(distance, 3),
        "path": [[lat, lon] for lat, lon in coords],
        "points": len(points),
        "samples": build_samples(series, start),
        "file": path.name,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Import Gadgetbridge FIT tracks into events.db")
    ap.add_argument("path", nargs="?", default=str(DEFAULT_DIR))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    target = Path(args.path)
    if target.is_file():
        files = [target]
    elif target.is_dir():
        files = sorted({p.resolve() for ext in ("*.fit", "*.FIT") for p in target.glob(ext)})
    else:
        print(f"✗ not found: {target}", file=sys.stderr)
        return 2
    if not files:
        print(f"· no .fit files in {target}")
        return 0

    cfg = load_config()
    client = None if args.dry_run else EventsClient(cfg)
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}

    imported = skipped = 0
    for path in files:
        st = path.stat()
        key = hashlib.sha1(f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
        if state.get(str(path.resolve())) == key:
            skipped += 1
            continue
        doc = parse_fit(path)
        if not doc:
            continue
        event = Event(
            ts_utc=doc["start"],
            domain="fitness",
            metric="watch_track",
            value_num=doc["minutes"],
            value_text=path.stem[:80],
            source="gadgetbridge-fit",
            meta={
                "path": doc["path"],
                "distance_km": doc["distance_km"],
                "start_utc": doc["start"],
                "end_utc": doc["end"],
                "points": doc["points"],
                # 逐帧心率/速度/海拔（见 track_samples.py）。没有就是 None，
                # 站点那边当「这条没有曲线」处理。
                "samples": doc.get("samples"),
                "file": doc["file"],
            },
        )
        print(f"  ✓ {path.name}: {doc['distance_km']:.2f} km / {doc['minutes']:.0f} min"
              f" / {len(doc['path'])} pts → {doc['start']}")
        if not args.dry_run:
            client.post_batch([event])
            state[str(path.resolve())] = key
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        imported += 1

    print(f"{'· dry-run' if args.dry_run else '✓'} {imported} FIT file(s), {skipped} unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
