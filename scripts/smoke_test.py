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


# ── 6b. 输出截断必须被发现 ────────────────────────────────────────
print("\n[12] Truncated output detection")


def t_truncation():
    """被 max_tokens 截断的正文**不能**被当成正常结果。

    这是实测出来的缺陷（2026-10-13 23:00 那次跑）：
      · `max_output_tokens: 8000` 是**推理 token + 正文**共用的额度
      · 那次输出整 8,000 token，其中 7,915 是推理，正文只剩几十个 token
      · 产出在第二条时间块中间断掉，但退出码是 0、水位线照常推进
        —— 于是那一天永远不会重跑，而文件看起来「有内容」

    这里锁住三件事：截断要抛异常、正常结束不能误报、被截断那次的钱仍要进台账。
    """
    from common import CostLedger, LLM, TruncatedOutput, Usage

    with tempfile.TemporaryDirectory() as td:
        ledger = CostLedger(Path(td) / "ledger.jsonl")
        llm = LLM(cfg=CFG, ledger=ledger)
        original = LLM._deepseek
        try:
            # ── 截断：必须抛 ──
            LLM._deepseek = lambda self, s, u: (
                "- [time:: 19:16-21:11] [duration:: 2h] [category:: Work] [",
                Usage(cache_hit=0, cache_miss=1741, output=8000,
                      reasoning=7915, finish_reason="length"),
            )
            raised = False
            try:
                llm.complete("sys", "user", stage="journal", run_id="t-trunc")
            except TruncatedOutput:
                raised = True
            assert raised, "被截断的输出必须抛 TruncatedOutput，不能静默通过"

            # ── 钱已经花了，台账必须记上（检查在记账之后） ──
            entries = ledger.entries()
            assert len(entries) == 1, \
                f"截断那次调用照样产生费用，必须进台账；实际 {len(entries)} 条"
            assert entries[0]["output"] == 8000, entries[0]

            # ── Anthropic 用 "max_tokens" 表示截断，也要认 ──
            LLM._deepseek = lambda self, s, u: (
                "half a body",
                Usage(cache_hit=0, cache_miss=100, output=50,
                      reasoning=10, finish_reason="max_tokens"),
            )
            raised = False
            try:
                llm.complete("sys", "user", stage="journal", run_id="t-trunc2")
            except TruncatedOutput:
                raised = True
            assert raised, 'finish_reason="max_tokens" 同样要抛'

            # ── 正常结束：不能误报 ──
            LLM._deepseek = lambda self, s, u: (
                "# body\n- [time:: 09:00-10:00] [duration:: 1h] [category:: Work] [blackhole:: false] did things",
                Usage(cache_hit=0, cache_miss=100, output=200,
                      reasoning=50, finish_reason="stop"),
            )
            body = llm.complete("sys", "user", stage="journal", run_id="t-ok")
            assert body.startswith("# body"), f"正常输出不该被改动: {body!r}"
        finally:
            LLM._deepseek = original


check("finish_reason=length 抛异常 / 成本仍入账 / 正常结束不误报", t_truncation)

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


# ── 7b. 时间块合并与折叠 ──────────────────────────────────────────
print("\n[13] Time-block merging and folding")


def t_block_merging():
    """锁住「碎片化吃掉 90% 时间」那个缺陷的修复。

    实测背景（2026-10-13）：ActivityWatch 记录了 2.34 小时窗口活动，
    但只有 **0.41 小时**进了任务表、进了日记。剩下 82% 无声消失，
    而日记模型只能靠**编造时长**填空（它写出了「19:16-21:11 1.9h」
    这种数据里根本不存在的区间）。

    两个成因，各对应下面一组断言：
      1. 规则没认出来（Other）被当成「换了一件事」，把连续活动切成碎片
      2. 碎片被 MIN_BLOCK **直接丢掉**，而不是并进相邻块
    """
    from datetime import datetime, timedelta
    import normalize as N

    tz = ZoneInfo(CFG["timezone"])
    t0 = datetime(2026, 10, 13, 19, 0, 0, tzinfo=tz)

    def sl(off: int, dur: float, app: str, title: str) -> dict:
        return {"ts": t0 + timedelta(seconds=off), "dur": dur,
                "app": app, "title": title, "url": ""}

    # Other 是「没有信息」，不是一种分类
    assert N._is_unknown("Other", "ambiguous") and N._is_unknown("Other", "default")
    assert not N._is_unknown("Work", "rule")
    assert not N._is_unknown("Leisure", "ambiguous"), "认出来了、只是判不准，不算未知"

    # 1. 一条认不出的记录不构成活动切换
    blocks, _ = N.build_blocks([
        sl(0, 60, "msedge.exe", "GitHub - Pull Request"),   # Work（规则命中）
        sl(60, 30, "msedge.exe", "New Tab"),                # Other，无线索
        sl(90, 60, "msedge.exe", "GitHub - Issue"),         # Work
    ])
    assert len(blocks) == 1, f"认不出的记录不该切断活动，得到 {len(blocks)} 块"
    assert blocks[0].category == "Work", blocks[0].category
    assert blocks[0].seconds == 150, blocks[0].seconds

    # 2. 焦点闪动（1 秒的另一个应用）不构成活动切换
    blocks, _ = N.build_blocks([
        sl(0, 300, "WindowsTerminal.exe", "terminal"),
        sl(300, 1, "explorer.exe", "Downloads"),            # 只 1 秒
        sl(301, 300, "WindowsTerminal.exe", "terminal"),
    ])
    assert len(blocks) == 1, f"1 秒的焦点闪动不该切断活动，得到 {len(blocks)} 块"
    assert blocks[0].category == "Work", blocks[0].category

    # 3. 真的换了另一件事 —— 必须切开，别把模糊合并做成「全并成一块」
    blocks, _ = N.build_blocks([
        sl(0, 300, "WindowsTerminal.exe", "terminal"),
        sl(300, 300, "Steam.exe", "Library"),               # 5 分钟 Leisure
    ])
    assert len(blocks) == 2, f"真的换了事必须切成两块，得到 {len(blocks)}"

    # 4. 短块要被**折叠**进相邻块，不是丢掉 —— 总量必须守恒
    blocks, _ = N.build_blocks([
        sl(0, 600, "WindowsTerminal.exe", "terminal"),
        sl(600, 20, "explorer.exe", "Downloads"),           # 20 秒，不够 MIN_BLOCK
        sl(620, 600, "WindowsTerminal.exe", "terminal"),
    ])
    before = sum(b.seconds for b in blocks)
    folded = N.fold_short_blocks(blocks)
    after = sum(b.seconds for b in folded)
    assert after == before, f"折叠必须守恒时间：{before}s -> {after}s"
    assert len(folded) < len(blocks), "短块应该被折进去，块数要减少"


