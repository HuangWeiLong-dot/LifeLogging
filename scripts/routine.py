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

# ── 偏离追问 ────────────────────────────────────────────────────────
#
# 判出偏离只是「发现」，不产生标签。真正有用的是那句「为什么」——
# 而理由必须是**可数**的几类，自由文本进不了回归。
DEVIATION_KIND = "deviation"

REASON_OPTIONS: tuple[tuple[str, str], ...] = (
    ("计划变了", "plan_changed"),
    ("主动选择不做", "chose_not_to"),
    ("被打断", "interrupted"),
    ("身体原因", "body"),
    ("环境 / 工具", "environment"),
    ("记错 / 忘了", "mistake"),
    ("本来就不需要", "not_applicable"),
)
LABEL_TO_CLASS = {lab: cls for lab, cls in REASON_OPTIONS}

# 一天最多追问几段偏离。多了一晚上变成审问，而且人只会开始乱点。
MAX_DEVIATION_QS = 2

# 人自己说的活动，按天索引。main() 里加载一次，analyze_day 直接读。
_EVENTS: dict[str, list[dict]] = {}

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


def load_timetable(path: Path) -> tuple[date | None, dict[str, list[dict]]]:
    """读课表 → (学期第 1 周的周一, {星期: [时段]})。读不了就返回 (None, {})。"""
    try:
        import yaml
    except Exception:  # noqa: BLE001
        print("  ⚠️  没有 pyyaml，读不了课表")
        return None, {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        return None, {}
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  课表解析失败（{path}）：{type(e).__name__}: {e}")
        return None, {}
    if not isinstance(raw, dict):
        return None, {}

    start: date | None = None
    if raw.get("semester_start"):
        try:
            start = date.fromisoformat(str(raw["semester_start"]))
        except ValueError:
            print(f"  ⚠️  semester_start 格式不对：{raw.get('semester_start')!r}")

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
            weeks = s.get("weeks")
            if (isinstance(weeks, list) and len(weeks) == 2
                    and all(isinstance(w, int) for w in weeks)):
                weeks = (weeks[0], weeks[1])
            else:
                weeks = None
            clean.append({"start": a, "end": z,
                          "what": str(s.get("what") or "").strip(),
                          "kind": kind,
                          "place": str(s.get("place") or "").strip(),
                          "weeks": weeks})
        clean.sort(key=lambda x: x["start"])
        out[day] = clean
    return start, out


def week_of(day: date, semester_start: date | None) -> int | None:
    """这一天的周次（第 1 周起）。没有学期开始日就返回 None。"""
    if semester_start is None:
        return None
    return (day - semester_start).days // 7 + 1


def slot_active(slot: dict, week: int | None) -> bool:
    """这一周这个时段开不开。

    ⚠️ 不判周次的话，第 11 周才开的创新创业教育会在前 10 周被判成
       「该上课却在干别的」—— 那是最冤的一类假偏离。
    """
    w = slot.get("weeks")
    if w is None or week is None:
        return True
    return w[0] <= week <= w[1]


def _timeline(day_iso: str) -> dict | None:
    p = STATE / f"timeline-{day_iso}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _events_by_day() -> dict[str, list[dict]]:
    """`state/events.jsonl` → 按天分组。这是**人自己说**的活动。"""
    out: dict[str, list[dict]] = defaultdict(list)
    p = STATE / "events.jsonl"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(r, dict):
            continue
        day = str(r.get("day") or "")
        a, z = _hhmm(r.get("start")), _hhmm(r.get("end"))
        if not day or a is None or z is None or z <= a:
            continue
        out[day].append({**r, "_a": a, "_z": z})
    return out


def event_observation(slot: dict, events: list[dict]) -> dict:
    """用**他自己说的**活动算同一个时段。

    ⚠️ 这是第二条观测路径，不是补丁。笔记本合着的时候（在上课、在通勤、
       在睡觉）屏幕上一个字都没有，但他在每小时问答里写得清清楚楚：
       「上英语课」「在图书馆坐了一会儿之后去跑了个步」。
       不接这条路径的话，课表时段永远判「未观测」，偏离检测等于没做。
    """
    spans = []
    for e in events:
        a = max(e["_a"], slot["start"])
        z = min(e["_z"], slot["end"])
        if z > a:
            spans.append((a, z, str(e.get("category") or "Other"),
                          str(e.get("activity") or "")))
    return _summarise(slot, spans, label_key=3)


def slot_observation(slot: dict, blocks: list[dict]) -> dict:
    """一个时段里到底观测到了什么。

    ⚠️ `observed` 是**这一段时间里被记录到的分钟数**，不是时段长度。笔记本
       关着的时候这个数会很小 —— 那是「没观测」，不是「没做事」。
    """
    spans = []
    for b in blocks:
        r = _block_range(b)
        if r is None:
            continue
        a = max(r[0], slot["start"])
        z = min(r[1], slot["end"])
        if z > a:
            spans.append((a, z, str(b.get("category") or "Other"),
                          str(b.get("app") or "")))
    return _summarise(slot, spans, label_key=3)


def _summarise(slot: dict, spans: list[tuple[int, int, str, str]],
               label_key: int = 3) -> dict:
    """把 [起,止,类别,标签] 归到**分钟**上再统计。

    ⚠️ 必须逐分钟归属，不能各自求和。事件是按**小时**报的，两个相邻小时
       会和同一个 90 分钟时段重叠 —— 求和会算出「观测 115 分钟 / 时段 90 分钟」
       这种覆盖率 127% 的数，然后「偏离 60 分钟」也是假的。
       一分钟只能属于一件事，后来的覆盖先来的（他说得越晚越具体）。
    """
    total = slot["end"] - slot["start"]
    want = KIND_TO_CATEGORY.get(slot["kind"])

    minutes: dict[int, tuple[str, str]] = {}
    for a, z, cat, label in sorted(spans, key=lambda x: x[0]):
        for m in range(a, z):
            minutes[m] = (cat, label)

    observed = len(minutes)
    adhered = 0
    others: Counter = Counter()
    labels: Counter = Counter()
    for cat, label in minutes.values():
        if want and cat in want:
            adhered += 1
        else:
            others[cat] += 1
            if label:
                labels[label] += 1

    other = observed - adhered
    return {
        "slot_minutes": total,
        "observed_minutes": float(observed),
        "adhered_minutes": float(adhered),
        "other_minutes": float(other),
        "coverage": round(observed / total if total else 0.0, 2),
        "top_other_category": others.most_common(1)[0][0] if others else "",
        "top_other_app": (labels.most_common(1)[0][0]
                          if labels else (others.most_common(1)[0][0]
                                          if others else "")),
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


def analyze_day(day_iso: str, timetable: dict[str, list[dict]],
                semester_start: date | None = None) -> list[dict]:
    tl = _timeline(day_iso)
    if tl is None:
        return []
    try:
        d = date.fromisoformat(day_iso)
        wd = WEEKDAYS[d.weekday()]
    except ValueError:
        return []
    week = week_of(d, semester_start)
    slots = [sl for sl in (timetable.get(wd) or []) if slot_active(sl, week)]
    blocks = tl.get("blocks") or []
    events = _EVENTS.get(day_iso) or []

    rows: list[dict] = []
    for slot in slots:
        # 先看屏幕，屏幕看不见再看他自己的说法。
        step = slot_observation(slot, blocks)
        srcd = "screen"
        if step["coverage"] < MIN_COVERAGE and events:
            by_self = event_observation(slot, events)
            if by_self["coverage"] >= MIN_COVERAGE:
                step, srcd = by_self, "self"
        obs = step
        v = verdict(slot, obs)
        rows.append({
            "day": day_iso,
            "weekday": wd,
            "start": f"{slot['start'] // 60:02d}:{slot['start'] % 60:02d}",
            "end": f"{slot['end'] // 60:02d}:{slot['end'] % 60:02d}",
            "what": slot["what"],
            "kind": slot["kind"],
            "place": slot["place"],
            "week": week,
            "observed_by": srcd,
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


def _qid(day: str, text: str) -> str:
    import hashlib

    return f"{day}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"


def deviation_question(row: dict) -> dict:
    """把一条偏离变成一道题。题面里必须**写清楚该做什么、实际做了什么**。

    ⚠️ 不能问「你今天为什么没按课表来」——太笼统，答不出有用的东西，
       而且被问的人会觉得被指责。写具体的时间、课名、实际活动，
       他才答得出「哦那天是因为要交作业」。
    """
    did = row.get("top_other_app") or row.get("top_other_category") or "别的事"
    text = (f"{row['start']}–{row['end']} 排的是「{row['what']}」，"
            f"实际在「{did}」，占了 {int(round(float(row['other_minutes'])))} 分钟。"
            "这一段为什么没按课表来？")
    return {
        "id": _qid(row["day"], text),
        "kind": DEVIATION_KIND,
        "format": "choice",
        "text": text,
        "evidence": ("课表 vs 实际活动；观测方式："
                     + ("你自己在问答里说的" if row.get("observed_by") == "self"
                        else "屏幕记录")),
        "ref": "",
        "options": [{"label": lab} for lab, _ in REASON_OPTIONS],
        "day": row["day"],
    }


def push_questions(day: str, questions: list[dict]) -> tuple[bool, str]:
    """推到中继。走 `/questions/live`（它现在按 id 合并，不会顶掉别的题）。"""
    cfg = (CFG.get("sources") or {}).get("phone_relay") or {}
    if not cfg.get("enabled"):
        return False, "phone_relay 没开"
    base = str(cfg.get("api_base") or "").rstrip("/")
    key = str(cfg.get("api_key") or "")
    if not base or "REPLACE" in base or "REPLACE" in key:
        return False, "phone_relay 还是占位符"
    import httpx

    try:
        r = httpx.post(f"{base}/questions/live",
                       json={"day": day, "questions": questions},
                       headers={"X-API-Key": key}, timeout=20.0)
    except Exception as e:  # noqa: BLE001
        return False, f"推不到中继：{e}"
    if r.status_code != 200:
        return False, f"中继返回 {r.status_code}：{r.text[:160]}"
    return True, "ok"


def attach_reasons(rows: list[dict]) -> int:
    """把已经答过的偏离题的理由**回填**到偏离记录上。

    ⚠️ 回填而不是另存一份：偏离和理由必须是同一条记录的两个字段。
       分成两份文件的话，以后一 join 就会出现「有偏离没理由」和
       「有理由没偏离」两种孤儿行，而且没人会发现。
    """
    answers: dict[str, str] = {}
    for path in sorted(STATE.glob("qa-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for x in (payload.get("qa") or []):
            if not isinstance(x, dict):
                continue
            qid = str(x.get("id") or "")
            if qid:
                answers[qid] = str(x.get("answer") or "").strip()

    n = 0
    for r in rows:
        if r.get("verdict") != "deviated":
            continue
        q = deviation_question(r)
        ans = answers.get(q["id"], "")
        cls = LABEL_TO_CLASS.get(ans)
        if cls:
            r["reason_class"] = cls
            r["reason_answer"] = ans
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="课表 vs 实际 → 偏离")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--timetable", default="")
    ap.add_argument("--actual", action="store_true", help="只输出实际作息表")
    ap.add_argument("--push", action="store_true",
                    help="把今天的偏离推成追问题到中继")
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
    semester_start, timetable = load_timetable(path)
    if not timetable:
        print(f"  没有可用课表（找的是 {path}）。")
        print("  复制 config/timetable.example.yaml 成 config/timetable.yaml 再改。")
        return 0

    global _EVENTS
    _EVENTS = _events_by_day()
    rows: list[dict] = []
    for d in days:
        rows += analyze_day(d, timetable, semester_start)
    if semester_start is None:
        print("  ⚠️  课表里没有 semester_start，周次过滤关掉了（第 N 周才开的课会被误判）")

    counts = Counter(r["verdict"] for r in rows)
    print(f"  课表时段 {len(rows)} 个（{len(days)} 天）")
    print("  " + " · ".join(f"{k} {v}" for k, v in counts.most_common()))

    devs = [r for r in rows if r["verdict"] == "deviated"]
    for r in devs[:12]:
        print(f"    {r['day']} {r['weekday']} {r['start']}-{r['end']} "
              f"{r['what']} → {r['top_other_category']}"
              f"({r['top_other_app']}) {r['other_minutes']:.0f}分"
              f" / 覆盖 {r['coverage']:.0%}")

    # 先回填已经答过的理由 —— 必须在写盘之前，否则写出去的还是空的。
    filled = attach_reasons(rows)
    if filled:
        print(f"  回填了 {filled} 条偏离理由")

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

    if args.push:
        today = datetime.now().date().isoformat()
        todo = [r for r in rows
                if r["day"] == today and r["verdict"] == "deviated"
                and not r.get("reason_class")][:MAX_DEVIATION_QS]
        if not todo:
            print("  今天没有待追问的偏离")
        else:
            qs = [deviation_question(r) for r in todo]
            ok, detail = push_questions(today, qs)
            print(f"  {'OK' if ok else '⚠️ '} 偏离追问 {len(qs)} 条 → {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
