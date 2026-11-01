#!/usr/bin/env python3
"""饮食：飞书里那句大白话 → 热量和三大营养素。

**为什么走这条路**

    没有任何一个饮食 App 会把数据交给你（YAZIO / MyFitnessPal 都要登录态
    和爬虫），而且就算接了，录入负担也只是从这边挪到那边。
    真正低成本的是：你在飞书里随手发一句「吃：午饭 鸡胸肉200g 米饭一碗」，
    机器人把它存成 `manual/nutrition`，这里再用一次 LLM 解析成数字。

    一句话的成本可以忽略，而且用的是项目里已经有的成本台账和预算闸门。

**它是估算，不是测量**

    模型会按常识补份量（「一碗米饭」→ 200 g）。每一行都带 `confidence`
    和 `assumptions`，日记提示词里要写清楚这是估计值 ——
    「今天摄入 2100 kcal」和「今天走了 8000 步」不是一个量级的确定性，
    不能并排放。

**幂等**

    同一天的原文没变就**不重复调用模型**，直接复用 `state/nutrition.jsonl`
    里的结果。所以 `run_daily.ps1` 每两小时跑一次刷新也不会重复花钱。

用法：
  python scripts/parse_nutrition.py --date today
  python scripts/parse_nutrition.py --date 2026-10-20 --dry-run
  python scripts/parse_nutrition.py --date 2026-10-20 --rebuild   # 忽略缓存重算
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from common import (  # noqa: E402
    BudgetExceeded, CostLedger, Day, Event, EventsClient, LLM, TruncatedOutput,
    header, load_config, new_run_id,
)

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])
STATE = Path(CFG["state_dir"])
CACHE = STATE / "nutrition.jsonl"

SOURCE = "nutrition-llm"

# ⚠️ 这个提示词必须**完全静态**。DeepSeek 靠前缀缓存把输入价压到 1/50，
#    往里拼日期或当天的原文，缓存每天失效一次，成本翻上去。
SYSTEM_PROMPT = """You extract nutrition data from a short food log.

The log is written in Chinese or English, usually as one line per meal,
sometimes with portion sizes ("鸡胸肉 200g", "一碗米饭", "a bowl of rice").

Return ONLY a JSON object, no prose, no markdown fence:

{
  "meals": [
    {"kind": "breakfast|lunch|dinner|snack|unknown",
     "text": "the original text of this meal, verbatim",
     "calories": 650,
     "protein_g": 45,
     "carbs_g": 60,
     "fat_g": 20}
  ],
  "totals": {"calories": 2100, "protein_g": 118, "carbs_g": 215, "fat_g": 75},
  "confidence": "high|medium|low",
  "assumptions": ["rice counted as 200 g cooked", "chicken breast is skinless"]
}

Rules:
- Estimate portions from normal serving sizes when the text does not say.
  Every estimate you make MUST appear in "assumptions".
- Never add items that are not mentioned. If only one meal is logged, return
  one meal — do not invent breakfast and dinner.
- Numbers are grams and kcal. If the text mentions a quantity the food does not
  change (e.g. "2 eggs"), convert it to grams.
- If the text is not about food at all, return
  {"meals": [], "totals": null, "confidence": "low", "assumptions": []}.
