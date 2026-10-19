"""笔记本侧：从服务器中继读取某一天作答的全部问答。

**它解决什么**

    答案在事件表里只有 `<题号>|<答案>|<备注>`，题号里带着提问日期，但**没有
    问题原文**。复盘、日记和长期记忆都需要「问的是什么」，所以由中继的
    `GET /qa?day=` 把两份问题文件（早晨批 + 实时批）与答案按题号对齐。

    ⚠️ 语义是**作答日**：`fetch("2026-10-18")` 返回 10-18 这一天答的题。
       早晨批的问题日通常是 10-17（晚上生成、早上回答），也在这个结果里。

**它不做什么**

    · 不写服务器上的任何文件；中继那边是只读合并。
    · 失败不抛异常 —— 复盘/日记/勾任务都应该能降级，而不是因为中继临时
      不可达就整条流水线停下。

用法：
  python scripts/qa_client.py 2026-10-18
  python scripts/qa_client.py 2026-10-18 --save
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import header, load_config  # noqa: E402

CFG = load_config()
STATE = Path(CFG["state_dir"])


def _endpoint() -> tuple[str, str] | None:
    cfg = (CFG.get("sources") or {}).get("phone_relay") or {}
    if not cfg.get("enabled"):
        print("-> phone relay disabled (config.yaml -> sources.phone_relay.enabled)")
        return None
    base = (cfg.get("api_base") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    if not base or "REPLACE_WITH" in base or "REPLACE_WITH" in key:
        print("  ⚠️  phone_relay is enabled but still has placeholder values.")
        return None
    return base, key


def fetch(day: str) -> dict | None:
    """`GET /qa?day=`。返回 None 表示**没拿到**（没配、连不上、旧版中继）。"""
    conn = _endpoint()
    if conn is None:
        return None
    base, key = conn
    try:
        r = httpx.get(f"{base}/qa", params={"day": day},
                      headers={"X-API-Key": key}, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Could not reach the phone relay for Q&A at {base}: {e}")
        return None

    if r.status_code == 401:
        print("  ⚠️  phone relay rejected the API key (401) for /qa.")
        return None
    if r.status_code == 404:
        print("  ⚠️  relay has no /qa — the server is running an older version.")
        return None
    if r.status_code != 200:
        print(f"  ⚠️  relay /qa returned {r.status_code}: {r.text[:200]}")
        return None
    data = r.json()
    if not isinstance(data, dict):
        print(f"  ⚠️  Unrecognised /qa response: {str(data)[:200]}")
        return None
    return data


def fetch_events(day: str) -> list[dict] | None:
    """当天的原始事件（复盘笔记也在这里面）。失败返回 None。"""
    conn = _endpoint()
    if conn is None:
        return None
    base, key = conn
    try:
        r = httpx.get(f"{base}/events", params={"day": day},
                      headers={"X-API-Key": key}, timeout=30.0)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Could not reach the phone relay for events at {base}: {e}")
        return None
    evs = r.json().get("events")
    return evs if isinstance(evs, list) else None


def fetch_previous_recap(day: str, limit: int = 1500) -> str:
    """取 `day` 那天最后一次写下的【今日复盘】原文。没有就返回空串。"""
    evs = fetch_events(day)
    if not evs:
        return ""
    best: tuple[str, str] = ("", "")
    for e in evs:
        if not isinstance(e, dict) or e.get("domain") != "manual":
            continue
        text = str(e.get("value_text") or "")
        if not text.startswith("【今日复盘】"):
            continue
        ts = str(e.get("ts_utc") or "")
        if ts >= best[0]:
            best = (ts, text)
    return best[1][:limit]


def save_local(day: str, payload: dict) -> Path:
    """把 /qa 结果落成 `state/qa-<day>.json`，供 memory.py 离线索引。"""
    dest = STATE / f"qa-{day}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body.setdefault("day", day)
    body["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(dest)
    return dest


def load_local(day: str) -> dict | None:
    p = STATE / f"qa-{day}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch a day's Q&A from the relay")
    ap.add_argument("day")
    ap.add_argument("--save", action="store_true", help="also write state/qa-<day>.json")
    args = ap.parse_args()

    header(f"Q&A · {args.day}")
    data = fetch(args.day)
    if data is None:
        print("  ✗ could not fetch /qa")
        return 1

    qa = data.get("qa") or []
    print(f"  OK  {len(qa)} answered question(s)")
    for x in qa:
        q = str(x.get("text") or "(题号不在问题文件里)").replace("\n", " ")
        a = str(x.get("answer") or "")
        n = str(x.get("note") or "")
        print(f"  · [{x.get('kind') or '?'}] {q[:70]} → {a[:60]}"
              + (f"（{n[:40]}）" if n else ""))
    if args.save:
        print(f"  OK  saved {save_local(args.day, data)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
