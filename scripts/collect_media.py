#!/usr/bin/env python3
"""从中继下载 GPX / FIT 锻炼文件。

手机 Tasker 把 Gadgetbridge 导出的 GPX/FIT 传到中继（数据源和
gadgetbridge.db 一样），中继按内容 sha1 存；这里把它们拉到：

    state/gpx/<sha1>.gpx
    state/fit/<sha1>.fit

之后：
    scripts/import_gpx.py   → domain=fitness / metric=watch_track
    scripts/import_fit.py   → 同一个 watch_track（FIT 需要 fitdecode）

用法：
    python scripts/collect_media.py
    python scripts/collect_media.py --kind gpx
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MEDIA_RE = re.compile(r"^[a-f0-9]{40}\.(gpx|fit)$")
KINDS = ("gpx", "fit")


def relay() -> tuple[str, str] | None:
    cfg = load_config()
    src = (cfg.get("sources") or {}).get("phone_relay") or {}
    if not src.get("enabled"):
        return None
    base = str(src.get("api_base") or "").rstrip("/")
    key = str(src.get("api_key") or "")
    if not base or not key or "REPLACE" in (base + key).upper():
        return None
    return base, key


def main() -> int:
    ap = argparse.ArgumentParser(description="Download GPX/FIT workout files from the relay")
    ap.add_argument("--kind", choices=KINDS, action="append")
    args = ap.parse_args()

    conn = relay()
    if not conn:
        print("✗ phone_relay 未启用或 api_base/api_key 没填")
        return 2
    base, key = conn
    headers = {"X-API-Key": key}
    kinds = args.kind or list(KINDS)

    for kind in kinds:
        try:
            r = httpx.get(f"{base}/files/{kind}", headers=headers, timeout=30.0)
            r.raise_for_status()
            files: list[dict[str, Any]] = r.json().get("files") or []
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️  {kind}: list failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        dest_dir = ROOT / "state" / kind
        dest_dir.mkdir(parents=True, exist_ok=True)
        added = 0
        for f in files:
            name = str(f.get("name") or "")
            if not MEDIA_RE.match(name):
                continue
            dest = dest_dir / name
            size = int(f.get("bytes") or 0)
            if dest.exists() and dest.stat().st_size == size:
                continue
            try:
                data = httpx.get(f"{base}/files/{kind}/{name}", headers=headers,
                                 timeout=60.0).content
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️  {kind}/{name}: download failed: {exc}", file=sys.stderr)
                continue
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)
            print(f"  ✓ {kind}/{name}: {len(data)} bytes")
            added += 1
        print(f"  · {kind}: {len(files)} on relay, {added} new")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
