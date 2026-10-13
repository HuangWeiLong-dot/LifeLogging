"""冒烟测试 —— 不依赖任何真实采集源，验证整条链路的代码是通的。

装完依赖后先跑这个。全绿再去接真实数据源，能省掉一大半排障时间。

用法：
  .venv/Scripts/python scripts/smoke_test.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# 本脚本在 import common 之前就要打印，所以这里单独修一次编码
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = 0, 0


def check(name: str, fn) -> None:
    global PASS, FAIL
    try:
        fn()
        print(f"  ✅ {name}")
        PASS += 1
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ {name}\n       {type(e).__name__}: {e}")
        FAIL += 1


print("=" * 64)
print("LifeLogging smoke test")
print("=" * 64)

# ── 1. 配置 ────────────────────────────────────────────────────────
print("\n[1] Config loading")
CFG = None


def t_config():
    global CFG
    from common import load_config

    CFG = load_config()
    assert "vault_path" in CFG, "config.yaml is missing vault_path"
    assert CFG["categories"], "categories must not be empty"
    assert CFG["timezone"], "timezone is missing"
    ZoneInfo(CFG["timezone"])  # 时区名必须合法


check("config.yaml parses and has all required fields", t_config)

if CFG is None:
    print("\nFAIL config unreadable; the rest of the tests are meaningless. Fix config/config.yaml.")
    sys.exit(1)

# ── 2. 日期与时区 ──────────────────────────────────────────────────
print("\n[2] Dates and timezones")


def t_day():
    from common import Day

    tz = ZoneInfo(CFG["timezone"])
    d = Day.parse("2026-10-13", tz)
    assert d.iso == "2026-10-13"
    s, e = d.utc_range_iso()
    assert s.startswith("2026-10-1"), s
    assert s.endswith("Z") and e.endswith("Z"), "must be UTC ISO format"
    # 东八区的一天，UTC 起点应该落在前一天
    assert s < "2026-10-13T00", f"timezone conversion is wrong: {s}"
    today = Day.parse("today", tz)
    assert today.d == datetime.now(tz).date()


check("Day parsing + local-day to UTC range", t_day)

# ── 3. 事件表 ──────────────────────────────────────────────────────
print("\n[3] Events table (middleware)")
DB_OK = False


def t_db():
    global DB_OK
    from common import Day
    from middleware import events_api as api

    with tempfile.TemporaryDirectory() as td:
        api.DB_PATH = Path(td) / "test.db"
        api.init_db()

        from middleware.events_api import BatchIn, EventIn, _insert_many

        day = Day.parse("2026-10-13", ZoneInfo(CFG["timezone"]))
        batch = [
            EventIn(
                ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                domain="screen", metric="app_usage",
                value_num=3600.0, value_text="Code.exe", source="test",
            ),
            EventIn(
                ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                domain="screen", metric="app_usage",
                value_num=1800.0, value_text="chrome.exe", source="test",
            ),
            EventIn(
                ts_utc=day.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                domain="health", metric="sleep_hours",
                value_num=7.5, source="test",
            ),
        ]
        n = _insert_many(batch)
        assert n == 3, f"should insert 3 rows, actually {n}"

        # 幂等：再写一遍不该增加
        n2 = _insert_many(batch)
        assert n2 == 0, f"a duplicate insert should be 0 rows, actually {n2} (is the unique index working?)"

        # 查询
        r = api.get_events(domain="screen", day="2026-10-13", limit=100)
        assert r["count"] == 2, f"screen should be 2 rows, actually {r['count']}"

        # 聚合
        agg = api.aggregate(domain="screen", metric="app_usage", day="2026-10-13")
        assert agg["total"] == 5400.0, f"total should be 5400 seconds, actually {agg['total']}"
        assert agg["groups"][0]["k"] == "Code.exe", "should group by value_text, descending"

        # 健康
        h = api.get_events(domain="health", day="2026-10-13")
        assert h["events"][0]["value_num"] == 7.5

        # 删除（重跑某天时用）
        d = api.delete_day("2026-10-13", source="test")
        assert d["deleted"] == 3, f"should delete 3 rows, actually {d['deleted']}"

        # ── 时间块（可视化的数据源）──
        from middleware.events_api import BlockIn, BlocksIn

        bl = BlocksIn(blocks=[
            BlockIn(day_local="2026-10-13", start_utc="2026-10-13T01:00:00Z",
                    end_utc="2026-10-13T02:00:00Z", hours=1.0,
                    category="Work", app="Code.exe", blackhole=False),
            BlockIn(day_local="2026-10-13", start_utc="2026-10-13T02:00:00Z",
                    end_utc="2026-10-13T02:30:00Z", hours=0.5,
                    category="Leisure", app="chrome.exe", blackhole=True),
        ])
        r = api.post_blocks(bl)
        assert r["inserted"] == 2, f"should write 2 blocks, got {r}"

        # 关键：重跑同一天不能翻倍（先按天删旧再插）
        r2 = api.post_blocks(bl)
        assert r2["inserted"] == 2 and r2["deleted"] == 2, \
            f"a re-run should replace, not accumulate; got {r2}"

        daily = api.blocks_daily(frm="2026-10-13", to="2026-10-13")
        assert daily["count"] == 2, f"should have 2 day x category groups, got {daily['count']}"

        work = [x for x in daily["rows"] if x["category"] == "Work"]
        assert work and work[0]["hours"] == 1.0, work
        sinks = sum(x["sinks"] for x in daily["rows"])
        assert sinks == 1, f"should have exactly 1 time-sink block, got {sinks}"

        # 删掉整天时，blocks 也要一起清 —— 否则会留下孤儿时间块
        api.delete_day("2026-10-13")
        assert api.blocks_daily(frm="2026-10-13", to="2026-10-13")["count"] == 0, \
            "deleting a whole day must leave no orphan blocks"

        DB_OK = True


check("create / insert / idempotent dedup / query / aggregate / delete", t_db)

# ── 4. 成本计算 ────────────────────────────────────────────────────
print("\n[4] Cost calculation")


def t_cost():
    from datetime import datetime, timezone

    from common import Usage, compute_cost, is_peak

    model = CFG["ai"]["deepseek_model"]
    pricing = CFG["pricing"]

    # 峰谷判定。2026-10-13 是周二，2026-10-17 是周六。
    off = datetime(2026, 10, 13, 19, 0, tzinfo=timezone.utc)      # 北京 03:00 周二
    pk = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)        # 北京 11:00 周二
    wk = datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)        # 北京 11:00 周六

    assert not is_peak(off), "Beijing small hours should be off-peak"
    assert is_peak(pk), "a weekday at 11:00 should be peak"
    assert not is_peak(wk), "weekends should be off-peak all day"

    # 峰谷价差必须真的是两倍
    u = Usage(cache_miss=1_000_000)
    c_off = compute_cost(u, model, pricing, off)["cost"]
    c_pk = compute_cost(u, model, pricing, pk)["cost"]
    assert abs(c_off - pricing[model]["cache_miss"]["off_peak"]) < 1e-6, c_off
    assert abs(c_pk - pricing[model]["cache_miss"]["peak"]) < 1e-6, c_pk
    assert abs(c_pk / c_off - 2.0) < 1e-6, f"peak should be exactly 2x off-peak, got {c_pk/c_off}"

    # 缓存命中必须远便宜于未命中 —— 这是 DeepSeek 最大的成本杠杆
    h = compute_cost(Usage(cache_hit=1_000_000), model, pricing, off)["cost"]
    assert h < c_off / 10, f"cache hit {h} is not significantly below a miss of {c_off}; the price table is probably wrong"

    # 输出 token 单独计价
    o = compute_cost(Usage(output=1_000_000), model, pricing, off)["cost"]
    assert abs(o - pricing[model]["output"]["off_peak"]) < 1e-6

    # 币种要跟着价格表走，不能被硬编码成 USD
    r = compute_cost(u, model, pricing, off)
    assert r["currency"] == pricing[model]["currency"], "currency did not follow the price table"
    assert r["peak"] is False and compute_cost(u, model, pricing, pk)["peak"] is True

    # 未知模型不该崩，但必须打标记 —— 悄悄按 0 计费会让预算控制失效
    r3 = compute_cost(u, "no-such-model", pricing, off)
    assert r3["cost"] == 0.0
    assert r3.get("unknown_pricing") == 1.0, "an unknown model must be flagged"


check("peak detection / cache-hit vs miss / currency / unknown-model fallback", t_cost)

# ── 5. 台账 ────────────────────────────────────────────────────────
print("\n[5] Cost ledger")


def t_ledger():
    from datetime import date

    from common import CostLedger

    with tempfile.TemporaryDirectory() as td:
        led = CostLedger(Path(td) / "ledger.jsonl")
        tz = ZoneInfo(CFG["timezone"])
        today = datetime.now(tz).date()
        led.append({"day": today.isoformat(), "cost": 0.25, "stage": "test"})
        led.append({"day": today.isoformat(), "cost": 0.50, "stage": "test"})
        led.append({"day": "2020-01-01", "cost": 9.99, "stage": "old"})
        assert led.spend_on(today, tz) == 0.75, f"got {led.spend_on(today, tz)}"
        assert led.currency() == "USD", "should fall back to USD when currency is absent"

        # 半截行（进程被杀）不该导致解析崩溃
        with (Path(td) / "ledger.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"day": "broken"\n')
        assert len(led.entries()) == 3, "a truncated line should be skipped, not raise"


check("append / per-day totals / tolerate truncated line", t_ledger)


def t_chain():
    """台账 hash 链 —— 这是全系统唯一碰钱的数据，必须能发现被改过。"""
    import hashlib
    import json

    from common import GENESIS, CostLedger

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "l.jsonl"
        led = CostLedger(p)
        led.append({"day": "2026-10-13", "cost": 0.10})
        led.append({"day": "2026-10-13", "cost": 0.20})

        lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert len(lines) == 2
        assert all("chain" in e for e in lines), "append must write the chain field"

        def verify(rows: list[dict]) -> bool:
            prev = GENESIS
            for e in rows:
                e = dict(e)
                stored = e.pop("chain", None)
                payload = json.dumps(e, ensure_ascii=False, sort_keys=True)
                if stored != hashlib.sha256((prev + payload).encode()).hexdigest():
                    return False
                prev = stored
            return True

        assert verify(lines), "a normally written ledger should verify"

        # 篡改金额 —— 必须被检出，否则「防篡改」是句空话
        tampered = [dict(lines[0]), dict(lines[1])]
        tampered[0]["cost"] = 999.0
        assert not verify(tampered), "editing an amount went undetected -- the chain is fake"

        # 丢一行 —— 同样必须被检出
        assert not verify([dict(lines[1])]), "dropping the first row went undetected"


check("ledger hash chain detects edits and dropped rows", t_chain)

# ── 6. 日记正文校验 ────────────────────────────────────────────────
print("\n[6] Journal body parsing (AI output contract)")


def t_parse():
    from generate_journal import BLOCK_RE, validate_body

    good = """## Time Log