check("unknown/blip continue the run, real switches split, folding conserves time",
      t_block_merging)


# ── 7c. 手机事件汇总 ──────────────────────────────────────────────
print("\n[14] Phone event summary")


def t_phone_events():
    """锁住手机事件的汇总：包名映射 + 前台时长推导。

    「使用最久的 App」是**推导**出来的，不是上报的：Tasker 的 App Changed
    事件只带一个时间戳，没有时长；但两条相邻切换之间的时间差，就是前一个
    应用在前台待了多久。
    """
    from datetime import datetime, timedelta
    import normalize as N

    tz = ZoneInfo(CFG["timezone"])
    t0 = datetime(2026, 10, 13, 20, 0, 0, tzinfo=tz)

    def iso(dt):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 包名 → 人话。不查表的话日记里会写一堆 com.tencent.mm
    assert N.phone_app_name("com.tencent.mm") == "WeChat"
    assert N.phone_app_name("WeChat") == "WeChat", "已经是应用名就原样用（不是包名就查不了表）"
    assert N.phone_app_name("com.example.unknown") == "com.example.unknown", "不在表里也不能丢"
    assert N.phone_app_name("") == "unknown"

    evs = [
        {"ts_utc": iso(t0), "metric": "unlock"},
        {"ts_utc": iso(t0), "metric": "app_switch", "value_text": "com.tencent.mm"},
        {"ts_utc": iso(t0 + timedelta(minutes=10)), "metric": "app_switch",
         "value_text": "com.android.chrome"},
        {"ts_utc": iso(t0 + timedelta(minutes=25)), "metric": "unlock"},
        {"ts_utc": iso(t0 + timedelta(minutes=25)), "metric": "app_switch",
         "value_text": "com.tencent.mm"},
        {"ts_utc": iso(t0 + timedelta(minutes=40)), "metric": "app_switch",
         "value_text": "com.taobao.taobao"},
    ]
    s = N.summarize_phone(evs)
    assert s["unlocks"] == 2, s
    assert s["app_switches"] == 4, s
    assert s["first_unlock"] and s["last_unlock"], s
    top = dict(s["top_apps"])
    assert top["WeChat"] == 25.0, f"微信应是 10+15 分钟，得到 {top}"
    assert top["Chrome"] == 15.0, top
    assert "Taobao" not in top, "最后一个应用没有下一跳，时长未知，不该凭空补"

    # 大间隔不算 —— 否则「昨晚最后一个应用」会把一整夜算成它的前台时长
    s2 = N.summarize_phone([
        {"ts_utc": iso(t0), "metric": "app_switch", "value_text": "com.tencent.mm"},
        {"ts_utc": iso(t0 + timedelta(hours=9)), "metric": "app_switch",
         "value_text": "com.android.chrome"},
    ])
    assert dict(s2["top_apps"]).get("WeChat") is None, \
        f"9 小时的间隔不该算成微信的前台时长: {s2['top_apps']}"

    # 桌面不是「在用的应用」，不能进排名。
    # 实测这台手机：从微信退回桌面、再从桌面点开浏览器，中间那一跳就是
    # com.miui.home。不过滤的话桌面会在「用得最多的应用」里永远名列前茅。
    s3 = N.summarize_phone([
        {"ts_utc": iso(t0), "metric": "app_switch", "value_text": "com.tencent.mm"},
        {"ts_utc": iso(t0 + timedelta(minutes=2)), "metric": "app_switch",
         "value_text": "com.miui.home"},
        {"ts_utc": iso(t0 + timedelta(minutes=3)), "metric": "app_switch",
         "value_text": "com.android.chrome"},
        {"ts_utc": iso(t0 + timedelta(minutes=5)), "metric": "app_switch",
         "value_text": "com.miui.home"},
    ])
    names3 = [n for n, _ in s3["top_apps"]]
    assert "com.miui.home" not in names3, f"桌面不该进「用得最多的应用」: {names3}"
    assert dict(s3["top_apps"]).get("WeChat") == 2.0, s3["top_apps"]
    # 跳过桌面**不影响**别人的时长：微信那段照样是 t0→t0+2
    assert dict(s3["top_apps"]).get("Chrome") == 2.0, s3["top_apps"]

    # 不在表里的包：时长照算（不能凭空丢），但要单独列出来让人补映射表 ——
    # 不列的话日记里就是一堆 com.xxx.yyy，谁也不知道该去加什么
    s4 = N.summarize_phone([
        {"ts_utc": iso(t0), "metric": "app_switch", "value_text": "com.tencent.mm"},
        {"ts_utc": iso(t0 + timedelta(minutes=4)), "metric": "app_switch",
         "value_text": "com.acme.unknownapp"},
        {"ts_utc": iso(t0 + timedelta(minutes=9)), "metric": "app_switch",
         "value_text": "com.android.chrome"},
    ])
    assert dict(s4["top_apps"]).get("WeChat") == 4.0, s4["top_apps"]
    assert dict(s4["unknown_apps"]).get("com.acme.unknownapp") == 5.0, s4["unknown_apps"]
    assert "com.android.chrome" not in dict(s4["unknown_apps"]), \
        "已经在表里的包不该被报成未知"

    # ── 屏幕事件（screen_on / screen_off）──────────────────────────
    # 配不成对的必须丢掉，不能猜 —— 猜出来的时长会污染「屏幕时间」这个数字
    assert N._screen_intervals([(t0, "screen_on")]) == [], "只有 on：终点未知，丢掉"
    assert N._screen_intervals([(t0, "screen_off")]) == [], "没有 on 的 off"
    assert N._screen_intervals([
        (t0, "screen_on"), (t0 + timedelta(minutes=1), "screen_on"),
        (t0 + timedelta(minutes=2), "screen_off"),
    ]) == [(t0 + timedelta(minutes=1), t0 + timedelta(minutes=2))], \
        "两个 on 之间没有 off：前一段算不出终点，丢掉而不是猜"

    # ⚠️ 去抖：Tasker 会连发同一种事件（实测见过 3 条 screen_off 相隔 3~4 秒）。
    #    off 连发无害；但 on 连发会把前一段窗口作废 —— 那是直接丢掉屏幕时间。
    assert N._screen_intervals([
        (t0, "screen_on"), (t0 + timedelta(seconds=3), "screen_on"),
        (t0 + timedelta(minutes=10), "screen_off"),
    ]) == [(t0, t0 + timedelta(minutes=10))], \
        "挨着连发的 on 只该认第一条，否则屏幕时间被挖掉一段"
    # 但真的「灭了又亮」不该被去抖吞掉
    assert N._screen_intervals([
        (t0, "screen_on"), (t0 + timedelta(minutes=1), "screen_off"),
        (t0 + timedelta(minutes=2), "screen_on"),
        (t0 + timedelta(minutes=5), "screen_off"),
    ]) == [(t0, t0 + timedelta(minutes=1)),
           (t0 + timedelta(minutes=2), t0 + timedelta(minutes=5))], \
        "间隔超过去抖窗口的两次亮屏是两段，不能被合并"

    # 屏幕时间 = 实测，不是推的
    scr = [
        {"ts_utc": iso(t0), "metric": "screen_on"},
        {"ts_utc": iso(t0 + timedelta(minutes=1)), "metric": "app_switch",
         "value_text": "com.tencent.mm"},
        {"ts_utc": iso(t0 + timedelta(minutes=4)), "metric": "app_switch",
         "value_text": "com.android.chrome"},
        {"ts_utc": iso(t0 + timedelta(minutes=10)), "metric": "screen_off"},
    ]
    s5 = N.summarize_phone(scr)
    assert s5["screen_minutes"] == 10.0, s5["screen_minutes"]
    top5 = dict(s5["top_apps"])
    assert top5["WeChat"] == 3.0, top5
    # ⚠️ 最后一个应用原来**测不出来**（没有下一跳），有了 screen_off 才有终点。
    #    这是加 Display 事件最实在的一处收益 —— 否则「睡前刷了半小时」整段丢账。
    assert top5["Chrome"] == 6.0, f"最后一个应用该被 screen_off 收尾: {top5}"

    # 屏幕灭着的那段不能算给任何应用
    gap = [
        {"ts_utc": iso(t0), "metric": "screen_on"},
        {"ts_utc": iso(t0 + timedelta(minutes=1)), "metric": "app_switch",
         "value_text": "com.tencent.mm"},
        {"ts_utc": iso(t0 + timedelta(minutes=3)), "metric": "screen_off"},
        {"ts_utc": iso(t0 + timedelta(hours=2)), "metric": "screen_on"},
        {"ts_utc": iso(t0 + timedelta(hours=2, minutes=1)), "metric": "app_switch",
         "value_text": "com.android.chrome"},
        {"ts_utc": iso(t0 + timedelta(hours=2, minutes=5)), "metric": "screen_off"},
    ]
    s6 = N.summarize_phone(gap)
    assert s6["screen_minutes"] == 8.0, s6["screen_minutes"]     # 3 + 5
    top6 = dict(s6["top_apps"])
    # 微信：3 分钟窗口里的 1→3（2 分钟），**加上**第二次亮屏到切走之间的 1 分钟
    # （屏幕重新亮起时前台还是它）。中间灭屏的 1 小时 57 分**不算给任何人**。
    assert top6["WeChat"] == 3.0, f"灭屏时间被算进去了: {top6}"
    assert top6["Chrome"] == 4.0, top6
    # 时长合计不该超过屏幕亮着的总时长
    assert s6["foreground_minutes"] <= s6["screen_minutes"], \
        f"应用时长合计不该超过屏幕时间：{s6}"

    # ⚠️ 屏幕事件**只对它自己覆盖的那一段**负责。
    #
    # 实测踩到过：下午 19:40 才加上 Display 两个 Profile，而应用切换从 17:05
    # 就有了 —— 于是 17:05~19:40 之间那 200 多次切换**全部被裁成 0 分钟**，
    # 「用得最多的应用」整个榜单变成一串 0，看起来像功能根本没生效。
    # 屏幕数据之前/之后的时段必须退回按解锁事件裁，不能归零。
    partial = [
        {"ts_utc": iso(t0), "metric": "app_switch", "value_text": "com.tencent.mm"},
        {"ts_utc": iso(t0 + timedelta(minutes=6)), "metric": "app_switch",
         "value_text": "com.android.chrome"},
        # 屏幕事件整整两小时后才开始
        {"ts_utc": iso(t0 + timedelta(hours=2)), "metric": "screen_on"},
        {"ts_utc": iso(t0 + timedelta(hours=2, minutes=3)), "metric": "screen_off"},
    ]
    s7 = N.summarize_phone(partial)
    assert s7["screen_minutes"] == 3.0, s7["screen_minutes"]
    assert dict(s7["top_apps"]).get("WeChat") == 6.0, \
        f"屏幕数据之前那 6 分钟被吞掉了: {s7['top_apps']}"


