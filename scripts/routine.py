#!/usr/bin/env python3
"""把「课表」和「实际发生的事」对上，算出**偏离** → `state/deviations.jsonl`。

**为什么偏离比时长有用**

    「今天电脑用了 3.5 小时」不知道你本来该干嘛，所以它没法解释任何事。
    「周二 08:00–09:40 排的是思政，实际 08:12–08:47 在刷推，55 分钟没做该做的事」
    才是一个**可解释的观测**：它自带参照系，而且天然带一个「为什么」。

    所以课表不是标签，是**特征**：它提供预期，预期和实际的差才是信号。

**三个判定，别混成一个**

    符合  这一段时间主要花在预期的事上
    偏离  这一段时间有观测，但主要花在别的事上
    未观测 笔记本没开 / 没数据 —— **不判**

    ⚠️ 「未观测」必须单独一种。把「中午笔记本关着」判成「没上思政课」，
       等于拿「不知道」冒充「没做」—— 这个项目一路在跟这类假数据打架。

**它不产生标签，只产生候选问题**

    这里的每一行 `deviated` 都是一条**待追问**的记录：那一天那个时段，
    本该做什么、实际做了什么。真正的原因要人来答（见 feishu-bot 的追问），
    答完之后 reason 才成为标签。

    「他说因为什么」和「实际因为什么」仍然不是一回事 —— 前者是证词，
    存的时候要标清楚（见 `label_free_text.py` 顶部关于三种关系的说明）。

用法：
  python scripts/routine.py                       # 最近 14 天
  python scripts/routine.py --days 30
  python scripts/routine.py --timetable config/timetable.example.yaml   # 用模板试跑
  python scripts/routine.py --actual              # 只看「实际作息表」
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import load_config  # noqa: E402

CFG = load_config()
STATE = Path(CFG["state_dir"])

OUT_DEVIATIONS = STATE / "deviations.jsonl"
OUT_ROUTINE = STATE / "routine.json"

# 课表里的 kind → **可以接受的**实际 category。
#
# ⚠️ 是「一组」不是一个。自习时写代码既不是玩也不是上课，把 Study 和 Work
#    都算成符合预期才对；写死成 Study 的话，每次自习都在写代码都会被判偏离 ——
#    那会产出一批**假偏离**，而假偏离比没有偏离糟得多（它会让追问变成噪音）。
KIND_TO_CATEGORY: dict[str, tuple[str, ...] | None] = {
    "课": ("Study",),
    "自习": ("Study", "Work"),
    "学习": ("Study",),
    "工作": ("Work",),
    "兼职": ("Work",),
    "运动": ("Health",),
    "吃饭": ("Life",),
    "社交": ("Social",),
    "通勤": ("Transport",),
    "休息": ("Leisure",),
    "自由": None,          # 不判
}

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 判定阈值。两个都要过 —— 只看比例的话，一个 10 分钟的时段里刷了 6 分钟
# 就算「60% 偏离」，那是在凑数据。
MIN_SLOT_MIN = 30          # 短于半小时的时段不判
MIN_OTHER_MIN = 20         # 偏离至少要看得到 20 分钟
MIN_OTHER_RATIO = 0.4      # 而且至少要占时段的四成
MIN_COVERAGE = 0.25        # 观测覆盖率低于这个数 → 未观测，不判


def _hhmm(s: str) -> int | None:
    try:
        h, _, m = str(s).partition(":")
        v = int(h) * 60 + int(m or 0)
        return v if 0 <= v < 24 * 60 else None
    except (TypeError, ValueError):
        return None


def _block_range(b: dict) -> tuple[int, int] | None:
    """`{"time": "19:49-19:58"}` → (1189, 1198)。"""
    t = str(b.get("time") or "")
    a, _, z = t.partition("-")
    s, e = _hhmm(a), _hhmm(z)
    if s is None or e is None or e <= s:
        return None
    return s, e


def load_timetable(path: Path) -> dict[str, list[dict]]:
    """读课表。文件不在就返回空 —— 调用方会明说「没有课表」。"""
    try:
        import yaml
    except Exception:  # noqa: BLE001
        print("  ⚠️  没有 pyyaml，读不了课表")
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  课表解析失败（{path}）：{type(e).__name__}: {e}")
        return {}
    if not isinstance(raw, dict):
        return {}

    out: dict[str, list[dict]] = {}
    for day, slots in raw.items():
        if day not in WEEKDAYS or not isinstance(slots, list):
            continue
        clean: list[dict] = []
        for s in slots:
            if not isinstance(s, dict):
                continue
            a, z = _hhmm(s.get("start")), _hhmm(s.get("end"))
            kind = str(s.get("kind") or "").strip()
            if a is None or z is None or z <= a:
                continue
            if kind not in KIND_TO_CATEGORY:
                print(f"  ⚠️  {day} {s.get('start')}-{s.get('end')} 的 kind "
                      f"{kind!r} 不认识，跳过")
                continue
            clean.append({"start": a, "end": z,
                          "what": str(s.get("what") or "").strip(),
                          "kind": kind,
                          "place": str(s.get("place") or "").strip()})
        clean.sort(key=lambda x: x["start"])
        out[day] = clean
    return out


def _timeline(day_iso: str) -> dict | None:
    p = STATE / f"timeline-{day_iso}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def slot_observation(slot: dict, blocks: list[dict]) -> dict:
    """一个时段里到底观测到了什么。

    ⚠️ `observed` 是**这一段时间里被记录到的分钟数**，不是时段长度。笔记本
       关着的时候这个数会很小 —— 那是「没观测」，不是「没做事」。
    """
    total = slot["end"] - slot["start"]
    want = KIND_TO_CATEGORY.get(slot["kind"])
    observed = adhered = 0.0
    others: Counter = Counter()
    apps: Counter = Counter()

    for b in blocks:
        r = _block_range(b)
        if r is None:
            continue
        bs, be = r
        overlap = min(be, slot["end"]) - max(bs, slot["start"])
        if overlap <= 0:
            continue
        mins = float(overlap)
        observed += mins
        cat = str(b.get("category") or "Other")
        if want and cat in want:
            adhered += mins
        else:
            others[cat] += mins
            apps[str(b.get("app") or "")] += mins

    other = observed - adhered
    coverage = observed / total if total else 0.0
    return {
        "slot_minutes": total,
        "observed_minutes": round(observed, 1),
        "adhered_minutes": round(adhered, 1),
        "other_minutes": round(other, 1),
        "coverage": round(coverage, 2),
        "top_other_category": others.most_common(1)[0][0] if others else "",
        "top_other_app": apps.most_common(1)[0][0] if apps else "",
    }


def verdict(slot: dict, obs: dict) -> str:
    if KIND_TO_CATEGORY.get(slot["kind"]) is None:
        return "free"
    if obs["slot_minutes"] < MIN_SLOT_MIN:
        return "short"
    if obs["coverage"] < MIN_COVERAGE:
        # ⚠️ 没观测到 ≠ 没做。这一条是整个脚本最要紧的分支。
        return "unobserved"
    if (obs["other_minutes"] >= MIN_OTHER_MIN
            and obs["other_minutes"] / obs["slot_minutes"] >= MIN_OTHER_RATIO):
        return "deviated"
    return "on_track"


def analyze_day(day_iso: str, timetable: dict[str, list[dict]]) -> list[dict]:
    tl = _timeline(day_iso)
    if tl is None:
        return []
    try:
        wd = WEEKDAYS[date.fromisoformat(day_iso).weekday()]
    except ValueError:
        return []
    slots = timetable.get(wd) or []
    blocks = tl.get("blocks") or []

    rows: list[dict] = []
    for slot in slots:
        obs = slot_observation(slot, blocks)
        v = verdict(slot, obs)
        rows.append({
            "day": day_iso,
            "weekday": wd,
            "start": f"{slot['start'] // 60:02d}:{slot['start'] % 60:02d}",
            "end": f"{slot['end'] // 60:02d}:{slot['end'] % 60:02d}",
            "what": slot["what"],
            "kind": slot["kind"],
            "place": slot["place"],
            "verdict": v,
            **obs,
        })
    return rows


def actual_routine(days: list[str]) -> dict:
    """实际作息表：星期几 × 小时 的众数活动。纯统计，不需要模型。"""
    buckets: dict[str, dict[int, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for day_iso in days:
        tl = _timeline(day_iso)
        if not tl:
            continue
        try:
            wd = WEEKDAYS[date.fromisoformat(day_iso).weekday()]
        except ValueError:
            continue
        for b in (tl.get("blocks") or []):
            r = _block_range(b)
            if r is None:
                continue
            cat = str(b.get("category") or "Other")
            for minute in range(r[0], r[1], 15):
                buckets[wd][minute // 60 * 60][cat] += 1

    out: dict[str, list[dict]] = {}
    for wd in WEEKDAYS:
        rows = []
        for hour in sorted(buckets.get(wd) or {}):
            top = buckets[wd][hour].most_common(1)[0]
            rows.append({"hour": f"{hour // 60:02d}:00",
                         "category": top[0], "share": round(top[1] / 4, 2)})
        out[wd] = rows
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="课表 vs 实际 → 偏离")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--timetable", default="")
    ap.add_argument("--actual", action="store_true", help="只输出实际作息表")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = datetime.now().date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(args.days)]
    days = [d for d in days if (STATE / f"timeline-{d}.json").exists()]
    days.sort()

    if args.actual:
        routine = actual_routine(days)
        print(json.dumps(routine, ensure_ascii=False, indent=1))
        return 0

    # 课表放哪：显式给了就用，否则按「仓库 config/ → vault/04-Tasks/」的顺序找。
    # ⚠️ 两个位置都找，是因为服务器上 vault 不存在（那边只有 config），
    #    而笔记本上课表更可能躺在 Obsidian 里。
    if args.timetable:
        candidates = [Path(args.timetable)]
    else:
        candidates = [Path("config/timetable.yaml")]
        vault = CFG.get("vault_path")
        if vault and "REPLACE" not in str(vault):
            candidates.append(Path(vault) / "04-Tasks" / "timetable.yaml")
    path = next((p for p in candidates if p.exists()), candidates[0])
    timetable = load_timetable(path)
    if not timetable:
        print(f"  没有可用课表（找的是 {path}）。")
        print("  复制 config/timetable.example.yaml 成 config/timetable.yaml 再改。")
        return 0

    rows: list[dict] = []
    for d in days:
        rows += analyze_day(d, timetable)

    counts = Counter(r["verdict"] for r in rows)
    print(f"  课表时段 {len(rows)} 个（{len(days)} 天）")
    print("  " + " · ".join(f"{k} {v}" for k, v in counts.most_common()))

    devs = [r for r in rows if r["verdict"] == "deviated"]
    for r in devs[:12]:
        print(f"    {r['day']} {r['weekday']} {r['start']}-{r['end']} "
              f"{r['what']} → {r['top_other_category']}"
              f"({r['top_other_app']}) {r['other_minutes']:.0f}分"
              f" / 覆盖 {r['coverage']:.0%}")

    if args.dry_run:
        print("  --dry-run，不写盘")
        return 0

    STATE.mkdir(parents=True, exist_ok=True)
    tmp = OUT_DEVIATIONS.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    tmp.replace(OUT_DEVIATIONS)
    OUT_ROUTINE.write_text(
        json.dumps({"days": days, "actual": actual_routine(days)},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  → {OUT_DEVIATIONS}（{len(rows)} 行）")
    print(f"  → {OUT_ROUTINE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