- [time:: 08:00-09:30] [duration:: 1.5h] [category:: Work] [blackhole:: false] Fixed form validation on the login page
- [time:: 09:30-12:00] [duration:: 2.5h] [category:: Work] [blackhole:: false] Wrote the API documentation
- [time:: 12:00-13:00] [duration:: 1h] [category:: Life] [blackhole:: false] Lunch
- [time:: 13:00-14:30] [duration:: 1.5h] [category:: Leisure] [blackhole:: true] Aimless feed scrolling
- [time:: 14:30-18:00] [duration:: 3.5h] [category:: Work] [blackhole:: false] Integration debugging
- [time:: 18:00-18:30] [duration:: 0.5h] [category:: Transport] [blackhole:: false] Commute home

## Review

### Time Sinks
- 13:00-14:30 scrolling, retained nothing

### What Went Well
- 4 hours of unbroken deep work in the morning

### Adjustments for Tomorrow
- Leave the phone in another room 13:00-14:30
"""

    blocks = BLOCK_RE.findall(good)
    assert len(blocks) == 6, f"should parse 6 blocks, got {len(blocks)}"

    total, warns = validate_body(good, expected_hours=10.5)
    assert abs(total - 10.5) < 0.01, f"total should be 10.5h, got {total}"
    # 6 块、分类合法、总时长对得上、有 sink —— 不该有告警
    assert not warns, f"should have no warnings: {warns}"

    # 坏输出：分类越界 + 块太少
    bad = """## Time Log