check("package→name mapping / inferred foreground time / long gaps ignored",
      t_phone_events)


# ── 7d. 服务器中继 ────────────────────────────────────────────────
print("\n[15] Phone relay (server side)")


def t_phone_relay():
    """锁住中继的分天算法和笔记本侧一致。

    ⚠️ 这是最容易静默出错的地方：中继按「本地日期」把事件分到
       `phone-YYYY-MM-DD.jsonl`，笔记本按 `?day=` 去拉。两边算出来的「天」
       只要差一点，那些事件就**永远拉不到** —— 不报错、不告警，就是没有。

       所以这里拿同一批时间戳跑两个实现，要求逐条相等。
    """
    import importlib.util
    import tempfile
    from middleware.events_api import _day_local

    path = (Path(__file__).resolve().parent.parent
            / "server" / "phone-relay" / "phone_relay.py")
    spec = importlib.util.spec_from_file_location("phone_relay", path)
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)

    # 本地日期边界（+08:00）：16:00Z 是第二天 00:00 本地，15:59:59Z 还是当天
    for ts in ["2026-10-13T04:00:00Z", "2026-10-13T15:59:59Z", "2026-10-13T16:00:00Z",
               "2026-10-14T00:00:00Z", "2026-12-31T16:00:00Z"]:
        assert pr.day_of(ts) == _day_local(ts), \
            f"分天算法不一致 {ts}: relay={pr.day_of(ts)} laptop={_day_local(ts)}"

    # 时间戳两种形状都收。
    # Unix 秒这一条是**手机侧能不能配得简单**的关键：Tasker 的 %TIMES 就是秒，
    # 而且能直接写进 HTTP body；只收 ISO 的话手机上得多挂一段 JavaScriptlet，
    # 那是整个配置里最容易出错的一步。
    assert pr.to_utc_iso({"ts_utc": "2026-10-14T08:00:00Z"}) == "2026-10-14T08:00:00Z"
    assert pr.to_utc_iso({"ts": 1791964800}) == "2026-10-14T08:00:00Z", "Unix 秒要能转"
    # 毫秒是「看起来对其实大 1000 倍」的经典错误 —— 必须挡住，不能猜
    assert pr.to_utc_iso({"ts": 1791964800000}) is None, "毫秒值要丢掉"
    assert pr.to_utc_iso({}) is None, "没有时间戳要丢掉"
    assert pr.to_utc_iso({"ts": "not-a-number"}) is None
    assert pr.to_utc_iso({"ts": True}) is None, "布尔值不是时间戳"

    with tempfile.TemporaryDirectory() as td:
        pr.DATA_DIR = Path(td)
        pr._seen.clear()
        evs = [
            {"ts_utc": "2026-10-13T04:00:30Z", "domain": "phone", "metric": "unlock"},
            {"ts_utc": "2026-10-13T04:01:00Z", "domain": "phone",
             "metric": "app_switch", "value_text": "com.tencent.mm"},
            # 重复上报：内存集合要挡掉
            {"ts_utc": "2026-10-13T04:01:00Z", "domain": "phone",
             "metric": "app_switch", "value_text": "com.tencent.mm"},
            # 缺时间戳：丢掉，别写坏文件
            {"domain": "phone", "metric": "unlock"},
            # 缺 metric：同上
            {"ts_utc": "2026-10-13T04:02:00Z", "domain": "phone"},
        ]
        assert pr.append_events(evs) == 2, "重复和缺字段的都不该写进去"

        back = pr.read_events("2026-10-13")
        assert len(back) == 2, back
        assert {e["metric"] for e in back} == {"unlock", "app_switch"}
        # 分到了正确的那一天
        assert pr.read_events("2026-10-14") == [], "不该分到第二天"

        # 坏行不该让整天的读取失败（断电写坏的半行）
        with (Path(td) / "phone-2026-10-13.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"ts_utc": "broken"\n')
        assert len(pr.read_events("2026-10-13")) == 2, "坏行应被跳过，而不是让整天读不出来"

    # 拉取侧：中继里的一行 → 事件表的一条
    from collect_phone_events import to_events
    from common import Day
    day = Day.parse("2026-10-13", ZoneInfo(CFG["timezone"]))
    got = to_events([
        {"ts_utc": "2026-10-13T04:01:00Z", "domain": "phone", "metric": "app_switch",
         "value_text": "com.tencent.mm", "source": "tasker"},
        {"metric": "unlock"},                     # 缺时间戳 → 丢
    ], day)
    assert len(got) == 1 and got[0].value_text == "com.tencent.mm", got
    # ⚠️ 包名要**原样**存进事件表，翻译留给 normalize 的 PHONE_APPS ——
    #    采集层存原始值、展示层做解释，否则以后改映射就得回头改历史数据
    assert got[0].source == "tasker"

    # 「连不上」必须和「今天没数据」分得开：前者返回 None，后者返回 []。
    # 都返回 [] 的话，服务器挂了在输出上会长得跟「手机今天没用过」一样。
    import collect_phone_events as cpe
    saved = cpe.CFG
    try:
        for cfg, why in [
            ({"sources": {"phone_relay": {"enabled": False}}}, "没启用"),
            ({"sources": {"phone_relay": {"enabled": True, "api_base": "http://127.0.0.1:9",
                                          "api_key": "x"}}}, "连不上"),
            ({"sources": {"phone_relay": {"enabled": True,
                                          "api_base": "http://REPLACE_WITH_X:8788",
                                          "api_key": "REPLACE_WITH_THE_RELAY_KEY"}}}, "占位值"),
        ]:
            cpe.CFG = cfg
            assert cpe.fetch_day(day) is None, f"{why} 时必须返回 None，不能返回 []"
    finally:
        cpe.CFG = saved


check("relay day-bucketing matches the laptop / dedup / bad-line tolerance",
      t_phone_relay)


# ── 7e. Gadgetbridge 表选择 ────────────────────────────────────────
print("\n[16] Gadgetbridge table selection")


def t_gadgetbridge_pick():
    """锁住「选了空表」这个坑。

    实测背景（2026-10-14，Garmin Forerunner 235 的真实导出）：
    Gadgetbridge 的库会把**所有**设备的 schema 都建出来（小米 / 华米 / Garmin…），
    没接的设备就是一张空表。而原来的排序只看「匹配到几个指标」，
    于是系统性地偏向空表：

        HUAMI_EXTENDED_ACTIVITY_SAMPLE   匹配 5 个指标但 0 行   ← 被选中
        GARMIN_ACTIVITY_SAMPLE           匹配 2 个指标但 21090 行

    结果打印「没数据，可能没戴表」—— 一个错的解释，掩盖了一个真 bug。
    """
    import sqlite3
    from middleware.ingest_gadgetbridge import find_candidate_tables

    conn = sqlite3.connect(":memory:")
    # 空表：schema 齐全、匹配指标最多、但一行都没有
    conn.execute("""CREATE TABLE HUAMI_EXTENDED_ACTIVITY_SAMPLE (
        TIMESTAMP INTEGER, STEPS INTEGER, HEART_RATE INTEGER,
        SLEEP INTEGER, DEEP_SLEEP INTEGER, REM_SLEEP INTEGER)""")
    # 有数据的表：匹配指标少，但那是真的数据
    conn.execute("""CREATE TABLE GARMIN_ACTIVITY_SAMPLE (
        TIMESTAMP INTEGER, STEPS INTEGER, HEART_RATE INTEGER)""")
    conn.executemany("INSERT INTO GARMIN_ACTIVITY_SAMPLE VALUES (?,?,?)",
                     [(1791964800 + i * 60, 100 * i, 70 + i) for i in range(5)])
    conn.commit()

    cands = find_candidate_tables(conn)
    assert cands, "应该找到候选表"
    table, _cols, mapping, rows = cands[0]
    assert table == "GARMIN_ACTIVITY_SAMPLE", \
        f"必须选有数据的表，实际选了 {table}（这就是那个 bug）"
    assert rows == 5, rows
    assert set(mapping) == {"steps", "heart_rate"}, mapping

    # 空表仍然要出现在候选里（让人看到「它存在但没数据」），只是排在后面
    names = [c[0] for c in cands]
    assert "HUAMI_EXTENDED_ACTIVITY_SAMPLE" in names, names
    assert cands[-1][3] == 0, "空表应排在最后"


check("picks the table with data, not the biggest empty schema", t_gadgetbridge_pick)


# ── 7f. 锻炼会话（跑步）──────────────────────────────────────────
print("\n[17] Workout sessions (Garmin running)")


def t_workouts():
    """锁住锻炼会话的解析。表结构照抄 Garmin Forerunner 235 的真实导出。

    ⚠️ 两个只有拿到真数据才知道的点：

      1. `BASE_ACTIVITY_SUMMARY` 的时间列叫 **START_TIME 且是毫秒**，
         不是 TIMESTAMP。按秒解释会落到 1970 年 —— 而且是**静默**落到
         错误的那一天，因为它照样是个合法时间戳。
      2. 它没有 TIMESTAMP 列，所以**不在** find_candidate_tables 的候选里。
         必须单独读，而且不能因为候选表为空就把它一起跳过。
    """
    import sqlite3
    import json as _json
    from datetime import datetime
    from common import Day
    from middleware.ingest_gadgetbridge import read_workouts, workouts_to_events

    tz = ZoneInfo(CFG["timezone"])
    day = Day.parse("2026-10-13", tz)
    start_ms = int(datetime(2026, 10, 13, 9, 13, 16, tzinfo=tz).timestamp() * 1000)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # 没有这张表时要安静返回空（非 Garmin 设备就是这样）
    assert read_workouts(conn, day) == [], "没有 BASE_ACTIVITY_SUMMARY 时应返回空"

    conn.execute("""CREATE TABLE BASE_ACTIVITY_SUMMARY (
        _id INTEGER PRIMARY KEY, NAME TEXT, START_TIME INTEGER, END_TIME INTEGER,
        ACTIVITY_KIND INTEGER, SUMMARY_DATA TEXT)""")
    summary = {
        "activeSeconds": {"unit": "seconds", "value": 1200.382},
        "distanceMeters": {"unit": "meters", "value": 4236.75},
        "averageHR": {"unit": "bpm", "value": 170},
        "averageKMPaceSeconds": {"unit": "seconds_km", "value": 283.29},
        "maximumOxygenUptake": {"unit": "unit_ml_kg_min", "value": 47.642},
        "recoveryTime": {"unit": "seconds", "value": 155520},
    }
    conn.execute("INSERT INTO BASE_ACTIVITY_SUMMARY VALUES (1,'跑步',?,?,16,?)",
                 (start_ms, start_ms + 1200000, _json.dumps(summary)))
    # 同一次记录的次日版本：必须被日期过滤掉，不能算进 10-13
    conn.execute("INSERT INTO BASE_ACTIVITY_SUMMARY VALUES (2,'跑步',?,?,16,?)",
                 (start_ms + 86400000, start_ms + 86400000 + 600000, "{}"))
    conn.commit()

    ws = read_workouts(conn, day)
    assert len(ws) == 1, f"只该取当天那一次，得到 {len(ws)}"
    w = ws[0]
    assert w["name"] == "跑步" and w["kind"] == 16, w
    assert w["km"] == 4.24, w
    assert w["minutes"] == 20.0, w
    assert w["recovery_hours"] == 43.2, w
    # 毫秒被当成秒的话这里会是 1970 年
    assert w["start"].astimezone(tz).hour == 9, f"毫秒/秒搞错了: {w['start']}"

    by_metric = {e.metric: e for e in workouts_to_events(ws, day)}
    # 单次锻炼走 fitness —— 故意不进 health，否则日记里会出现
    # 一行没头没尾的「workout: 20.0」
    assert by_metric["workout"].domain == "fitness", by_metric["workout"].domain
    assert by_metric["workout"].meta["distance_km"] == 4.24, by_metric["workout"].meta
    assert by_metric["workout"].meta["recovery_hours"] == 43.2
    # 当天汇总走 health —— 日记要的是「今天跑了多少」
    assert by_metric["run_km"].domain == "health"
    assert by_metric["run_km"].value_num == 4.24
    assert by_metric["run_minutes"].value_num == 20.0
    assert by_metric["run_count"].value_num == 1
    assert by_metric["vo2max"].value_num == 47.64

    # 没锻炼的那天一个事件都不该产生
    assert read_workouts(conn, Day.parse("2026-10-20", tz)) == []
    assert workouts_to_events([], day) == []


check("millisecond START_TIME / date filter / fitness vs health split", t_workouts)


# ── 7g. 日记 frontmatter 里 health 的位置 ─────────────────────────
print("\n[18] Journal frontmatter with health data")


def t_journal_frontmatter():
    """锁住 health 块在 frontmatter **里面**。

    实测踩到的：`compose()` 原来是「先写收尾的 `---`，再追加 health」，
    于是 health 块落到了正文里，日记开头变成

        ---              ← frontmatter 收尾
        health:          ← 跑到正文里了
          steps: 18120.0
        # 2026-10-13 Tuesday

    这个 bug 一直存在，但从来不触发 —— 直到 Garmin 真的产出健康数据。
    """
    from generate_journal import compose
    from common import Day

    tz = ZoneInfo(CFG["timezone"])
    day = Day.parse("2026-10-13", tz)
    stats = {
        "total_hours": 2.3, "cost": 0.03, "currency": "CNY", "calls": 1,
        "tokens_in": 100, "tokens_out": 200, "cache_hit": 50, "peak": False,
        "generated_at": "2026-10-14 18:00", "sources": ["activitywatch", "gadgetbridge"],
        "reasoning": 10, "blocks": 5, "model": "test-model", "provider": "test",
    }
    out = compose(day, "# body\n", stats,
                  {"health": {"steps": 18120.0, "run_km": 4.24}}, "")

    lines = out.splitlines()
    assert lines[0] == "---", lines[:3]
    # 第二根分隔线才是 frontmatter 的结尾
    close = lines.index("---", 1)
    health = lines.index("health:")
    assert health < close, (
        "health 必须夹在两根 `---` 之间，否则它会作为正文出现在日记开头：\n"
        + "\n".join(lines[:close + 4])
    )
    # 两块 `---` 之间的内容应该能被 YAML 解析
    import yaml
    fm = yaml.safe_load("\n".join(lines[1:close]))
    assert fm["health"]["steps"] == 18120.0, fm.get("health")
    assert fm["date"] == "2026-10-13", fm


check("health block stays inside the frontmatter", t_journal_frontmatter)


# ── 7h. 中继的文件上传 ────────────────────────────────────────────
print("\n[19] Relay file upload")


def t_relay_upload():
    """锁住文件上传的两个安全点。

    ⚠️ 这是个**对外的端点**，能写文件就等于拿到服务器的一部分：

      1. **文件名必须走白名单。** 不做的话就是任意路径写入。
      2. **Tasker 发文件有两种写法**（裸字节 / multipart），两种都得认 ——
         只认一种的话，另一种会**静默**存下一个包着 multipart 外壳的坏文件：
         几十 KB、时间戳也对，但 SQLite 打不开。而下游只会说「数据库打不开」，
         看不出是传输格式的问题。
    """
    import importlib.util
    import tempfile

    spec = importlib.util.spec_from_file_location(
        "phone_relay",
        Path(__file__).resolve().parent.parent / "server" / "phone-relay" / "phone_relay.py")
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)

    # 白名单
    assert pr.upload_path("gadgetbridge.db") is not None
    for bad in ["passwd", "../../etc/passwd", "gadgetbridge.db.bak", "", "a/b",
                "GADGETBRIDGE.DB"]:
        assert pr.upload_path(bad) is None, f"{bad!r} 不该通过白名单"

    payload = b"SQLite format 3\x00" + b"x" * 200

    # 裸字节（Tasker 用 File To Send + Content-Type: application/octet-stream）
    assert pr.extract_upload(payload, "application/octet-stream") == payload

    # multipart（Tasker 不设 Content-Type 时可能这么发）
    boundary = "----testboundary123"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="gadgetbridge.db"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    got = pr.extract_upload(body, f"multipart/form-data; boundary={boundary}")
    assert got == payload, \
        f"multipart 没解对：拿到 {len(got)} 字节，应该是 {len(payload)}"

    with tempfile.TemporaryDirectory() as td:
        pr.DATA_DIR = Path(td)
        dest = Path(td) / "gadgetbridge.db"

        ok, _ = pr.save_upload("gadgetbridge.db", payload)
        assert ok and dest.read_bytes() == payload
        assert not dest.with_suffix(".db.tmp").exists(), "临时文件没清掉"

        # 再传一版：上一版要留成 .prev —— 新传的可能是坏的（传一半断了）
        pr.save_upload("gadgetbridge.db", payload + b"MORE")
        assert dest.read_bytes() == payload + b"MORE"
        assert dest.with_suffix(".db.prev").read_bytes() == payload, "上一版没保留"

        # 白名单外的名字：不落盘，且明确报错
        ok, msg = pr.save_upload("evil", b"x")
        assert not ok and "allow-list" in msg, msg
        assert not (Path(td) / "evil").exists()


