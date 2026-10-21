#!/usr/bin/env python3
"""把手表的**日汇总**推给服务器中继 —— 让飞书机器人答得了「走了多少步」。

**为什么要有它**

    步数 / 心率 / 睡眠已经在笔记本的 `events.db` 里了（Gadgetbridge 那条链路），
    但机器人跑在服务器上，看不到这台笔记本。所以「今天走了多少步」它一直
    只能回一句「手表数据还没接」—— 而数据其实在，只是没送过去。

    和 `push_desktop.py` 同一个思路：不搬原始数据，只搬**已经算好的日汇总**。
    中继只留最新一份，原始数据仍在本地。

**推的是「最后一份有数据的那天」，不是「今天」**

    Gadgetbridge 的导出常常是隔天才到。硬推今天的话，大多数时候是一份空
    快照，机器人会回答「今天没有记录」—— 而实际上昨天那份是完整的。
    所以这里取**最近一个真的有 health/fitness 事件的日期**，把日期一起推上去；
    机器人会明说「这是 X 的，不是今天」。

**睡眠**

    当前这台 Garmin 的导出里**没有睡眠样本**（SLEEP 相关的表都是 0 行），
    所以 payload 里通常没有 `sleep_hours`。有就带上，没有就不编。

用法：
  python scripts/push_health.py                # 推最近一份
  python scripts/push_health.py --dry-run      # 只打印，不推
  python scripts/push_health.py --date 2026-10-20
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])
DB = Path(CFG["state_dir"]) / "events.db"


def relay() -> tuple[str, str] | None:
    cfg = (CFG.get("sources") or {}).get("phone_relay") or {}
    if not cfg.get("enabled"):
        print("-> phone relay disabled (config.yaml -> sources.phone_relay.enabled)")
        return None
    base = (cfg.get("api_base") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    if not base or not key or "REPLACE" in (base + key).upper():
        print("  ⚠️  phone_relay 开着，但 config.yaml 里还是占位符")
        return None
    return base, key


def latest_day(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "select max(day_local) from events where domain in ('health','fitness')"
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def build_payload(rows: list[tuple], day: str, now: datetime) -> dict:
    """事件行 → 快照。纯函数，测试直接喂假行。

    行的形状：(metric, value_num, value_text, meta_json)
    """
    health: dict[str, float] = {}
    workouts: list[dict] = []
    for metric, num, text, meta_json in rows:
        if metric == "workout":
            try:
                meta = json.loads(meta_json or "{}")
            except json.JSONDecodeError:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            workouts.append({
                "kind": str(text or ""),
                "minutes": round(float(num or 0), 1),
                "distance_km": meta.get("distance_km"),
                "avg_hr": meta.get("avg_hr"),
                "max_hr": meta.get("max_hr"),
                "end_utc": meta.get("end_utc"),
            })
        elif num is not None:
            try:
                health[str(metric)] = round(float(num), 2)
            except (TypeError, ValueError):
                continue

    return {
        "day": day,
        "pushed_at": now.isoformat(timespec="seconds"),
        "health": health,
        # 只留最近几次；一年后没人会来服务器看第 40 次跑步。
        "workouts": sorted(workouts, key=lambda w: str(w.get("end_utc") or ""))[-5:],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="把健康日汇总推到中继")
    ap.add_argument("--date", default="", help="YYYY-MM-DD；默认最近一份有数据的天")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    endpoint = relay()
    if not endpoint:
        return 1
    base, key = endpoint

    if not DB.exists():
        print(f"  ⚠️  {DB} 不存在 —— 还没有采集过任何数据")
        return 0

    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except sqlite3.Error as e:
        print(f"  ⚠️  打不开 events.db：{e}")
        return 1
    try:
        day = args.date or latest_day(conn)
        if not day:
            print("  . events.db 里还没有 health/fitness 事件")
            return 0
        rows = conn.execute(
            "select metric, value_num, value_text, meta_json from events "
            "where day_local=? and domain in ('health','fitness')",
            (day,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"  . {day} 没有 health/fitness 事件")
        return 0

    payload = build_payload(rows, day, datetime.now(TZ))
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        print("  --dry-run，不推。")
        return 0

    try:
        r = httpx.post(f"{base}/watch", json=payload,
                       headers={"X-API-Key": key}, timeout=20.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  推不到中继 {base}：{type(e).__name__}: {e}")
        return 1
    if r.status_code == 404:
        print("  ⚠️  中继没有 /watch —— 服务器上跑的还是旧版代码")
        return 1
    if r.status_code != 200:
        print(f"  ⚠️  中继返回 {r.status_code}：{r.text[:200]}")
        return 1

    print(f"  OK  {day} 的健康汇总已推（{len(payload['health'])} 项指标、"
          f"{len(payload['workouts'])} 次运动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