- [time:: 08:00-12:00] [duration:: 4h] [category:: Slacking] [blackhole:: false] no idea what happened
"""
    _, w2 = validate_body(bad, expected_hours=8.0)
    assert any("Non-standard categories" in x for x in w2), f"should flag an out-of-scheme category: {w2}"
    assert any("over-merged" in x for x in w2), f"should flag too few blocks: {w2}"


check("valid output is clean / bad category and over-merging are caught", t_parse)

# ── 7. 归一化规则 ──────────────────────────────────────────────────
print("\n[7] Normalize rule table")


def t_rules():
    from normalize import classify

    cases = [
        ("Code.exe", "main.py - LifeLogging", "Work"),
        ("chrome.exe", "GitHub - Pull Request", "Work"),
        ("Anki", "复习卡片", "Study"),
        ("Steam", "Library", "Leisure"),
        ("explorer.exe", "Downloads", "Life"),
        ("LockApp", "", "__drop__"),
    ]
    for app, title, want in cases:
        got, _ = classify(app, title)
        assert got == want, f"{app!r} expected {want}，got {got}"

    # 浏览器必须走标题判断，不能只看应用名
    browser_cases = [
        ("msedge.exe", "Pull request #12 · GitHub", "Work"),
        ("chrome.exe", "Stack Overflow - How to fix", "Work"),
        ("msedge.exe", "arxiv.org - 论文摘要", "Study"),
        ("chrome.exe", "B站 - 哔哩哔哩", "Leisure"),
        ("msedge.exe", "淘宝 - 购物车", "Life"),
    ]
    for app, title, want in browser_cases:
        got, conf = classify(app, title)
        assert got == want, f"{app!r}/{title!r} expected {want}，got {got}（conf={conf}）"

    # 浏览器但标题没线索 → 必须标 ambiguous 交给 AI，而不是猜
    got, conf = classify("msedge.exe", "New Tab")
    assert conf == "ambiguous", f"a browser with no clues should be ambiguous, got {conf}"
    assert got == "Other", f"got {got}"

    # 浏览器里的 bilibili 走标题判断，直接定 Leisure（不再走 ambiguous 分支）
    got, conf = classify("chrome.exe", "bilibili 首页")
    assert (got, conf) == ("Leisure", "rule"), f"got ({got}, {conf})"

    # 非浏览器应用命中歧义关键词时，才走 ambiguous 分支
    got, conf = classify("WeChat.exe", "文件传输助手")
    assert conf == "ambiguous", f"native WeChat should be ambiguous, got {conf}"

    # 规则完全认不出来的应用 → default（会进 unmatched 列表提醒你补规则）
    got, conf = classify("SomeRandomThing", "whatever")
    assert got == "Other" and conf == "default"

    # 浏览器不该污染 unmatched 列表
    from normalize import BROWSERS
    for b in BROWSERS:
        _, c = classify(b, "随便什么标题")
        assert c != "default", f"{b} fell to default, which pollutes the unmatched list"


check("rule matching / browser title logic / ambiguity flag / fallback", t_rules)


# ── 8. 浏览器 URL 贴合 ─────────────────────────────────────────────
print("\n[8] Browser URL attachment (aw-watcher-web)")


def t_web():
    from datetime import datetime, timedelta, timezone as _tz

    from normalize import attach_web_urls

    base = datetime(2026, 10, 13, 1, 0, 0, tzinfo=_tz.utc)

    slices = [
        # 浏览器，web 记录**早** 5 秒开始 —— 严格覆盖成立
        {"ts": base, "dur": 300, "app": "msedge.exe", "title": "Microsoft Edge"},
        # 非浏览器，不该被贴 URL
        {"ts": base + timedelta(minutes=5), "dur": 300, "app": "Code.exe", "title": "main.py"},
        # 浏览器，web 记录**晚** 3 秒开始 —— 严格覆盖不成立，但必须仍能贴上
        {"ts": base + timedelta(minutes=10), "dur": 300, "app": "msedge.exe", "title": "Edge"},
    ]
    web = [
        {"ts": base - timedelta(seconds=5), "end": base + timedelta(seconds=295),
         "url": "https://github.com/acme/pull/12", "title": "PR #12"},
        {"ts": base + timedelta(minutes=10, seconds=3), "end": base + timedelta(minutes=15),
         "url": "https://www.bilibili.com/video/BV1", "title": "某个视频"},
    ]

    n = attach_web_urls(slices, web)
    assert n == 2, f"only 2 browser blocks should get URLs, got {n}"

    assert slices[0]["url"] == "https://github.com/acme/pull/12", slices[0]
    assert slices[0]["title"] == "PR #12", "the web title should override the window title"

    assert "url" not in slices[1], "non-browser blocks must not get a URL"

    # 这是关键的一条：晚 3 秒开始的 web 记录仍必须被贴上。
    # 早期实现要求「严格覆盖开始时刻」，这种情况下会一条都贴不上且不报错。
    assert slices[2].get("url") == "https://www.bilibili.com/video/BV1", \
        f"the record offset by 3s was not attached: {slices[2]}"

    # 贴上 URL 后，分类必须能利用它（github → Work，bilibili → Leisure）
    from normalize import classify
    c1, _ = classify("msedge.exe", f"{slices[0]['title']} {slices[0]['url']}")
    c2, _ = classify("msedge.exe", f"{slices[2]['title']} {slices[2]['url']}")
    assert c1 == "Work", f"a github URL should be Work, got {c1}"
    assert c2 == "Leisure", f"a bilibili URL should be Leisure, got {c2}"

    # 没有 web 数据时不能崩，也不该给任何块加 url。
    # 注意必须构造**全新**的切片 —— 上面那个 slices 已经被改过了，
    # 拷贝它会连 url 键一起拷过来，测的就不是同一件事了。
    fresh = [
        {"ts": base, "dur": 300, "app": "msedge.exe", "title": "Edge"},
        {"ts": base + timedelta(minutes=10), "dur": 300, "app": "msedge.exe", "title": "Edge"},
    ]
    assert attach_web_urls(fresh, []) == 0, "empty web data should return 0"
    assert all("url" not in x for x in fresh), "empty web data must not add a url to any block"


check("URL attach / timestamp offset tolerance / no browser pollution / empty-data safety", t_web)


# ── 9. Dawarich 轨迹点解析 ────────────────────────────────────────
print("\n[9] Dawarich track point parsing (real field shapes)")


def t_phone_points():
    """字段名和类型都是实测确认的，这里锁住行为防止回归。

    最关键的：`latitude` / `longitude` 在 API 里是**字符串**（"39.9042"）。
    不做 float() 转换的话，算范围会变成字符串比较，结果静默错误。
    """
    import collect_phone
    from collect_phone import collect_points_summary
    from common import Day

    day = Day.parse("2026-10-13", ZoneInfo(CFG["timezone"]))

    fake = [
        {"latitude": "39.9042", "longitude": "116.4074", "timestamp": 1791894173,
         "city": "Beijing", "accuracy": 10, "battery": 80},
        {"latitude": "39.9050", "longitude": "116.5100", "timestamp": 1791894233,
         "city": "Beijing", "accuracy": 12},
    ]

    orig = collect_phone._get
    collect_phone._get = lambda *a, **k: fake
    try:
        evs = collect_points_summary(day)
    finally:
        collect_phone._get = orig

    assert len(evs) == 1, f"should summarise into 1 event, got {len(evs)}"
    e = evs[0]
    assert e.domain == "location" and e.metric == "tracked_points"
    assert e.value_num == 2, f"point count should be 2, got {e.value_num}"
    assert e.value_text == "Beijing", f"main place should be Beijing, got {e.value_text}"

    # 字符串坐标必须被转成数字 —— 否则跨度会算成荒谬的值
    assert e.meta["span_km"] > 0, f"span not computed (string coords not converted?): {e.meta}"
    assert abs(e.meta["lat_center"] - 39.9046) < 0.001, e.meta
    assert abs(e.meta["lon_center"] - 116.4587) < 0.001, e.meta
    # 经度跨约 0.1 度 ≈ 11km
    assert 8 < e.meta["span_km"] < 15, f"span computed wrongly: {e.meta['span_km']}"

    # 空数据不能崩
    collect_phone._get = lambda *a, **k: []
    try:
        assert collect_points_summary(day) == []
    finally:
        collect_phone._get = orig

    # 坐标字段缺失 / 是非数字字符串 → 跳过该点，不抛异常
    collect_phone._get = lambda *a, **k: [
        {"latitude": "not-a-number", "longitude": "116.4", "timestamp": 1},
        {"latitude": "39.9", "longitude": "116.4", "timestamp": 2, "city": "X"},
    ]
    try:
        evs2 = collect_points_summary(day)
        assert len(evs2) == 1 and evs2[0].value_num == 1, \
            f"a bad point should be skipped, not crash; got {evs2}"
    finally:
        collect_phone._get = orig


check("string-coord conversion / summary / empty data / bad-point skip", t_phone_points)

# ── 8. 目录结构 ────────────────────────────────────────────────────
print("\n[10] Vault structure")


def t_vault():
    vault = Path(CFG["vault_path"])
    assert vault.exists(), f"vault does not exist: {vault}"
    for d in ["01-Location", "02-Phone", "03-Time-Log", "04-Tasks",
              "05-Journal", "06-AI-Sessions", "07-Reviews", "99-Templates"]:
        assert (vault / d).exists(), f"missing directory: {d}"
    for t in ["Journal-Template.md", "Panel-Today.md", "Panel-Week.md",
              "Panel-Month.md", "Panel-Time-Sinks.md"]:
        assert (vault / "99-Templates" / t).exists(), f"missing template: {t}"


check("vault has all 8 folders plus templates", t_vault)

# ── 汇总 ───────────────────────────────────────────────────────────
print("\n" + "=" * 64)
print(f"passed {PASS} · failed {FAIL}")
print("=" * 64)

if FAIL:
    print("\nThere are failures. Fix them and re-run before wiring up real sources.")
    sys.exit(1)

print("""
All green. Next:

  1. Make sure the resident service is running
       python -m middleware.events_api

  2. Wire up the first real source (ActivityWatch is the lightest)
       winget install ActivityWatch
       python scripts/collect_desktop.py --date today
       python scripts/normalize.py --date today
       type state\\timeline-<date>.md

  3. Take a look, then set up the AI
       setx DEEPSEEK_API_KEY "sk-..."
       python scripts/generate_journal.py --date today --dry-run

  Step-by-step walkthrough: docs/分阶段落地.md
""")
