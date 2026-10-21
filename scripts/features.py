#!/usr/bin/env python3
"""把每天的数据压成**一行状态向量** —— `state/features.jsonl`。

**为什么要有它**

    日记、周回顾、Grafana 各自读各自的原始数据。它们都**不复用同一个数**，
    所以想回答「微信超 3 小时的那几天，第二天体力是不是更低」时，得先决定
    「微信时长」到底怎么算 —— 然后你就会写出第三份实现。

    这一层只做一件事：把**已经算好的**日汇总（`state/timeline-<day>.json`）
    拍平成固定字段。不重新推导任何数字 —— 时间块、停留、手机时长都来自
    normalize，和日记用的是同一份。

**输入 / 输出**

    输入：state/timeline-<day>.json（normalize.py 的产物）
          state/labels.jsonl     （飞书自评，collect_labels.py 的产物）
    输出：state/features.jsonl   （一天一行，全量重写、幂等）

**为什么带 `feature_version`**

    字段定义一定会变（比如「娱乐时长」从 top10 改成全量）。版本号是为了
    以后的模型能说「只用 v1 的行」—— 否则新旧定义会静默混在一起，
    学到的权重一半是行为、一半是定义变更。

**不猜**

    缺的源就是 `null` / `false`，不补 0。补 0 会让「那天没开电脑」和
    「那天电脑数据没采到」在模型眼里变成同一件事。

用法：
  python scripts/features.py                # 全量重建
  python scripts/features.py --coverage     # 看每天哪些源缺了
  python scripts/features.py --dry-run      # 只打印最新一天，不写盘
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import load_config  # noqa: E402
from normalize import PHONE_LEISURE_APPS  # noqa: E402

CFG = load_config()
STATE = Path(CFG["state_dir"])
OUT = STATE / "features.jsonl"

# ⚠️ 字段定义一改就 +1。不要「反正差不多」就沿用旧版本号。
FEATURE_VERSION = 1

# 自评维度（中文，来自 live_questions.RATED_DIMENSIONS）→ 特征里的英文键。
# ⚠️ 和 live_questions 那边**必须同步**：那边多一个打分维度，这里不加映射
#    就只是少一列（不会错）；反过来映射到不存在的维度才会错 —— 所以这里
#    只写确定会有的。sleep 等维度加进打分表后再加。
LABEL_KEYS = {
    "心情": "mood",
    "体力": "energy",
}

_BLOCK_TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})")


def _h(minutes: float | None) -> float | None:
    if minutes is None:
        return None
    return round(float(minutes) / 60.0, 2)


def _block_hour(block: dict) -> int | None:
    """时间块开始的整点。解析不出来返回 None —— **不猜**。"""
    m = _BLOCK_TIME.match(str(block.get("time") or ""))
    return int(m.group(1)) if m else None


def _desktop(tl: dict) -> dict:
    blocks = [b for b in (tl.get("blocks") or []) if isinstance(b, dict)]
    by_app: dict[str, float] = {}
    longest = 0.0
    late = 0.0
    for b in blocks:
        hours = float(b.get("hours") or 0)
        app = str(b.get("app") or "").strip()
        if app:
            by_app[app] = by_app.get(app, 0.0) + hours
        longest = max(longest, hours)
        hour = _block_hour(b)
        if hour is not None and (hour >= 23 or hour < 6):
            late += hours
    top_app = max(by_app.items(), key=lambda kv: kv[1]) if by_app else None
    return {
        "hours": round(float(tl.get("total_hours") or 0), 2),
        "by_category": {str(k): round(float(v), 2)
                        for k, v in (tl.get("by_category") or {}).items()},
        "top_app": top_app[0] if top_app else None,
        "top_app_hours": round(top_app[1], 2) if top_app else None,
        "blocks": len(blocks),
        "longest_block_hours": round(longest, 2),
        "late_hours": round(late, 2),
    }


def _phone(tl: dict) -> dict | None:
    p = tl.get("phone")
    if not isinstance(p, dict) or not p:
        return None
    top = [(str(a), float(m)) for a, m in (p.get("top_apps") or [])]
    leisure = sum(m for a, m in top if a in PHONE_LEISURE_APPS)
    return {
        "screen_hours": _h(p.get("screen_minutes")),
        "foreground_hours": _h(p.get("foreground_minutes")),
        "unlocks": p.get("unlocks"),
        "switches": p.get("app_switches"),
        "top_app": top[0][0] if top else None,
        "top_app_minutes": round(top[0][1], 1) if top else None,
        # ⚠️ 只统计 top10 里的娱乐应用。名字里带 top10 是**故意的** ——
        #    它是下界，不是「娱乐总时长」；改成全量要改 normalize 的返回。
        "leisure_top10_minutes": round(leisure, 1),
    }


def _location(tl: dict) -> dict:
    stays = [s for s in (tl.get("location") or []) if isinstance(s, dict)]
    minutes = sum(float(s.get("minutes") or 0) for s in stays)
    places = [str(s.get("place") or "").strip() for s in stays]
    places = [p for p in places if p]
    main = max(stays, key=lambda s: float(s.get("minutes") or 0), default=None)
    return {
        "stays": len(stays),
        "stayed_hours": _h(minutes),
        "main_place": str(main.get("place") or "") if main else None,
        "distinct_places": len(set(places)),
    }


def _web(tl: dict) -> dict | None:
    rows = [w for w in (tl.get("web") or []) if isinstance(w, dict)]
    if not rows:
        return None
    return {
        "minutes": round(sum(float(w.get("minutes") or 0) for w in rows), 1),
        "top_site": str(max(rows, key=lambda w: float(w.get("minutes") or 0))
                        .get("site") or "") or None,
    }


def _health(tl: dict) -> dict | None:
    h = tl.get("health")
    if not isinstance(h, dict) or not h:
        return None
    # 原样保留 normalize 给出的键（steps / heart_rate_avg / sleep_hours …），
    # 不在这一层发明新指标。
    return {str(k): v for k, v in h.items()}


def features_for(day_iso: str, tl: dict, labels: dict[str, int]) -> dict:
    """一天的原始 timeline + 自评 → 一行特征。纯函数，测试直接喂假数据。"""
    phone = _phone(tl)
    health = _health(tl)
    location = _location(tl)
    web = _web(tl)

    # 哪些源**真的**报了东西。补 0 会让「没用」和「没采到」变成同一件事。
    sources = {
        "screen": bool(tl.get("blocks")) or float(tl.get("total_hours") or 0) > 0,
        "phone": bool(phone and ((phone.get("unlocks") or 0)
                                 or phone.get("screen_hours"))),
        "health": bool(health),
        "location": bool(location["stays"]),
        "web": bool(web),
        "labels": bool(labels),
    }

    return {
        "day": day_iso,
        "feature_version": FEATURE_VERSION,
        "desktop": _desktop(tl),
        "phone": phone,
        "location": location,
        "web": web,
        "health": health,
        "track": {
            "points": (tl.get("track") or {}).get("points"),
            "span_km": (tl.get("track") or {}).get("span_km"),
        },
        "labels": dict(labels),
        "sources": sources,
    }


def load_labels() -> dict[str, dict[str, int]]:
    """`state/labels.jsonl` → `{day: {mood: 4, energy: 2}}`。

    ⚠️ 同一维度同一天有多条时取 ts 最大的（重答）。中文维度名在这里翻译成
       英文键，特征表里不出现中文 —— 以后的模型不想处理两套命名。
    """
    path = STATE / "labels.jsonl"
    latest: dict[tuple[str, str], str] = {}
    out: dict[str, dict[str, int]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        key = LABEL_KEYS.get(str(row.get("dim") or ""))
        day = str(row.get("day") or "")
        score = row.get("score")
        if key is None or not day or not isinstance(score, int):
            continue
        ts = str(row.get("ts_utc") or "")
        k = (day, key)
        if k not in latest or ts >= latest[k]:
            latest[k] = ts
            out.setdefault(day, {})[key] = score
    return out


def build() -> list[dict]:
    labels = load_labels()
    rows: list[dict] = []
    for p in sorted(STATE.glob("timeline-*.json")):
        try:
            tl = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ⚠️  {p.name} 读不了，跳过：{type(e).__name__}: {e}",
                  file=sys.stderr)
            continue
        if not isinstance(tl, dict):
            continue
        day = str(tl.get("day") or p.stem.replace("timeline-", ""))
        rows.append(features_for(day, tl, labels.get(day, {})))
    rows.sort(key=lambda r: r["day"])
    return rows


def write(rows: list[dict]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")
    tmp.replace(OUT)


def coverage(rows: list[dict]) -> None:
    """哪天的哪个源缺了。这是给「以后训练前先看一眼」用的，不是装饰。"""
    if not rows:
        print("  还没有特征。先归一化至少一天。")
        return
    cols = ("screen", "phone", "health", "location", "web", "labels")
    print(f"  {'day':<12}" + " ".join(f"{c:>8}" for c in cols) + "   desktop_h")
    missing: dict[str, list[str]] = {c: [] for c in cols}
    for r in rows:
        marks = []
        for c in cols:
            ok = bool((r.get("sources") or {}).get(c))
            marks.append(f"{'✓' if ok else '—':>8}")
            if not ok:
                missing[c].append(r["day"])
        print(f"  {r['day']:<12}" + " ".join(marks)
              + f"   {r['desktop'].get('hours')}")
    print()
    for c in cols:
        if missing[c]:
            print(f"  缺 {c}: {', '.join(missing[c])}")


def main() -> int:
    ap = argparse.ArgumentParser(description="每日状态向量")
    ap.add_argument("--coverage", action="store_true", help="只看缺口")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写盘")
    args = ap.parse_args()

    rows = build()
    if not rows:
        print("  没有可用的 timeline-*.json —— 先跑 normalize.py。")
        return 0

    if not args.dry_run:
        write(rows)
        print(f"  OK  {len(rows)} 天 → {OUT}（feature_version={FEATURE_VERSION}）")

    if args.coverage:
        # run_daily 每天调的就是这个：先写盘，再打一张「哪个源缺了」的表。
        coverage(rows)
        return 0

    last = rows[-1]
    print(json.dumps(last, ensure_ascii=False, indent=1)[:1600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