check("upload allow-list / multipart unwrap / .prev kept", t_relay_upload)


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
    from collect_phone import _cluster_stays, collect_points_summary, fetch_points
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
        pts = fetch_points(day)
        evs = collect_points_summary(day, pts, _cluster_stays(pts))
    finally:
        collect_phone._get = orig

    assert len(pts) == 2, f"should normalise 2 points, got {len(pts)}"
    assert all(isinstance(p[1], float) and isinstance(p[2], float) for p in pts), \
        "string coords must be converted to float"

    assert len(evs) == 1, f"should summarise into 1 event, got {len(evs)}"
    e = evs[0]
    assert e.domain == "location" and e.metric == "tracked_points"
    assert e.value_num == 2, f"point count should be 2, got {e.value_num}"
    assert e.value_text, "main place should not be empty"

    # 字符串坐标必须被转成数字 —— 否则跨度会算成荒谬的值
    assert e.meta["span_km"] > 0, f"span not computed (string coords not converted?): {e.meta}"
    assert abs(e.meta["lat_center"] - 39.9046) < 0.001, e.meta
    assert abs(e.meta["lon_center"] - 116.4587) < 0.001, e.meta
    # 经度跨约 0.1 度 ≈ 11km
    assert 8 < e.meta["span_km"] < 15, f"span computed wrongly: {e.meta['span_km']}"

    # 空数据不能崩
    collect_phone._get = lambda *a, **k: []
    try:
        assert fetch_points(day) == []
        assert collect_points_summary(day, [], []) == []
    finally:
        collect_phone._get = orig

    # 坐标字段缺失 / 是非数字字符串 → 跳过该点，不抛异常
    collect_phone._get = lambda *a, **k: [
        {"latitude": "not-a-number", "longitude": "116.4", "timestamp": 1},
        {"latitude": "39.9", "longitude": "116.4", "timestamp": 2, "city": "X"},
    ]
    try:
        pts2 = fetch_points(day)
        evs2 = collect_points_summary(day, pts2, _cluster_stays(pts2))
        assert len(pts2) == 1 and evs2[0].value_num == 1, \
            f"a bad point should be skipped, not crash; got {pts2}"
    finally:
        collect_phone._get = orig