- "confidence" is about the whole day: "high" only when every portion was stated.
"""


def cache_key(texts: list[str]) -> str:
    h = hashlib.sha1()
    for t in sorted(texts):
        h.update(t.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def load_cache() -> dict[tuple[str, str], dict]:
    done: dict[tuple[str, str], dict] = {}
    if not CACHE.exists():
        return done
    for line in CACHE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (str(row.get("day") or ""), str(row.get("key") or ""))
        if key[1]:
            done[key] = row
    return done


def append_cache(row: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def extract_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON 对象。

    ⚠️ 不用 `json.loads(text)` 直接解：模型经常包一层 ```json 围栏，
       或者前面多说一句「Here is the JSON」。抠首尾大括号足够，
       而且比正则宽容。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 1) if f >= 0 else None


def build_events(obj: dict, day: Day, raw_text: str) -> list[Event]:
    ts = day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    meals = obj.get("meals") if isinstance(obj.get("meals"), list) else []
    confidence = str(obj.get("confidence") or "").lower() or "low"
    assumptions = obj.get("assumptions") if isinstance(obj.get("assumptions"), list) else []

    meta = {
        "confidence": confidence,
        "assumptions": assumptions[:6],
        "raw": raw_text[:1500],
        "items": len(meals),
    }

    out: list[Event] = []
    seen_kind: dict[str, int] = {}
    for m in meals:
        if not isinstance(m, dict):
            continue
        kind = str(m.get("kind") or "unknown").lower()
        # 同一天可能有两顿都叫 snack：唯一索引里有 value_text，
        # 不区分的话第二顿会被静默丢掉。
        seen_kind[kind] = seen_kind.get(kind, 0) + 1
        label = kind if seen_kind[kind] == 1 else f"{kind} #{seen_kind[kind]}"
        out.append(Event(
            ts_utc=ts, domain="nutrition", metric="meal", value_text=label,
            value_num=_num(m.get("calories")),
            source=SOURCE,
            meta={**meta, "protein_g": _num(m.get("protein_g")),
                  "carbs_g": _num(m.get("carbs_g")), "fat_g": _num(m.get("fat_g")),
                  "text": str(m.get("text") or "")[:300]},
        ))

    totals = obj.get("totals") if isinstance(obj.get("totals"), dict) else None
    if totals:
        for metric, key in (("calories", "calories"), ("protein_g", "protein_g"),
                            ("carbs_g", "carbs_g"), ("fat_g", "fat_g")):
            v = _num(totals.get(key))
            if v is not None:
                out.append(Event(ts_utc=ts, domain="nutrition", metric=metric,
                                 value_num=v, value_text="", source=SOURCE, meta=meta))
    return out


def main() -> int:
    from common import cli_day

    ap = argparse.ArgumentParser(description="把飞书里的饮食记录解析成营养素")
    ap.add_argument("--date", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="忽略缓存，重新调用模型")
    args = ap.parse_args()

    day = Day.parse(args.date or cli_day(), TZ)
    header(f"Parse nutrition · {day.iso}")

    client = EventsClient(CFG)
    if not client.ping():
        print(f"⚠️  events API 没在跑（{client.base}）")
        return 1

    try:
        events = client.query(domain="manual", metric="nutrition",
                              start=day.start_utc, end=day.end_utc, limit=200)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  查事件失败：{e}")
        return 1

    texts = [str(e.get("value_text") or "").strip() for e in events]
    texts = [t for t in texts if t]
    if not texts:
        print("  . 今天没有饮食记录（飞书里发「吃：……」就会有一条）")
        return 0

    print(f"  · {len(texts)} 条记录")
    for t in texts:
        print(f"      {t[:80]}")

    key = cache_key(texts)
    cache = {} if args.rebuild else load_cache()
    cached = cache.get((day.iso, key))

    if cached:
        print("  . 原文没变，复用上次的解析结果（不调用模型）")
        obj = cached.get("result") or {}
    else:
        run = "\n".join(f"- {t}" for t in texts)
        ledger = CostLedger(CFG["budget"]["ledger_path"])
        llm = LLM(cfg=CFG, ledger=ledger)

        if args.dry_run:
            print("\n(--dry-run，不调用模型)")
            return 0

        try:
            raw = llm.complete(
                SYSTEM_PROMPT,
                f"Food log for one day:\n{run}\n\nReturn the JSON object.",
                stage="nutrition",
                run_id=new_run_id(),
            )
        except (BudgetExceeded, TruncatedOutput) as e:
            print(f"⚠️  {e}")
            return 1

        obj = extract_json(raw)
        if obj is None:
            print("⚠️  模型输出不是合法 JSON，这一天的饮食先不写")
            print(f"     原文前 200 字：{raw[:200]}")
            return 1

        append_cache({"day": day.iso, "key": key, "at": datetime.now(timezone.utc).isoformat(),
                      "result": obj})

    out = build_events(obj, day, " | ".join(texts))
    if not out:
        print("  . 模型没解析出任何食物（可能这条记录不是在说吃的）")
        return 0

    for e in out:
        unit = {"calories": "kcal", "protein_g": "g", "carbs_g": "g", "fat_g": "g"}.get(e.metric, "")
        print(f"    {e.metric:12s} {e.value_text:12s} {e.value_num}{unit}")

    if args.dry_run:
        print("\n(--dry-run，不写库)")
        return 0

    # 先删当天这个 source 的旧行：同一天多记了一顿，总数要跟着变，
    # 而唯一索引不含 value_num，不删就只会保留第一次的结果。
    try:
        httpx.delete(f"{client.base}/day", headers=client.headers,
                     params={"day": day.iso, "source": SOURCE}, timeout=30.0)
    except Exception:  # noqa: BLE001
        pass

    n = client.post_batch(out)
    print(f"\nOK  写入 {n} 行（source={SOURCE}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
