#!/usr/bin/env python3
"""把中继上的 GPX / FIT 发布成站点的「手表路线」文档。

**它解决什么**

    中继 24 小时收手机传来的锻炼文件，但「文件 → 站点」这一步原来只在笔记本的
    run_daily.ps1 里（collect_media → import_gpx/fit → publish_life_raw）。
    笔记本一关机，那天的地图上就没有手表虚线 —— 哪怕文件早就躺在服务器上。

    这里绕开 events.db，直接把解析结果写成一份 `watch` 类型的原始文档：

        life/raw/watch/<day>.json   →  GET /api/life/raw/watch/<day>

    站点读地图时把它和笔记本发布的 `events` 一起交给 parseLocationEvents，
    按**开始时间**去重（同一次锻炼的 FIT / GPX 开始时间逐秒相同），只画一条。

**它不做什么**

    · 不写 events.db，也不碰笔记本的 state/gpx|fit —— 笔记本那条链路照常跑，
      两边谁先到都不冲突。
    · 不做聚合（steps / 心率 / workouts 仍在笔记本的 publish_life_site 里）。

**配置**（/etc/lifelogging/watch.env）

    LIFE_INGEST_TOKEN   必填，与 Worker secret 相同
    LIFE_API_BASE       默认 https://cms.liveinpassion.me
    RELAY_DATA_DIR      默认 /var/lib/lifelogging（与中继的目录一致）
    WATCH_WINDOW_DAYS   默认 30，只发布最近 N 天
    WATCH_TZ            默认 Asia/Shanghai（按本地日期分天）

用法：

    python3 server/watch-publisher/publish_watch.py
    python3 server/watch-publisher/publish_watch.py --dry-run
    python3 server/watch-publisher/publish_watch.py --days 7
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from import_fit import parse_fit  # noqa: E402
from import_gpx import parse_gpx  # noqa: E402

DATA_DIR = Path(os.environ.get("RELAY_DATA_DIR", "/var/lib/lifelogging")).resolve()
API_BASE = (os.environ.get("LIFE_API_BASE") or "https://cms.liveinpassion.me").rstrip("/")
TOKEN = os.environ.get("LIFE_INGEST_TOKEN", "").strip()
WINDOW_DAYS = max(1, int(os.environ.get("WATCH_WINDOW_DAYS", "30")))
TZ = ZoneInfo(os.environ.get("WATCH_TZ", "Asia/Shanghai"))
STATE_PATH = DATA_DIR / "watch-published.json"
MEDIA = {"gpx": DATA_DIR / "gpx", "fit": DATA_DIR / "fit"}
POST_URL = f"{API_BASE}/api/life/raw/ingest"


def day_of(ts_utc: str) -> str | None:
    """把 UTC 时间戳落到本地日期。分天口径必须和笔记本的 publish_life_raw 一致。"""
    try:
        dt = datetime.fromisoformat(str(ts_utc).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(TZ).date().isoformat()


def to_event(kind: str, path: Path, doc: dict) -> dict:
    """转成站点 parseLocationEvents 认识的形状（和 import_gpx/fit 写的一致）。"""
    return {
        "ts_utc": doc["start"],
        "domain": "fitness",
        "metric": "watch_track",
        "value_num": doc["minutes"],
        "value_text": path.name[:80],
        "source": f"watch-publisher-{kind}",
        "meta": {
            "path": doc["path"],
            "distance_km": doc["distance_km"],
            "start_utc": doc["start"],
            "end_utc": doc["end"],
            "points": doc["points"],
            # FIT 有逐秒心率/速度/海拔，GPX 有速度/海拔（见 scripts/track_samples.py）
            "samples": doc.get("samples"),
            "file": path.name,
        },
    }


def _pick_samples(a: dict | None, b: dict | None) -> dict | None:
    """同一活动的 FIT 和 GPX 各有一份采样：心率只有 FIT 有。

    换路径（留点数更多的那份）时不能顺手把它丢掉，否则跑步弹窗里的心率曲线
    会因为 GPX 恰好点数多而消失。
    """
    if a and a.get("hr"):
        return a
    if b and b.get("hr"):
        return b
    return a or b


def collect() -> dict[str, list[dict]]:
    """扫两个目录，返回 {本地日期: [事件]}。同一次活动的两份文件在这里就合并。"""
    by_day: dict[str, dict[str, dict]] = {}
    scanned = parsed = 0

    for kind, folder in MEDIA.items():
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob(f"*.{kind}")):
            scanned += 1
            doc = parse_gpx(path) if kind == "gpx" else parse_fit(path)
            if not doc:
                continue
            day = day_of(doc["start"])
            if not day:
                continue
            event = to_event(kind, path, doc)
            bucket = by_day.setdefault(day, {})
            # 同一活动（开始时间相同）：只留点数多的那条路径，和站点同一口径；
            # 采样则取有心率的那份（只有 FIT 有）。
            prev = bucket.get(doc["start"])
            if prev is None:
                bucket[doc["start"]] = event
            else:
                keep = event if len(doc["path"]) > len(prev["meta"]["path"]) else prev
                other = prev if keep is event else event
                merged = dict(keep)
                merged["meta"] = dict(keep["meta"])
                merged["meta"]["samples"] = _pick_samples(
                    keep["meta"].get("samples"), other["meta"].get("samples"),
                )
                bucket[doc["start"]] = merged
            parsed += 1

    out = {
        day: sorted(bucket.values(), key=lambda e: e["ts_utc"])
        for day, bucket in by_day.items()
    }
    print(f"  · 扫了 {scanned} 个文件，解析出 {parsed} 条，合并后 {sum(len(v) for v in out.values())} 条")
    return out


def load_state() -> dict[str, str]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def build_doc(day: str, events: list[dict]) -> tuple[dict, str]:
    payload = {
        "v": 1,
        "day": day,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "events": events,
    }
    # ⚠️ 摘要**不能**把 generated_at 算进去：它每次运行都变，那样状态文件永远对不上，
    #    「内容没变就跳过」会退化成每 15 分钟往 R2 重写一遍（一天 96 次）。
    #    只对真正的内容 —— 版本、日期、轨迹点 —— 取哈希。
    key = json.dumps({"v": payload["v"], "day": day, "events": events},
                     ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return payload, digest


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish relay GPX/FIT tracks to the site")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不 POST、不写状态")
    ap.add_argument("--days", type=int, default=WINDOW_DAYS, help="只发布最近 N 天")
    args = ap.parse_args()

    if not args.dry_run and not TOKEN:
        print("✗ LIFE_INGEST_TOKEN 未设置（应与 Worker secret 相同）", file=sys.stderr)
        return 2

    today = datetime.now(TZ).date()
    since = (today - timedelta(days=max(1, args.days) - 1)).isoformat()

    by_day = collect()
    state = load_state()

    docs = []
    for day in sorted(by_day):
        if day < since:
            continue
        payload, digest = build_doc(day, by_day[day])
        if state.get(day) == digest:
            continue
        docs.append({
            "kind": "watch",
            "id": day,
            "format": "json",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            "_digest": digest,
        })

    if not docs:
        print(f"· 最近 {args.days} 天没有新变化（{len(by_day)} 天有数据）")
        return 0

    print(f"  · 待发布 {len(docs)} 天：{', '.join(d['id'] for d in docs)}")
    if args.dry_run:
        for d in docs:
            print(f"    {d['id']}: {len(json.loads(d['content'])['events'])} 条")
        print("· dry-run，没有写入")
        return 0

    try:
        res = httpx.post(
            POST_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"v": 1, "docs": [{k: v for k, v in d.items() if k != "_digest"} for d in docs]},
            timeout=60.0,
        )
        res.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        detail = getattr(getattr(exc, "response", None), "text", "")[:200]
        print(f"✗ 发布失败: {type(exc).__name__}: {exc} {detail}", file=sys.stderr)
        return 1

    for d in docs:
        state[d["id"]] = d["_digest"]
    # 只留窗口内的摘要，避免文件无限增长
    state = {k: v for k, v in state.items() if k >= since}
    save_state(state)

    print(f"✓ 已发布 {len(docs)} 天 → {POST_URL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