check("string-coord conversion / summary / empty data / bad-point skip", t_phone_points)


# ── 9b. 停留推导 + 交通段解析 ──────────────────────────────────────
print("\n[11] Stay derivation and track segment parsing")


def t_stays_and_tracks():
    """锁住这次修掉的两个真实缺陷（都是在真数据上才暴露的）。

    1. `collect_tracks()` 曾经**一直静默返回空**：Dawarich 的 /tracks 返回的是
       **GeoJSON FeatureCollection**，而代码按「带 start_at/mode 的对象数组」解析。
       dict 进不了 isinstance(list) 分支就直接 return [] —— 不报错、不打日志，
       连「0 segments」都不打印。有用的字段全在 feature["properties"] 里。

    2. `/visits` 实测恒为 0（开一天车跑 107km、两地各待几小时，依然返回 []），
       而 visit 是 normalize.py 填日记 Location 分区的**唯一**来源 ——
       不自己从原始点推导，那一段就永远是空的。
    """
    import collect_phone
    from collect_phone import _cluster_stays, collect_tracks, derive_stays
    from common import Day

    day = Day.parse("2026-10-13", ZoneInfo(CFG["timezone"]))
    t0 = 1791894173

    # ── 停留推导：40 分钟静止 + 之后快速移动 ──
    pts = [(t0 + i * 60, 39.9042 + (0.00002 * (i % 3)), 116.4074) for i in range(40)]
    pts += [(t0 + 3600 + i * 60, 39.95 + i * 0.01, 116.50) for i in range(20)]
    pts.sort()

    assert _cluster_stays(pts), "should find at least one stay"
    visits = derive_stays(day, pts)
    assert len(visits) == 1, f"only the 40-min stationary stretch is a stay, got {len(visits)}"
    v = visits[0]
    assert v.domain == "location" and v.metric == "visit"
    assert 35 <= v.value_num <= 45, f"stay should be about 40 min, got {v.value_num}"
    assert v.value_text, "visit should carry a place name"
    assert v.meta["derived"] == "local-stay"

    # 低于阈值的一小会儿不算停留
    short = [(t0 + i * 60, 39.9042, 116.4074) for i in range(5)]
    assert derive_stays(day, short) == [], "a 4-minute pause is not a stay"

    # ── 交通段：GeoJSON FeatureCollection 形状 ──
    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": []},
            "properties": {
                "id": 10,
                "start_at": "2026-10-13T06:00:00+08:00",
                "end_at": "2026-10-13T07:00:00+08:00",
                "distance": 107848,        # 米
                "duration": 49986,         # 秒
                "dominant_mode": "driving",
                "mode_timeline": [
                    {"start_time": t0, "end_time": t0 + 3600,
                     "emoji": "📍"},                          # 原地 → 必须跳过
                    {"start_time": t0 + 3600, "end_time": t0 + 4200,
                     "emoji": "🚗"},                          # 10 分钟驾车
                    {"start_time": t0 + 4300, "end_time": t0 + 4360,
                     "emoji": "🚴"},                          # 1 分钟骑行
                ],
            },
        }],
    }

    orig = collect_phone._get
    collect_phone._get = lambda *a, **k: geojson
    try:
        tr = collect_tracks(day)
    finally:
        collect_phone._get = orig

    assert len(tr) == 2, f"📍 must be skipped: expected 2 moving segments, got {len(tr)}"
    assert sorted(x.value_text for x in tr) == ["cycling", "driving"], \
        f"emoji must map to mode names, got {[x.value_text for x in tr]}"
    assert all(x.domain == "location" and x.metric == "transport" for x in tr)
    assert max(x.value_num for x in tr) == 10.0, [x.value_num for x in tr]

    # 认不出的形状不能崩，也不能假装成功
    collect_phone._get = lambda *a, **k: {"unexpected": True}
    try:
        assert collect_tracks(day) == []
    finally:
        collect_phone._get = orig


check("stay derivation / 📍 skipped / emoji→mode / unknown shape", t_stays_and_tracks)

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
