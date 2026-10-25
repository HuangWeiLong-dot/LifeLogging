#!/usr/bin/env python3
"""把 Gadgetbridge 导出的 GPX 路线接进 events.db。

背景：
  Gadgetbridge 的 SQLite 导出里 ``BASE_ACTIVITY_SUMMARY.GPX_TRACK`` 是 NULL，
  所以手表有距离/配速/心率，却没有地图路线。Gadgetbridge 的「导出 GPX」
  会生成带时间戳的 trkpt；这个脚本把它们写成：

      domain="fitness" / metric="watch_track"
      meta.path = [[lat, lon], ...]
      meta.distance_km / start_utc / end_utc / file / points

  站点 LIFE 的地图读 raw events 时会把这条路径画成虚线（watch track），
  和手机 Dawarich 的实线轨迹区分开。

用法：
  python scripts/import_gpx.py                 # 读 state/gpx/*.gpx
  python scripts/import_gpx.py path/to/run.gpx
  python scripts/import_gpx.py path/to/folder --dry-run

只导入「文件内容变了」或「还没导入过」的文件；state/gpx-imported.json
记 path+mtime+size，重复跑不会产生重复事件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Event, EventsClient, load_config  # noqa: E402
from track_samples import build_samples  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "state" / "gpx"
STATE_PATH = ROOT / "state" / "gpx-imported.json"
MAX_POINTS = 500


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(h)))


def parse_gpx(path: Path) -> dict[str, Any] | None:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as e:
        print(f"  ⚠️  {path.name}: parse failed: {e}", file=sys.stderr)
        return None

    # (lat, lon, time, ele)。海拔跟着点走，不能单独存一个数组 ——
    # 下面按时间排序会把两个数组拆散。
    points: list[tuple[float, float, datetime | None, float | None]] = []
    for el in tree.iter():
        if _local(el.tag) not in ("trkpt", "rtept", "wpt"):
            continue
        try:
            lat = float(el.get("lat") or "")
            lon = float(el.get("lon") or "")
        except ValueError:
            continue
        when = None
        ele = None
        for child in el:
            tag = _local(child.tag)
            if tag == "time" and when is None:
                when = _parse_time(child.text)
            elif tag == "ele" and ele is None:
                try:
                    ele = float(child.text or "")
                except ValueError:
                    ele = None
        points.append((lat, lon, when, ele))

    if len(points) < 2:
        print(f"  ⚠️  {path.name}: fewer than 2 track points", file=sys.stderr)
        return None

    timed = [p for p in points if p[2] is not None]
    start = min((p[2] for p in timed), default=None)
    end = max((p[2] for p in timed), default=None)
    if start is None or end is None:
        print(f"  ⚠️  {path.name}: no <time> in track points", file=sys.stderr)
        return None

    # 按时间排序；没有时间的点保持文件顺序。
    if len(timed) == len(points):
        points.sort(key=lambda p: p[2])

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

    # 速度 GPX 不直接给，用相邻点的距离/时间差算；只在整条轨迹都有时间时才算，
    # 否则会在缺时间的点上编出一个 0 速度的坑。
    series: list[tuple] = []
    if len(timed) == len(points):
        prev = None
        dist_m = 0.0
        for lat, lon, when, ele in points:
            speed = None
            if prev is not None:
                step_m = _haversine_km((prev[1], prev[2]), (lat, lon)) * 1000.0
                dist_m += step_m
                dt = (when - prev[0]).total_seconds()
                if dt > 0:
                    speed = step_m / dt
            series.append((when, None, speed, ele, dist_m, None))
            prev = (when, lat, lon)

    return {
        "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "minutes": round(minutes, 1),
        "distance_km": round(distance, 3),
        "path": [[lat, lon] for lat, lon in coords],
        "points": len(points),
        "samples": build_samples(series, start) if series else None,
        "file": path.name,
    }


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def file_key(path: Path) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Import Gadgetbridge GPX tracks into events.db")
    ap.add_argument("path", nargs="?", default=str(DEFAULT_DIR),
                    help="GPX file or folder (default: state/gpx)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    target = Path(args.path)
    if target.is_file():
        files = [target]
    elif target.is_dir():
        # Windows 的 glob 大小写不敏感；用 set 去重，避免同一个文件导入两次。
        files = sorted({
            p.resolve() for ext in ("*.gpx", "*.GPX") for p in target.glob(ext)
        })
    else:
        print(f"✗ not found: {target}", file=sys.stderr)
        return 2
    if not files:
        print(f"· no .gpx files in {target}")
        return 0

    cfg = load_config()
    client = None if args.dry_run else EventsClient(cfg)
    state = load_state()
    imported = 0
    skipped = 0

    for path in files:
        key = file_key(path)
        if state.get(str(path.resolve()), {}).get("key") == key:
            skipped += 1
            continue
        doc = parse_gpx(path)
        if not doc:
            continue
        event = Event(
            ts_utc=doc["start"],
            domain="fitness",
            metric="watch_track",
            value_num=doc["minutes"],
            value_text=path.stem[:80],
            source="gadgetbridge-gpx",
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
            state[str(path.resolve())] = {"key": key, "start": doc["start"]}
            save_state(state)
        imported += 1

    if args.dry_run:
        print(f"· dry-run: {imported} file(s) would be imported, {skipped} unchanged")
    else:
        print(f"✓ imported {imported} GPX file(s), skipped {skipped} unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
