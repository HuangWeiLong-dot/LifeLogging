"""冒烟测试 —— 不依赖任何真实采集源，验证整条链路的代码是通的。

装完依赖后先跑这个。全绿再去接真实数据源，能省掉一大半排障时间。

用法：
  .venv/Scripts/python scripts/smoke_test.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ⚠️ 测试**不能**读真实的学习表 `state/app_map.json`。
#    否则 `classify()` 的结果会取决于你上周学过什么 —— 换台机器、换个时间，
#    同一批测试时红时绿。指到一个空的临时文件，等于「没有学到的映射」。
os.environ["LIFELOGGING_APP_MAP"] = str(
    Path(tempfile.mkdtemp(prefix="lifelogging-test-")) / "app_map.json")

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
        # ⚠️ 带上**出错的那一行**。裸 `assert x == y` 没有消息，光看
        #    「AssertionError: 」根本不知道是几十条断言里的哪一条 ——
        #    实测为这个来回跑了好几轮。
        tb = sys.exc_info()[2]
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        where = (f"\n       at {Path(tb.tb_frame.f_code.co_filename).name}:"
                 f"{tb.tb_lineno}" if tb is not None else "")
        print(f"  ❌ {name}\n       {type(e).__name__}: {e}{where}")
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


# ── 7d-2. 每日提问（中继 ↔ 笔记本的约定）────────────────────────────
print("\n[20] Daily questions")


def t_questions():
    """锁住「手机 → 笔记本」这条链上唯一的约定：value_text 的编码。

    ⚠️ 中继把答案编成 ``<题号>|<yes|no>|<备注>``，apply_answers.py 按同样的
       规则解。两边一旦错位，答案就变成一堆没人认得的字符串 —— **两边都不会
       报错**，只是「勾掉任务」这件事永远不发生。这个项目栽过的静默失效里，
       这条属于最难发现的一类。
    """
    import importlib.util
    import tempfile

    path = (Path(__file__).resolve().parent.parent
            / "server" / "phone-relay" / "phone_relay.py")
    spec = importlib.util.spec_from_file_location("phone_relay_q", path)
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)

    # 1. 备注里的竖线和换行不能破坏编码（用户随手打的东西什么都有）
    evs = pr.answers_to_events([
        {"id": "2026-10-14-aaa1", "value": "YES", "note": "提交了|带竖线\n和换行"},
        {"id": "2026-10-14-bbb2", "value": "no"},
        {"id": "", "value": "yes"},                    # 没题号 → 丢
        {"id": "2026-10-14-ccc3", "value": "HalfDone"},  # 选择题的选项原文 → 收下
        {"id": "2026-10-14-ddd4", "value": "   "},     # 空值 → 丢
        "not a dict",                                  # 丢
    ])
    assert len(evs) == 3, evs
    qid, val, note = evs[0]["value_text"].split("|", 2)
    assert (qid, val) == ("2026-10-14-aaa1", "yes"), "yes/no 要归一成小写，题号原样保留"
    assert "|" not in note and "\n" not in note, f"备注必须压成一行且不含竖线: {note!r}"
    assert evs[1]["value_text"] == "2026-10-14-bbb2|no|", evs[1]["value_text"]
    # ⚠️ 选项原文**不能**被 lower 掉 —— 回写时要拿它和问题文件里的选项逐字对上，
    #    大小写一变就匹配不到，那条「做到了」就不会勾。
    assert evs[2]["value_text"] == "2026-10-14-ccc3|HalfDone|", evs[2]["value_text"]
    assert evs[0]["domain"] == "question" and evs[0]["metric"] == "answer"

    with tempfile.TemporaryDirectory() as td:
        pr.DATA_DIR = Path(td)
        pr._seen.clear()
        ok, _ = pr.save_questions("2026-10-14", {
            "day": "2026-10-14",
            "questions": [{"id": "2026-10-14-aaa1", "text": "做了吗？", "evidence": "原话"},
                          {"id": "2026-10-14-bbb2", "text": "另一个？", "evidence": ""}],
        })
        assert ok
        q, left = pr.pending_questions()
        assert q and len(left) == 2, (q, left)

        # 答掉一条 → 表单里只剩另一条
        pr.append_events(pr.answers_to_events(
            [{"id": "2026-10-14-aaa1", "value": "yes", "note": ""}]))
        q, left = pr.pending_questions()
        assert len(left) == 1 and left[0]["id"] == "2026-10-14-bbb2", left

        # 2. 日期形状不对不能拼出文件路径（这是个对外的写端点）
        assert pr.questions_path("../../etc/passwd") is None
        assert pr.questions_path("") is None
        assert pr.questions_path("2026-10-14") is not None
        assert not pr.save_questions("not-a-day", {"day": "x", "questions": []})[0]

        # 3. 页面里的题干必须转义 —— 它是要在手机浏览器里渲染的
        page = pr.render_ask_page(
            q, [{"id": "x<y>", "text": "<script>alert(1)</script>", "evidence": "a & b"}], "k")
        assert "<script>alert(1)</script>" not in page, "题干必须转义"
        assert "&lt;script&gt;" in page and "a &amp; b" in page

    # 4. 笔记本那侧的解析必须和上面同源
    from apply_answers import find_task
    ops = ["Commit the LifeLogging repo now that the .gitignore exists",
           "Complete the Tailscale key configuration that was started at 20:11"]
    assert find_task(ops, ops[1]) == ops[1], "逐字照抄要能对上"
    assert find_task(ops, "complete the tailscale key configuration, that was started at 20:11.") \
        == ops[1], "大小写和标点差异要能容忍"
    # ⚠️ 这条是「别把任务误标成完成」的唯一防线：公共片段太短就不许对。
    assert find_task(ops, "Commit the repo") is None, "短片段不能乱对"
    assert find_task(ops, "") is None, "没有 ref 就不该对"


check("answer encoding round-trips / no path escape / HTML escaped / safe task matching",
      t_questions)


def t_live_questions():
    """实时问题（服务器白天生成）和笔记本批**并存**，且互不遮蔽。

    ⚠️ 这里锁的是一个实测踩到的静默缺陷：
       `load_latest_questions()` 的通配符是 `questions-*.json`，而
       `questions-live-2026-10-16.json` **也匹配**它。按文件名排序时
       '2' < 'l'，所以实时那份永远排在最后、被当成「最新一份」。
       后果不是报错，而是**笔记本那批题整体消失** —— 飞书机器人只会问
       实时题，问责那批（拿你自己写的话堵你的那批）静默不见了。
    """
    import importlib.util
    import tempfile

    path = (Path(__file__).resolve().parent.parent
            / "server" / "phone-relay" / "phone_relay.py")
    spec = importlib.util.spec_from_file_location("phone_relay_lq", path)
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)

    with tempfile.TemporaryDirectory() as td:
        pr.DATA_DIR = Path(td)
        # ⚠️ 用**真实的今天**，不能写死。`answered_ids()` 是从今天往回扫 7 天，
        #    硬编码一个日期的话，这个测试只在当天跑才是真的 —— 换一天它要么
        #    假绿要么假红，而「过期」那条断言更是永远不成立。
        day = datetime.now(pr.TZ).date().isoformat()

        pr.save_questions(day, {"day": day, "generated_at": "x", "questions": [
            {"id": f"{day}-lap1", "format": "yes_no", "text": "笔记本那道"}]})
        pr.save_live_questions(day, {"day": day, "generated_at": "y", "questions": [
            {"id": f"{day}-liv1", "format": "yes_no", "text": "实时那道"}]})

        # ⚠️ 核心断言：笔记本那份仍要能被认出来
        got = pr.load_latest_questions()
        assert got and got["questions"][0]["id"] == f"{day}-lap1", \
            f"实时文件把笔记本那份顶掉了：{got}"

        assert pr.load_latest_live_questions()["questions"][0]["id"] == f"{day}-liv1"

        # 手机那条链路：**只**看笔记本批（语义冻结）
        _, phone = pr.pending_questions()
        assert [q["id"] for q in phone] == [f"{day}-lap1"], phone

        # 飞书那条：两批的并集
        _, both = pr.pending_questions(include_live=True)
        assert [q["id"] for q in both] == [f"{day}-lap1", f"{day}-liv1"], both
        # ⚠️ 每题都要带上「它属于哪一天」。渲染层靠它决定要不要写
        #    「这是 X 月 X 日的题」—— 缺了的话，前一天的实时题会以「今天」
        #    的口吻出现在今天，看着正常但是错的。
        assert [q["day"] for q in both] == [day, day], both
        # ⚠️ 还要带**来源**。机器人靠它区分「现在就推」（实时批）和
        #    「每天整批推」（问责批）—— 没有这个字段，实时提问就变成了
        #    「第二天早上一次性看到一堆昨天的题」。实测由使用者指出。
        assert [q["source"] for q in both] == ["laptop", "live"], both

        # 答掉一题 → 并集里就该没了（去重靠的是 answered_ids，不是文件）
        pr.append_events([{"ts": int(datetime.now(timezone.utc).timestamp()),
                           "domain": "question", "metric": "answer",
                           "value_text": f"{day}-liv1|yes|", "source": "test"}])
        _, both = pr.pending_questions(include_live=True)
        assert [q["id"] for q in both] == [f"{day}-lap1"], both

        # 实时问题**会过期**：隔了几天的实时题不该再冒出来。
        # （笔记本那批正相反 —— 它无脑取最新，因为「昨晚生成、今早来取」是常态。）
        # 注意：「最新一份」是按**文件名**排的，所以只写一份旧的是不够的，
        # 得先把当天那份挪走，剩下的「最新」才是旧的那份。
        pr.live_questions_path(day).unlink()
        old = "2020-01-01"
        pr.save_live_questions(old, {"day": old, "generated_at": "z", "questions": [
            {"id": f"{old}-stale", "format": "yes_no", "text": "三天前的实时题"}]})
        assert pr.load_latest_live_questions() is None, "隔了几天的实时题该过期"
        _, both = pr.pending_questions(include_live=True)
        assert all(not q["id"].endswith("-stale") for q in both), both
        # 笔记本那批不受影响，照常还在
        assert [q["id"] for q in both] == [f"{day}-lap1"], both

        # 路径穿越那道门对实时文件同样要拦
        assert pr.live_questions_path("../../etc/passwd") is None
        assert not pr.save_live_questions("not-a-day", {"questions": [{}]})[0]


check("live questions: 实时批不遮蔽笔记本批，手机链路冻结，实时题会过期",
      t_live_questions)


def t_relay_now_timestamp():
    """`"ts":"now"` 只认这一个字面量；**没写 ts 不能变成 now**。

    ⚠️ 加这个是为了 OpenTasker —— 它的模板里没有内置的 Unix 秒变量。
       但「没写 ts 也当 now」是另一种失败：批量补传历史数据时少一个字段，
       所有事件会被压到收件那一刻。数据看起来都在，只是时间全错。
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "pr_now", root / "server" / "phone-relay" / "phone_relay.py")
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)

    s = pr.to_utc_iso({"ts": "now", "metric": "unlock"})
    assert s and s.endswith("Z"), s
    assert s[:4] == str(datetime.now(timezone.utc).year), s

    assert pr.to_utc_iso({"metric": "unlock"}) is None, "没写 ts 不能被当成 now"
    assert pr.to_utc_iso({"ts": 1700000000000, "metric": "unlock"}) is None, \
        "毫秒值必须丢掉，不能猜"
    assert pr.to_utc_iso({"ts": 1700000000, "metric": "unlock"}) == \
        "2023-11-14T22:13:20Z"


check("relay: ts=now 可用，但没写 ts / 毫秒值仍然丢掉",
      t_relay_now_timestamp)


def t_live_question_rules():
    """每小时一题：**固定节拍**，数据再平淡也要问；且不勾任务、不写死「今天」。

    ⚠️ 这里锁的是最早那版的设计错误：最初是三条阈值规则（停留/静默/单应用），
       够不到阈值就一题都不发 —— 而使用者要的是「每小时至少问一次」。
       把节拍和选材混成一套规则，就会在数据平淡的那天彻底沉默。
    """
    import importlib.util

    lq_path = Path(__file__).resolve().parent / "live_questions.py"
    spec = importlib.util.spec_from_file_location("live_q", lq_path)
    lq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lq)

    # 用真实的「整点」构造，这样和脚本里的 now 是同一套时区语义。
    now = lq.datetime.now(lq.TZ).replace(minute=0, second=0, microsecond=0)
    day = now.date().isoformat()
    start = now - lq.timedelta(hours=1)
    iso = lambda dt: dt.isoformat()          # noqa: E731

    stay = {"place": "望京, 北京朝阳", "minutes": 80.0,
            "start": iso(start - lq.timedelta(minutes=20)),
            "end": iso(now), "is_coord": False}
    events = [
        {"metric": "unlock", "ts_utc": iso(start + lq.timedelta(minutes=5))},
        {"metric": "app_switch", "value_text": "com.tencent.mm",
         "ts_utc": iso(start + lq.timedelta(minutes=6))},
        {"metric": "unlock", "ts_utc": iso(start + lq.timedelta(minutes=40))},
    ]
    snap = {"last_hour": {"total_minutes": 35.0,
                          "top_apps": [["Code.exe", 25.0], ["msedge.exe", 10.0]]}}

    q = lq.rule_hourly(day, [stay], events, snap, now)
    assert q, "每小时都该有一题"
    assert q["text"].startswith(start.strftime("%H:%M")), q["text"]
    assert "望京" in q["text"], q["text"]
    assert "Code.exe" in q["text"], q["text"]
    assert "2 unlocks" in q["text"], q["text"]
    assert "WeChat" in q["text"], f"包名要翻成人话：{q['text']}"
    assert "今天" not in q["text"], f"题干不该写死「今天」：{q['text']}"
    assert q["_key"] == f"hour-{day}T{now.strftime('%H')}", q["_key"]
    # ⚠️ 文字题：apply_answers 对 text 永远判「没完成」，所以实时题**不会勾任务**。
    assert q["format"] == "text" and q["ref"] == ""
    assert q["id"].startswith(f"{day}-"), "题号必须带日期前缀"

    # ⚠️ 核心：数据平淡也要问。这是「每小时一次」和「够阈值才问」的分水岭。
    quiet = lq.rule_hourly(day, [], [], None, now)
    assert quiet is not None, "这一小时没数据也要问，不能沉默"
    # ⚠️ 措辞必须是「不知道」，不能是「没有活动」——
    #    「笔记本没推过快照」和「电脑上一片空白」是两件事，
    #    后者会一路写进日记，变成一句假话。
    assert "laptop has not pushed a snapshot" in quiet["text"], quiet["text"]
    assert "nothing reported yet" in quiet["text"], quiet["text"]
    assert "今天" not in quiet["text"], f"题干不该写死「今天」：{quiet['text']}"

    # ⚠️ 过期的快照不能当「这一小时」的数据讲。
    #    实测事故：07:02 / 08:03 / 10:03 三道题复读同一份 Code.exe 数字，
    #    因为笔记本睡着时快照停在原地，而代码没有判新鲜度。
    stale = {"day": "2020-01-01", "pushed_at": "2020-01-01T00:00:00+08:00",
             "last_hour": {"total_minutes": 35.0,
                           "top_apps": [["Code.exe", 25.0]]}}
    stale_txt = lq.rule_hourly(day, [], [], stale, now)["text"]
    assert "Code.exe" not in stale_txt, f"过期快照不该出现：{stale_txt}"
    assert "data stops at" in stale_txt, stale_txt

    # 一小时前就结束的停留，不该被当成「还在那儿」
    old = dict(stay, place="中关村", minutes=80.0,
               start=iso(start - lq.timedelta(hours=3)),
               end=iso(start - lq.timedelta(minutes=10)))
    assert "中关村" not in lq.rule_hourly(day, [old], [], None, now)["text"]

    # 路过的短暂停留不值得一提
    brief = dict(stay, place="便利店", minutes=10.0)
    assert "便利店" not in lq.rule_hourly(day, [brief], [], None, now)["text"]

    # 同一小时只问一次：key 里带小时，第二次跑会被 fired 拦住
    assert lq.rule_hourly(day, [], [], None, now)["_key"] == \
        lq.rule_hourly(day, [], [], None, now)["_key"]


check("live questions: 每小时必问（数据平淡也问）/ 不勾任务 / 不写死「今天」",
      t_live_question_rules)


def t_qa_join():
    """中继 /qa：按**作答日**把两份问题文件和答案对上。

    ⚠️ 早晨批的问题日通常是 D-1（晚上生成、早上回答），实时批是 D。
       只按同一份文件找题会把早晨批漏掉；只按提问日找答案又会把晚答漏掉。
       这里锁住「加载 D 和 D-1 两份、按题号合并、重答取最新、保留备注」。
    """
    import importlib.util
    import json as _json
    import tempfile

    path = (Path(__file__).resolve().parent.parent
            / "server" / "phone-relay" / "phone_relay.py")
    spec = importlib.util.spec_from_file_location("phone_relay_qa", path)
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)

    with tempfile.TemporaryDirectory() as td:
        pr.DATA_DIR = Path(td)
        (Path(td) / "questions-2026-10-17.json").write_text(_json.dumps({
            "day": "2026-10-17",
            "questions": [{"id": "2026-10-17-aaaa1111", "kind": "commitment",
                           "format": "yes_no", "text": "任务 A 做了吗？",
                           "evidence": "任务表", "ref": "Task A"}],
        }, ensure_ascii=False), encoding="utf-8")
        (Path(td) / "questions-live-2026-10-18.json").write_text(_json.dumps({
            "day": "2026-10-18",
            "questions": [
                {"id": "2026-10-18-bbbb2222", "kind": "routine",
                 "format": "text", "text": "07:00–08:00 在忙什么？",
                 "evidence": "数据", "ref": ""},
                {"id": "2026-10-18-cccc3333", "kind": "summary",
                 "format": "text", "text": "今天最值得记住的事？",
                 "evidence": "只有你知道", "ref": ""},
                # 自评打分题：dim/routine 必须透到 /qa，否则 collect_labels
                # 只能去猜题面 —— 猜错一条就是脏标签。
                {"id": "2026-10-18-dddd4444", "kind": "summary",
                 "routine": True, "dim": "心情", "format": "choice",
                 "text": "今天整体心情打几分？",
                 "evidence": "情绪数据看不到。", "ref": "",
                 "options": [{"label": "1 很差"}, {"label": "4 不错"}]},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        rows = [
            {"ts_utc": "2026-10-18T00:10:00Z", "domain": "question",
             "metric": "answer", "source": "relay",
             "value_text": "2026-10-17-aaaa1111|yes|早上完成了"},
            {"ts_utc": "2026-10-18T01:00:00Z", "domain": "question",
             "metric": "answer", "source": "relay",
             "value_text": "2026-10-18-bbbb2222|在上课|"},
            {"ts_utc": "2026-10-18T02:00:00Z", "domain": "question",
             "metric": "answer", "source": "relay",
             "value_text": "2026-10-18-bbbb2222|在图书馆|重答"},
            {"ts_utc": "2026-10-18T15:00:00Z", "domain": "question",
             "metric": "answer", "source": "relay",
             "value_text": "2026-10-18-cccc3333|见到朋友|很好"},
            {"ts_utc": "2026-10-18T15:02:00Z", "domain": "question",
             "metric": "answer", "source": "relay",
             "value_text": "2026-10-18-dddd4444|4 不错|下午有点堵"},
            {"ts_utc": "2026-10-18T15:01:00Z", "domain": "question",
             "metric": "answer", "source": "relay",
             "value_text": "2026-10-18-unknown|随便|"},
        ]
        (Path(td) / "phone-2026-10-18.jsonl").write_text(
            "\n".join(_json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

        out = pr.day_qa("2026-10-18")
        assert len(out["qa"]) == 5, out
        by = {x["id"]: x for x in out["qa"]}
        assert by["2026-10-17-aaaa1111"]["source"] == "laptop"
        assert by["2026-10-17-aaaa1111"]["note"] == "早上完成了"
        assert by["2026-10-18-bbbb2222"]["source"] == "live"
        assert by["2026-10-18-bbbb2222"]["answer"] == "在图书馆", "重答要取最新"
        assert by["2026-10-18-bbbb2222"]["note"] == "重答"
        assert by["2026-10-18-cccc3333"]["kind"] == "summary"
        assert by["2026-10-18-unknown"].get("answer_only") is True, "没题文也不能丢答案"
        # 自评题的 dim / routine 要透出来，collect_labels 靠它认维度
        rated = by["2026-10-18-dddd4444"]
        assert rated["dim"] == "心情" and rated["routine"] is True, rated
        assert rated["format"] == "choice" and rated["answer"] == "4 不错", rated
        assert by["2026-10-18-bbbb2222"]["dim"] == "", "没有 dim 的题不能编一个"


check("relay /qa: 作答日合并两份问题文件 / 保留备注 / 重答取最新 / answer-only",
      t_qa_join)


def t_commitment_questions():
    """实时任务检查：固定时段、只问未问过的任务、ref 能对回 Tasks.md。"""
    import importlib.util
    import json as _json
    import tempfile
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    path = (Path(__file__).resolve().parent.parent
            / "scripts" / "live_questions.py")
    spec = importlib.util.spec_from_file_location("live_questions_commit", path)
    lq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lq)

    tz = ZoneInfo("Asia/Shanghai")
    now = _dt(2026, 10, 19, 14, 5, tzinfo=tz)
    tasks = ["14:00-16:00 学习通网课", "Configure OBS", "买牛奶"]
    q = lq.commitment_question("2026-10-19", tasks, now, [])
    assert q and q["kind"] == "commitment" and q["format"] == "choice", q
    assert q["ref"] == "14:00-16:00 学习通网课", q
    # ⚠️ 最后一项必须是「关掉这条任务」。没有它，放弃的任务永远出不来出口，
    #    每天照问 —— 实测同一条被连问三天。这一断言就是为了钉住那个出口。
    assert q["options"][0]["done"] is True, q["options"]
    assert q["options"][-1].get("drop") is True, q["options"]
    assert not any(o.get("done") and o.get("drop") for o in q["options"]), q["options"]

    # 不在固定时段就不生成
    assert lq.commitment_question("2026-10-19", tasks,
                                  _dt(2026, 10, 19, 15, 5, tzinfo=tz), []) is None
    # 同一任务不重复问
    q2 = lq.commitment_question("2026-10-19", tasks, now, [{"ref": q["ref"]}])
    assert q2 and q2["ref"] != q["ref"], q2

    old_state = lq.CFG.get("state_dir")
    with tempfile.TemporaryDirectory() as td:
        lq.CFG["state_dir"] = td
        (Path(td) / "journal-input-2026-10-19.json").write_text(_json.dumps({
            "tasks": ["B", "A", "B"], "carryover": ["C", "A"],
        }, ensure_ascii=False), encoding="utf-8")
        assert lq.load_commitment_tasks("2026-10-19") == ["C", "A", "B"], \
            "carryover 在前、去重、保序"
    lq.CFG["state_dir"] = old_state

    # 日记提示词要把今天和昨天的问答都带上
    import generate_journal as gj
    from common import Day
    day = Day.parse("2026-10-19", tz)
    prompt = gj.build_user_prompt(
        {"blocks": [], "total_hours": 0}, day, [],
        qa=[{"text": "Q1", "answer": "A1", "note": "N1"}],
        previous_day_qa=[{"text": "Q0", "answer": "A0"}],
        previous_day_recap="RECAP")
    assert "Questions you answered today" in prompt and "A1" in prompt
    assert "N1" in prompt, "备注要进日记"
    assert "What the user said yesterday" in prompt and "A0" in prompt
    assert "RECAP" in prompt


check("live commitment: 固定时段 / 同任务不重复 / ref 可勾任务 / 问答进日记提示词",
      t_commitment_questions)


def t_final_marker():
    """final 标记：23:00 不是终版，午夜后才是；水位线只看它。"""
    import importlib.util
    import json as _json
    import tempfile
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    path = (Path(__file__).resolve().parent.parent
            / "scripts" / "push_journal_input.py")
    spec = importlib.util.spec_from_file_location("push_journal_final", path)
    pji = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pji)

    old_state = pji.STATE
    with tempfile.TemporaryDirectory() as td:
        pji.STATE = Path(td)
        p = pji.write_final_marker("2026-10-18", False, True)
        d = _json.loads(p.read_text(encoding="utf-8"))
        assert d["final"] is False and d["relay_ok"] is True, d
        p = pji.write_final_marker("2026-10-18", True, True)
        assert _json.loads(p.read_text(encoding="utf-8"))["final"] is True
    pji.STATE = old_state

    from common import Day
    tz = ZoneInfo("Asia/Shanghai")
    day = Day.parse("2026-10-18", tz)
    assert _dt(2026, 10, 18, 23, 0, tzinfo=tz) < day.end_local
    assert _dt(2026, 10, 19, 0, 1, tzinfo=tz) >= day.end_local


check("final marker: 23:00 仍是 final=false，午夜后才终版", t_final_marker)


def t_apply_answers_live():
    """apply_answers 要能看见服务器生成的实时任务题（否则 ref 永远对不上）。"""
    import importlib.util
    import json as _json
    import sys as _sys
    import tempfile
    import types

    path = (Path(__file__).resolve().parent.parent
            / "scripts" / "apply_answers.py")
    spec = importlib.util.spec_from_file_location("apply_answers_live", path)
    aa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aa)

    from common import Day
    from zoneinfo import ZoneInfo
    day = Day.parse("2026-10-18", ZoneInfo("Asia/Shanghai"))

    old_state = aa.STATE
    old_mod = _sys.modules.get("qa_client")
    with tempfile.TemporaryDirectory() as td:
        aa.STATE = Path(td)
        (Path(td) / "questions-2026-10-18.json").write_text(_json.dumps({
            "questions": [{"id": "2026-10-18-morning1", "kind": "commitment",
                           "format": "yes_no", "text": "早晨题",
                           "ref": "Task M", "options": []}],
        }, ensure_ascii=False), encoding="utf-8")
        fake = types.ModuleType("qa_client")
        fake.fetch = lambda d: {"qa": [{
            "id": "2026-10-18-live1", "kind": "commitment",
            "format": "choice", "text": "任务 A 做了吗？", "ref": "Task A",
            "options": [{"label": "做了", "done": True}],
        }]} if d == "2026-10-19" else {"qa": []}
        _sys.modules["qa_client"] = fake
        try:
            qs = aa.read_questions(day)
            assert {q["id"] for q in qs} == {
                "2026-10-18-morning1", "2026-10-18-live1"}, qs
            live = [q for q in qs if q["id"] == "2026-10-18-live1"][0]
            assert aa.answered_done(live, "做了") is True
            assert aa.find_task(["Task A", "Task B"], live["ref"]) == "Task A"
            assert aa.answered_done({"format": "text"}, "随便") is False
        finally:
            aa.STATE = old_state
            if old_mod is not None:
                _sys.modules["qa_client"] = old_mod
            else:
                _sys.modules.pop("qa_client", None)


check("apply_answers: 合并实时任务题，答「做了」能对回任务", t_apply_answers_live)


def t_memory_qa():
    """长期记忆要能按**问题文本**搜到答案，而不是只有一句孤零零的回答。"""
    import importlib.util
    import json as _json
    import os
    import tempfile

    path = (Path(__file__).resolve().parent.parent / "scripts" / "memory.py")
    spec = importlib.util.spec_from_file_location("memory_qa", path)
    mem = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mem)

    state = Path(tempfile.mkdtemp(prefix="mem-qa-"))
    db = Path(tempfile.mkdtemp(prefix="mem-db-")) / "events.db"
    (state / "qa-2026-10-18.json").write_text(_json.dumps({
        "day": "2026-10-18",
        "qa": [{"id": "2026-10-18-b", "text": "任务 B 做了吗？",
                "answer": "没做", "note": "太累", "kind": "commitment",
                "source": "live", "ts_utc": "2026-10-18T10:00:00Z"}],
    }, ensure_ascii=False), encoding="utf-8")

    old_db = os.environ.get("LIFELOGGING_MEMORY_DB")
    old_state = mem.CFG.get("state_dir")
    os.environ["LIFELOGGING_MEMORY_DB"] = str(db)
    mem.CFG["state_dir"] = str(state)
    try:
        mem.reindex()
        hits = mem.search("任务", 3)
        assert any(h["doc_key"].startswith("qa:") for h in hits), hits
        assert any("任务 B 做了吗" in h["excerpt"] for h in hits), hits
    finally:
        mem.CFG["state_dir"] = old_state
        if old_db is None:
            os.environ.pop("LIFELOGGING_MEMORY_DB", None)
        else:
            os.environ["LIFELOGGING_MEMORY_DB"] = old_db


check("memory: 问答带问题原文入索引，可按题目搜到", t_memory_qa)


def t_feishu_freeform_cap():
    """自由问答的每日封顶 —— 唯一挡住「随手问问变成花钱黑洞」的东西。

    ⚠️ 自由问答每问一次就是一次真实的 LLM 调用。这条通道又特别容易问多
       （打字就能问），所以上限必须在**调模型之前**拦住，而不是事后统计。
       同时要保住一件事：模板那几类（去哪了/手机/电脑）**不占这个额度**，
       否则额度用完之后整条通道看着就像哑了。
    """
    import importlib.util
    import tempfile

    path = (Path(__file__).resolve().parent.parent
            / "server" / "feishu-bot" / "live.py")
    spec = importlib.util.spec_from_file_location("live_ff", path)
    lv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lv)

    with tempfile.TemporaryDirectory() as td:
        lv.cfg = lambda: {"state_dir": td}       # 不碰真实配置
        lv.LLM_DAILY_CAP = 2
        day = "2026-10-16"
        counter = Path(td) / f"feishu-llm-{day}.json"

        # 计数已经到上限 → 必须**在调模型之前**返回，这里没有任何网络可用，
        # 真去调的话这个测试会以连接错误的形式炸掉。
        counter.write_text(json.dumps({"calls": 2}), encoding="utf-8")
        out = lv.answer_freeform("今天出门几次", day_iso=day,
                                 base="http://127.0.0.1:1", key="k")
        assert "额度用完" in out, out
        assert "2 次" in out, out
        assert "模板" in out, "要说清模板那几类不受影响，别让人以为整个哑了"

        # 计数文件坏了/不存在 → 当成 0，不能因此崩掉（那时它会去调模型，
        # 这里只验「读不出来不抛异常」这一半：给个坏文件，看它是否还走额度分支）
        counter.write_text("{ 这不是 json", encoding="utf-8")
        lv.LLM_DAILY_CAP = 0                      # 逼它在读完之后立刻拦下来
        out = lv.answer_freeform("再问一次", day_iso=day,
                                 base="http://127.0.0.1:1", key="k")
        assert "额度用完" in out, f"坏掉的计数文件该当成 0，然后被上限拦住：{out}"


check("feishu: 自由问答每日封顶（在调模型之前拦，且不牵连模板那几类）",
      t_feishu_freeform_cap)


def t_placeholder_detection():
    """「还没填」的判定 —— 三种写法都得认出来。

    ⚠️ 实测踩到：文档里的占位符是纯英文的（cli_REPLACE_ME），但**整行连着中文
       一起粘进去**（cli_你的真实appid）才是常态。那种值是「有」的 —— 非空、
       也不含 REPLACE，看着完全正常 —— 然后一路跑到 SDK / httpx 里炸成一个
       编码错误，跟「你忘了填」这个事实看不出任何关系。

    ⚠️ 另一个实测踩到的：我原来写的是 startswith(\"cli-REPLACE\")（连字符），
       而文档里的占位符是 cli_REPLACE_ME（下划线）—— 连自己写的占位符都拦不住。
       所以现在查的是 REPLACE 这个子串，不分位置、不分大小写。
    """
    import importlib.util

    def load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    root = Path(__file__).resolve().parent.parent
    fb = load("fb_ph", root / "server" / "feishu-bot" / "feishu_bot.py")
    lq = load("lq_ph", root / "scripts" / "live_questions.py")
    pd = load("pd_ph", root / "scripts" / "push_desktop.py")

    for bad in ("", "   ", "REPLACE_ME", "cli_REPLACE_ME", "cli-REPLACE-ME",
                "REPLACE_WITH_THE_RELAY_KEY", "你的key", "cli_你的真实appid",
                "sk-你的key"):
        assert fb.looks_like_placeholder(bad), f"{bad!r} 该被认成「还没填」"

    for good in ("cli_a1b2c3d4", "sk-9f8e7d6c", "rO5b4H6l6Rw_R_4DBwbv"):
        assert not fb.looks_like_placeholder(good), f"{good!r} 是正常值，别误伤"

    # ⚠️ 三处的判断必须**同义**。同一个 config 在两个脚本里一个过、一个不过，
    #    是那种「换个脚本跑就好了」的幽灵故障。
    for v in ("", "REPLACE_ME", "你的key", "http://100.127.41.49:8788", "cli_a1b2c3d4"):
        assert fb.looks_like_placeholder(v) == lq._unfilled(v) == pd._unfilled(v), v


check("placeholder: 中文占位符 / 各种 REPLACE 写法，三处判断同义",
      t_placeholder_detection)


def t_feishu_exit_codes():
    """配置错要用**专属退出码**退出，而且 unit 里必须认这个码。

    ⚠️ 实测（2026-10-16）：env 里留着中文占位符时，`Restart=always` +
       `RestartSec=5` 让日志被同一句「✗ FEISHU_APP_SECRET 没填…」每 5 秒刷一遍，
       restart counter 涨到 24 才停（因为人把它改对了）。
       systemd 自己的 StartLimitBurst **拦不住** —— 默认 5 次/10 秒，而 5 秒一次
       意味着 10 秒里只重启 2 次，永远够不到上限。

    这个测试钉的是**两半**：代码得用那个码退出，unit 得认那个码。
    任一边改了没跟上，整套就静默退回「每 5 秒刷屏」。
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "fb_ec", root / "server" / "feishu-bot" / "feishu_bot.py")
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    assert fb.EXIT_CONFIG not in (0, 1), \
        "0 是成功、1 是普通出错，配置错要用一个专属的码"

    # 带着错配置启动 → 必须用那个码退出，而不是 1
    old_id, old_secret, old_argv = fb.APP_ID, fb.APP_SECRET, sys.argv
    fb.APP_ID, fb.APP_SECRET = "cli_你的appid", "REPLACE_ME"
    sys.argv = ["feishu_bot.py"]
    try:
        rc = fb.main()
    finally:
        fb.APP_ID, fb.APP_SECRET, sys.argv = old_id, old_secret, old_argv
    assert rc == fb.EXIT_CONFIG, f"配置错时该退 {fb.EXIT_CONFIG}，实际 {rc}"

    # 另一半：unit 里必须真的认这个码
    unit = (root / "server" / "feishu-bot"
            / "lifelogging-feishu-bot.service").read_text(encoding="utf-8")
    assert f"RestartPreventExitStatus={fb.EXIT_CONFIG}" in unit, \
        "unit 没认这个退出码 —— 那它就会继续每 5 秒重启一次"


check("feishu: 配置错用专属退出码，且 unit 里认它（两边必须一致）",
      t_feishu_exit_codes)


def t_feishu_relay_base():
    """中继地址不能写死 —— 它绑的是 Tailscale 地址，而那是机器相关的。

    ⚠️ 实测踩到：unit 里默认值写的是 127.0.0.1，而中继绑在 100.127.41.49。
       后果**完全静默**：机器人连飞书一切正常、日志一片祥和，只是每一次中继
       调用都失败 —— 表现是「收得到消息，答案从来不落库」。
       讽刺的是那个 unit 的注释里恰好写了这条坑，然后自己踩了。
    """
    import importlib.util
    import tempfile

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "fb_rb", root / "server" / "feishu-bot" / "feishu_bot.py")
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    # ── 能从**中继自己的 unit** 里问出地址 ──
    with tempfile.TemporaryDirectory() as td:
        u = Path(td) / "lifelogging-relay.service"
        u.write_text(
            "[Service]\n"
            "Environment=RELAY_HOST=100.127.41.49\n"
            "Environment=RELAY_PORT=8788\n"
            "Environment=RELAY_DATA_DIR=/var/lib/lifelogging\n",
            encoding="utf-8")
        old_unit, old_base = fb.RELAY_UNIT_FILE, fb.RELAY_BASE
        fb.RELAY_UNIT_FILE, fb.RELAY_BASE = u, ""
        try:
            cands = fb.relay_base_candidates()
        finally:
            fb.RELAY_UNIT_FILE, fb.RELAY_BASE = old_unit, old_base
        assert "http://100.127.41.49:8788" in cands, cands
        assert any("127.0.0.1" in c for c in cands), "本机回环也要留着当兜底"
        assert len(cands) == len(set(cands)), f"候选里有重复：{cands}"

    # 显式配置的必须排第一（要覆盖时它得赢）
    old_base = fb.RELAY_BASE
    fb.RELAY_BASE = "http://example.invalid:9999"
    try:
        assert fb.relay_base_candidates()[0] == "http://example.invalid:9999"
    finally:
        fb.RELAY_BASE = old_base

    # ── ⚠️ 另一半：unit 里**不能**再写死一个默认地址 ──
    unit = (root / "server" / "feishu-bot"
            / "lifelogging-feishu-bot.service").read_text(encoding="utf-8")
    for line in unit.splitlines():
        s = line.strip()
        if s.startswith("Environment=FEISHU_RELAY_BASE="):
            raise AssertionError(
                f"unit 里又写死了中继地址，而那台机器上多半不是它：{s}\n"
                "    要么删掉（让程序自己去问），要么确认它真的是那个地址。")


check("feishu: 中继地址去问中继自己的 unit，不在 unit 里写死",
      t_feishu_relay_base)


def t_feishu_api_error_hints():
    """飞书的错误码要翻成「该去改什么」，而不是把栈甩给人看。

    ⚠️ 实测踩到的那个：`code 21001 Access denied by access control policy`
       看着**非常像** IP 白名单问题，实际不是 —— IP 白名单的码是 99991401，
       而且官方文档明说它**不作用于 access_token 接口**。
       21001 是 type=policy / subtype=access_denied，即权限被数据安全策略拦了。
       我当时差点照 IP 白名单去指方向。

       另一件实测的事：`--push-test` 原来直接把 traceback 甩出来，屏幕上最显眼
       的是 lark_oapi 七八层调用栈，真正有用的那句缩在最后一行。
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "fb_hint", root / "server" / "feishu-bot" / "feishu_bot.py")
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    # 真实的异常长这样（--push-test 那次）：
    real = RuntimeError(
        "obtain self tenant access token failed, code: 21001, msg: "
        "Access denied by access control policy. Contact the administrator "
        "for assistance.")
    got = fb.explain_api_error(real)
    assert "21001" in got, got
    assert "数据安全" in got, got
    # ⚠️ 这条断言是重点：必须**明确否定** IP 白名单，否则下一个人还是会跑偏
    assert "不是 IP 白名单" in got and "99991401" in got, got

    assert "可用范围" in fb.explain_api_error(RuntimeError("code: 230013 x"))
    assert "IP 白名单" in fb.explain_api_error(RuntimeError("code: 99991401 x"))

    # 认不出来的错误不要瞎编一个原因
    assert fb.explain_api_error(RuntimeError("something else entirely")) == ""


check("feishu: 21001 要指向数据安全策略，并**明确否定** IP 白名单",
      t_feishu_api_error_hints)


def t_feishu_live_push():
    """实时题要**尽快单独推**，而且不能打断正在答的那题。

    ⚠️ 实测踩到的缺口：服务器每 30 分钟生成实时题推到中继，但机器人只在每天
       08:30 推一次 —— 于是「实时提问」的实际效果是「第二天早上一次性看到
       一堆昨天的题」。生成和送达是两件事，中间那段当时是断的。

    这个测试钉三件事：
      · 只有问责批时**不推**（它走每天整批）
      · 有实时题时推一条，并记进 sent
      · 正在等答案时**不插队**
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "fb_lp", root / "server" / "feishu-bot" / "feishu_bot.py")
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    box: dict = {"state": {}, "sent": []}
    pending: dict = {"qs": []}
    fb.load_state = lambda: box["state"]
    fb.save_state = lambda s: box.__setitem__("state", s)
    fb.send_text = lambda oid, text: box["sent"].append(text)
    fb.relay_pending = lambda: pending["qs"]
    # ⚠️ 必须把「别打扰时段」也固定住，否则这条测试**按运行时刻时红时绿**：
    #    晚上跑就永远推不出去，白天跑就正常。
    fb.in_quiet_hours = lambda now: False

    day = fb.datetime.now(fb.TZ).date().isoformat()
    laptop = {"id": f"{day}-lap", "day": day, "source": "laptop",
              "format": "yes_no", "text": "问责批的题"}
    live = {"id": f"{day}-liv", "day": day, "source": "live",
            "format": "text", "text": "实时批的题"}

    # 只有问责批 → 不推。它该等 08:30 的整批推送。
    pending["qs"] = [laptop]
    box["state"] = {}
    assert fb.push_next_live("ou_x") is False, "问责批不该被实时循环推出去"
    assert box["sent"] == []

    # 有实时题 → 推且只推一题
    pending["qs"] = [laptop, live]
    assert fb.push_next_live("ou_x") is True
    assert box["state"]["awaiting"] == f"{day}-liv", box["state"]
    assert f"{day}-liv" in box["state"]["sent"]
    assert len(box["sent"]) == 1
    assert "实时" in box["sent"][0], "题干里该标明是实时题"
    assert "问责批的题" not in box["sent"][0], "不该把问责批一起推出去"

    # 还在等答案 → 不插队（哪怕又来了新题）
    live2 = {"id": f"{day}-liv2", "day": day, "source": "live",
             "format": "text", "text": "第二道实时题"}
    pending["qs"] = [laptop, live, live2]
    assert fb.push_next_live("ou_x") is False, "正在等答案时不该插队"
    assert len(box["sent"]) == 1

    # 答完了，但同一题不重复推
    box["state"]["awaiting"] = None
    assert fb.push_next_live("ou_x") is True, "答完之后该推下一道新题"
    assert box["state"]["awaiting"] == f"{day}-liv2"
    assert len(box["sent"]) == 2

    # ⚠️ 「别打扰」时段一律不推 —— 哪怕是当天的题、哪怕没在等答案。
    #    生成器那边有窗口，但推送这一层必须自己再拦一道（见 t_quiet_hours）。
    fb.in_quiet_hours = lambda now: True
    box["state"] = {}
    box["sent"] = []
    pending["qs"] = [live]
    assert fb.push_next_live("ou_x") is False, "夜里不该推消息"
    assert box["sent"] == []

    # ⚠️ 但**收尾题要放行** —— 自动收尾（默认 23:00）就落在静音时段里，
    #    挡住了就等于「到点不收尾」，而实时题过零点就作废。
    summary = {"id": f"{day}-sum9", "day": day, "source": "live",
               "kind": "summary", "format": "text", "text": "收尾题"}
    box["state"] = {}
    pending["qs"] = [summary]
    assert fb.push_next_live("ou_x") is True, "夜间该放行收尾题"
    assert len(box["sent"]) == 1
    fb.in_quiet_hours = lambda now: False

    # ⚠️ 隔了夜的实时题也不推：早上收到「昨天 21:00–22:00 这一段…」只会困惑。
    from datetime import timedelta as _td
    yesterday = (datetime.now(fb.TZ).date() - _td(days=1)).isoformat()
    old = {"id": f"{yesterday}-old", "day": yesterday, "source": "live",
           "format": "text", "text": "昨天那道实时题"}
    box["state"] = {}
    box["sent"] = []
    pending["qs"] = [old]
    assert fb.push_next_live("ou_x") is False, "隔夜的实时题不该在早上推出来"
    assert box["sent"] == []


check("feishu: 实时题尽快单独推（不插队 / 不重复 / 不夹带问责批）",
      t_feishu_live_push)


def t_quiet_hours():
    """夜里不能发消息 —— 而且**生成器和推送层都要拦**。

    ⚠️ 实测的设计漏洞：生成器有 8–22 的窗口，但推送那一层没有。于是
       21:58 生成的题会在 22:03 被收到，或者服务器半夜重启时把待推的题发出去。
       两道闸缺一不可 —— 生成器只管「生不生成」，题生成完还躺着等推送。
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "fb_qh", root / "server" / "feishu-bot" / "feishu_bot.py")
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    base = fb.datetime.now(fb.TZ).replace(minute=0, second=0, microsecond=0)

    def at(h: int) -> bool:
        return fb.in_quiet_hours(base.replace(hour=h))

    fb.QUIET_FROM, fb.QUIET_UNTIL = 22, 8
    for h in (22, 23, 0, 3, 7):
        assert at(h), f"{h} 点该是别打扰时段（跨午夜）"
    for h in (8, 12, 18, 21):
        assert not at(h), f"{h} 点不该被禁"

    # 不跨午夜的时段也要对
    fb.QUIET_FROM, fb.QUIET_UNTIL = 1, 5
    assert at(2) and not at(6)

    # 两端相等 = 不静音（别把人锁死）
    fb.QUIET_FROM = fb.QUIET_UNTIL = 0
    assert not at(3)


check("feishu: 夜间静音（跨午夜 / 不跨午夜 / 两端相等都要对）",
      t_quiet_hours)


def t_summary_questions():
    """晚上收尾：三条题、带 kind=summary、一天一次、到点才发。"""
    import importlib.util

    lq_path = Path(__file__).resolve().parent / "live_questions.py"
    spec = importlib.util.spec_from_file_location("live_q2", lq_path)
    lq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lq)

    now = lq.datetime.now(lq.TZ).replace(minute=0, second=0, microsecond=0)
    start = now - lq.timedelta(hours=1)
    iso = lambda dt: dt.isoformat()          # noqa: E731
    day = now.date().isoformat()

    qs = lq.summary_questions(
        day, [], [],
        {"today": {"total_hours": 6.5,
                   "by_category": [["Work", 3.2], ["Life", 2.1]]}})
    assert len(qs) == 10, qs
    assert all(q["kind"] == "summary" for q in qs), "必须带 kind=summary"
    # ⚠️ 「不能去勾任务」靠的是 ref 为空，**不是** format=text：
    #    心情/体力现在是 1–5 的选择题（见 RATED_DIMENSIONS），它们仍然不该
    #    勾任何任务 —— apply_answers 只看带 ref 的题。
    assert all(q["ref"] == "" for q in qs), "收尾题不能去勾任务"
    rated = [q for q in qs if q.get("dim") in lq.RATED_DIMENSIONS]
    assert len(rated) == 3, [q.get("dim") for q in rated]
    assert {q["dim"] for q in rated} == {"体力", "心情", "睡眠"}, \
        [q["dim"] for q in rated]
    assert all(q["format"] == "choice" for q in rated), [q["format"] for q in rated]
    assert all(len(q.get("options") or []) == 5 for q in rated)
    assert all(str(o["label"])[0].isdigit() for q in rated for o in q["options"]), \
        "选项必须以分数开头 —— collect_labels.py 靠它取数字"
    assert all(q["format"] == "text" for q in qs if q.get("dim") not in
               lq.RATED_DIMENSIONS), "其余常规题仍是自由文本"
    keys = [q["_key"] for q in qs]
    assert len(set(keys)) == 10, f"题号不能重：{keys}"
    assert all(k.startswith(f"summary-{day}") for k in keys), keys
    # 第二题要把当天的真实数字带进去，而不是空泛地问
    assert "6.5" in qs[1]["text"], qs[1]["text"]

    # ⚠️ 数据齐的时候题更多、更具体：电脑 / 手机 / 位置各一题
    rich = lq.summary_questions(
        day,
        [{"place": "望京, 北京朝阳", "minutes": 150.0,
          "start": iso(start), "end": iso(now), "is_coord": False}],
        [{"metric": "unlock", "ts_utc": iso(start)},
         {"metric": "app_switch", "value_text": "com.tencent.mm",
          "ts_utc": iso(start)}],
        {"today": {"total_hours": 6.5,
                   "by_category": [["Work", 3.2]],
                   "top_apps": [["Code.exe", 2.1]]}},
    )
    assert len(rich) == 12, [q["text"] for q in rich]
    assert any("1 unlocks" in q["text"] for q in rich), "phone line missing"
    assert any("望京" in q["text"] for q in rich), "位置那题没出"
    assert any("Code.exe" in q["text"] for q in rich), "电脑那题没带最久的应用"

    # 没有位置/手机数据时，**不能硬问** —— 只问有数据的那几块
    lean = lq.summary_questions(day, [], [], None)
    assert len(lean) == 9, [q["text"] for q in lean]
    assert all("去过" not in q["text"] and "解锁" not in q["text"] for q in lean)

    # ── 常规题：数据看不见的维度 ──
    routines = [q for q in qs if q.get("routine")]
    assert len(routines) == 7, [q["text"] for q in routines]
    # ⚠️ kind 必须仍是 summary —— 收尾流程靠它判断「答完了没有」。
    #    改成 routine 的话，答完第一道常规题就会误判成收尾结束、提前写总结。
    assert all(q["kind"] == "summary" for q in routines), [q["kind"] for q in routines]
    # 七个维度**每天一个不少**
    assert [q["dim"] for q in routines] == [
        "体力", "心情", "人际", "睡眠", "吃饭", "休息", "学习"], \
        [q["dim"] for q in routines]

    # 按天轮换：两天的常规题不该一样（否则连问一周就退化成「还行」）
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    nxt_routines = [q["text"] for q in lq.summary_questions(nxt, [], [], None)
                    if q.get("routine")]
    assert [q["text"] for q in routines] != nxt_routines, "两天的常规题不该一样"

    # 到点判定
    lq.SUMMARY_AT = "21:30"
    lq.SUMMARY_AT = "23:00"
    assert not lq._summary_due(now.replace(hour=22, minute=59))
    assert lq._summary_due(now.replace(hour=23))
    # ⚠️ 收尾前**半小时**停发小时题，不是一小时 —— 原来一小时是从 21:30 那个
    #    收尾时间推出来的；收尾挪到 23:00 之后，一小时会让 22 点那个小时空掉，
    #    而使用者要的是每小时一条。
    assert not lq._summary_soon(now.replace(hour=22, minute=0))
    assert lq._summary_soon(now.replace(hour=22, minute=30))
    assert not lq._summary_soon(now.replace(hour=21))

    # 留空 = 不做收尾
    lq.SUMMARY_AT = ""
    assert not lq._summary_due(now.replace(hour=23))


check("live questions: 收尾三题（kind=summary / 一天一次 / 到点才发）",
      t_summary_questions)


def t_label_extraction():
    """自评答案 → 数值标签：只认带 dim 的 1–5，**绝不猜**自由文本。

    这是以后所有「相关性/预测」的唯一标签来源。错一条会污染整张表，
    而且不会报错 —— 所以这里钉死两件事：
      · 「4 不错」→ 4，备注保留
      · 没有 dim / 答的是「还行」→ 不进标签（不是估成 3 分）
    """
    import importlib.util

    path = Path(__file__).resolve().parent / "collect_labels.py"
    spec = importlib.util.spec_from_file_location("collect_labels_t", path)
    cl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cl)

    for s, want in (("4 不错", 4), ("5", 5), ("1 没电", 1),
                    ("3分，下午开会", 3), ("4、还行", 4)):
        assert cl.score_of(s) == want, (s, cl.score_of(s))
    for s in ("还行", "", "6 很好", "0 很差", "4.5", "十分"):
        assert cl.score_of(s) is None, (s, cl.score_of(s))

    payload = {"qa": [
        {"id": "d-1", "question_day": "2026-10-21", "dim": "心情",
         "answer": "4 不错", "note": "下午有点堵", "ts_utc": "2026-10-21T15:00:00Z"},
        {"id": "d-2", "question_day": "2026-10-21", "dim": "体力",
         "answer": "2 偏低", "note": "", "ts_utc": "2026-10-21T15:00:02Z"},
        # 不是打分维度 → 不进标签
        {"id": "d-3", "question_day": "2026-10-21", "dim": "人际",
         "answer": "4 不错", "ts_utc": "2026-10-21T15:00:03Z"},
        # 打分维度但答的是自由文本 → 不进标签（不估分）
        {"id": "d-4", "question_day": "2026-10-21", "dim": "心情",
         "answer": "还行", "ts_utc": "2026-10-21T15:00:04Z"},
        # 睡眠也是打分维度
        {"id": "d-5", "question_day": "2026-10-21", "dim": "睡眠",
         "answer": "5 很好", "note": "一觉到天亮", "ts_utc": "2026-10-21T15:00:05Z"},
        # 老数据没有 dim → 不进标签
        {"id": "d-6", "question_day": "2026-10-20",
         "answer": "4 不错", "ts_utc": "2026-10-20T15:00:00Z"},
    ]}
    rows = cl.extract(payload)
    assert {r["qid"] for r in rows} == {"d-1", "d-2", "d-5"}, rows
    mood = next(r for r in rows if r["dim"] == "心情")
    assert mood["score"] == 4 and mood["note"] == "下午有点堵", mood
    assert mood["day"] == "2026-10-21", mood


check("labels: 自评 1–5 → labels.jsonl，自由文本不估分",
      t_label_extraction)


def t_reflex_rules():
    """反射弧：只在该响的时候响 —— 每条一天一次、60 分钟间隔、答题时不打断。

    这些规则是**主动打扰**。误报的代价不是一条错消息，是你把整条通道
    静音 —— 所以边界（静音时段 / awaiting / 快照陈旧）必须被钉住。
    """
    import importlib.util
    from datetime import datetime as dt
    from datetime import timedelta as td
    from zoneinfo import ZoneInfo

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "reflex_t", root / "server" / "feishu-bot" / "reflex.py")
    rx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rx)

    tz = ZoneInfo("Asia/Shanghai")
    day = "2026-10-21"
    iso = lambda d: d.astimezone(tz).isoformat()  # noqa: E731

    # 01:00，手机 5 分钟前还在用 → 熬夜
    late = dt(2026, 10, 21, 1, 0, tzinfo=tz)
    ev = [{"metric": "unlock", "ts_utc": iso(late - td(minutes=5))}]
    got = rx.evaluate(late, day, events=ev, phone=None, snap=None, state={})
    assert [a["key"] for a in got] == ["late_night"], got

    # 同一晚已经说过一次 → 不再说
    got = rx.evaluate(late, day, events=ev, phone=None, snap=None,
                      state={"day": day, "fired": {"late_night": iso(late)}})
    assert got == [], got

    # 手机 40 分钟没动 → 不算「还在用」
    ev_idle = [{"metric": "unlock", "ts_utc": iso(late - td(minutes=40))}]
    assert rx.evaluate(late, day, events=ev_idle, phone=None, snap=None,
                       state={}) == []

    # 正在等答案 → 一句话都不说（会被当成答案）
    assert rx.evaluate(late, day, events=ev, phone=None, snap=None,
                       state={}, awaiting=True) == []

    phone = {"screen_minutes": 400.0, "top_apps": [("WeChat", 200.0),
                                                    ("Code", 60.0)]}
    evening = dt(2026, 10, 21, 19, 0, tzinfo=tz)
    # 亮屏 6h40 + 微信 3h20：先报屏幕（一次只报一条）
    got = rx.evaluate(evening, day, events=[], phone=phone, snap=None, state={})
    assert [a["key"] for a in got] == ["screen_heavy"], got
    assert "6h" in got[0]["text"], got[0]

    # 屏幕没超、微信超了 → 报娱乐
    phone2 = {"screen_minutes": 120.0, "top_apps": [("WeChat", 200.0)]}
    got = rx.evaluate(evening, day, events=[], phone=phone2, snap=None, state={})
    assert [a["key"] for a in got] == ["leisure_heavy"], got
    assert "WeChat" in got[0]["text"], got[0]

    # 60 分钟内已经发过一条 → 这一轮全静音
    got = rx.evaluate(evening, day, events=[], phone=phone2, snap=None,
                      state={"day": day, "fired": {},
                             "last_at": iso(evening - td(minutes=30))})
    assert got == [], got

    # 电脑快照：新鲜 + 连续 200 分钟 → 久坐
    snap = {"pushed_at": iso(evening - td(minutes=5)),
            "current": {"app": "Code.exe", "minutes": 200, "category": "Work"}}
    got = rx.evaluate(evening, day, events=[], phone=None, snap=snap, state={})
    assert [a["key"] for a in got] == ["long_desktop"], got

    # 快照 2 小时没更新 → 不拿它判断「现在」
    stale = {"pushed_at": iso(evening - td(hours=2)),
             "current": {"app": "Code.exe", "minutes": 200}}
    assert rx.evaluate(evening, day, events=[], phone=None, snap=stale,
                       state={}) == []

    # 静音时段（23:30）：屏幕/久坐都不说话
    night = dt(2026, 10, 21, 23, 30, tzinfo=tz)
    assert rx.evaluate(night, day, events=[], phone=phone, snap=snap,
                       state={}) == []

    # 传感器哑了：14:00，最后一条手机活动在 00:30（13.5 小时前）→ 报
    afternoon = dt(2026, 10, 21, 14, 0, tzinfo=tz)
    stale = [{"metric": "unlock",
              "ts_utc": iso(dt(2026, 10, 21, 0, 30, tzinfo=tz))}]
    got = rx.evaluate(afternoon, day, events=stale, phone=None, snap=None,
                      state={})
    assert [a["key"] for a in got] == ["sensor_silent"], got
    # 10 小时前还在用 → 不报（长睡不能被当成故障）
    fresh = [{"metric": "unlock", "ts_utc": iso(afternoon - td(hours=10))}]
    assert rx.evaluate(afternoon, day, events=fresh, phone=None, snap=None,
                       state={}) == []
    # 一条事件都没有 → 不报（多半是刚部署，不吓人）
    assert rx.evaluate(afternoon, day, events=[], phone=None, snap=None,
                       state={}) == []


check("reflex: 熬夜/亮屏/娱乐/久坐 —— 边界、冷却、答题时不打断",
      t_reflex_rules)


def t_reflex_outcomes():
    """反馈回路：提醒发出后 +15/+60 分钟各记一次行为，窗口没到不提前记。

    ⚠️ 这是**观测**，不是评价。这里只钉住计数和边界 —— 比如「窗口外的事件
       不能被算进来」，否则以后会拿错窗口的数据去证明「提醒有效」。
    """
    import importlib.util
    from datetime import datetime as dt
    from datetime import timedelta as td
    from zoneinfo import ZoneInfo

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "reflex_out", root / "server" / "feishu-bot" / "reflex.py")
    rx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rx)

    tz = ZoneInfo("Asia/Shanghai")
    base = dt(2026, 10, 21, 19, 0, tzinfo=tz)
    iso = lambda d: d.astimezone(tz).isoformat()  # noqa: E731

    alert = {"kind": "sent", "id": "x1", "rule": "screen_heavy", "ts": iso(base)}
    events = [
        {"metric": "unlock", "ts_utc": iso(base + td(minutes=5))},
        {"metric": "app_switch", "ts_utc": iso(base + td(minutes=10))},
        {"metric": "screen_off", "ts_utc": iso(base + td(minutes=12))},
        {"metric": "unlock", "ts_utc": iso(base + td(minutes=40))},
        # 窗口外的，不能被算进来
        {"metric": "unlock", "ts_utc": iso(base - td(minutes=5))},
        {"metric": "unlock", "ts_utc": iso(base + td(minutes=75))},
    ]

    # 15 分钟还没到 → 什么都不记
    assert rx.pending_outcomes([alert], base + td(minutes=10), events) == []

    rows = rx.pending_outcomes([alert], base + td(minutes=20), events)
    assert len(rows) == 1 and rows[0]["window_min"] == 15, rows
    r15 = rows[0]
    assert (r15["unlocks"], r15["switches"], r15["screen_off"], r15["activity"]) \
        == (1, 1, 1, 3), r15

    rows = rx.pending_outcomes([alert], base + td(minutes=65), events)
    assert sorted(r["window_min"] for r in rows) == [15, 60], rows
    r60 = next(r for r in rows if r["window_min"] == 60)
    assert (r60["unlocks"], r60["switches"], r60["screen_off"], r60["activity"]) \
        == (2, 1, 1, 4), r60

    # 记过的不再记（重启/多轮 tick 都不会重复写同一个窗口）
    log = [alert] + rows
    assert rx.pending_outcomes(log, base + td(minutes=90), events) == []

    # 时间解析不出来 → 不记，也不崩
    bad = {"kind": "sent", "id": "x2", "rule": "x", "ts": "not-a-time"}
    assert rx.pending_outcomes([bad], base + td(hours=3), events) == []


check("reflex: 提醒后的 +15/+60 观察只记一次、窗口外不算",
      t_reflex_outcomes)


def t_reflex_awaiting_gate():
    """「刚发出的题」才拦反射弧；挂了一整晚的旧题不能把凌晨提醒也拦掉。

    ⚠️ awaiting 只会被下一条用户消息清掉。昨晚 23:00 那道没答的题会一直
       挂着 —— 如果只看「awaiting 非空」，熬夜提醒就永远发不出去。
       所以判据是「这题什么时候发的」：1 小时内才安静。
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "fb_reflex", root / "server" / "feishu-bot" / "feishu_bot.py")
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    calls = {"events": 0, "sent": 0}
    originals = (fb.OWNER_OPEN_ID, fb.live.relay_events, fb.live.relay_desktop,
                 fb.live.deps, fb.send_text, fb.load_state)
    try:
        fb.OWNER_OPEN_ID = "ou_x"
        fb.live.relay_events = lambda *a, **k: (
            calls.__setitem__("events", calls["events"] + 1) or [])
        fb.live.relay_desktop = lambda *a, **k: None
        fb.live.deps = lambda: (_ for _ in ()).throw(RuntimeError("不测这块"))
        fb.send_text = lambda *a, **k: calls.__setitem__("sent", calls["sent"] + 1)
        fb.load_reflex_state = lambda: {}
        fb.save_reflex_state = lambda s: None

        now = fb.datetime.now(fb.TZ)
        qid = now.date().isoformat() + "-abcd1234"

        # ① 题刚发出去 5 分钟 → 不插话，也不去拉数据
        fb.load_state = lambda: {"awaiting": qid, "sent": {
            qid: (now - fb.timedelta(minutes=5)).isoformat(timespec="seconds")}}
        fb.reflex_tick()
        assert calls["events"] == 0, "刚发出的题在等答案，不该去拉数据"

        # ② 同一道题挂了 3 小时 → 照常检查（昨晚的题不该挡住凌晨提醒）
        fb.load_state = lambda: {"awaiting": qid, "sent": {
            qid: (now - fb.timedelta(hours=3)).isoformat(timespec="seconds")}}
        fb.reflex_tick()
        assert calls["events"] >= 1, "旧题不该拦住反射弧"
    finally:
        (fb.OWNER_OPEN_ID, fb.live.relay_events, fb.live.relay_desktop,
         fb.live.deps, fb.send_text, fb.load_state) = originals


check("reflex: 只拦「一小时内的题」，挂了一晚的旧题不挡提醒",
      t_reflex_awaiting_gate)


def t_features():
    """状态向量：字段来自已算好的 timeline，缺的源**不补 0**。

    这一层以后会喂给相关性和模型。补 0 的话，「那天没开电脑」和「那天电脑
    数据没采到」在模型眼里会变成同一件事 —— 而这两件事的含义正相反。
    """
    import features

    tl = {
        "day": "2026-10-20", "total_hours": 4.76,
        "by_category": {"Leisure": 2.17, "Work": 2.06},
        "health": {"steps": 4426, "heart_rate_avg": 79.3},
        "location": [{"place": "A", "minutes": 434.0},
                     {"place": "B", "minutes": 270.6}],
        "phone": {"unlocks": 109, "app_switches": 937,
                  "top_apps": [["WeChat", 195.0], ["Pinduoduo", 73.9],
                               ["DeepSeek", 53.5]],
                  "screen_minutes": 597.7, "foreground_minutes": 543.1},
        "web": [{"site": "chat.deepseek.com", "minutes": 166.6},
                {"site": "other", "minutes": 25.9}],
        "track": {"points": 1294, "span_km": 42.0},
        "blocks": [
            {"time": "17:41-17:47", "hours": 0.08, "app": "Weixin.exe"},
            {"time": "23:10-23:40", "hours": 0.5, "app": "Weixin.exe"},
            {"time": "09:00-10:00", "hours": 1.0, "app": "Code.exe"},
            {"time": "坏时间", "hours": 0.2, "app": "Code.exe"},
        ],
    }
    f = features.features_for("2026-10-20", tl, {"mood": 4, "energy": 2})
    assert f["feature_version"] == features.FEATURE_VERSION, f
    assert f["desktop"]["hours"] == 4.76, f["desktop"]
    # 时间解析不出来的块：时长照算，但**不**算进「深夜使用」
    assert f["desktop"]["top_app"] == "Code.exe", f["desktop"]
    assert f["desktop"]["top_app_hours"] == 1.2, f["desktop"]
    assert f["desktop"]["longest_block_hours"] == 1.0, f["desktop"]
    assert f["desktop"]["late_hours"] == 0.5, f["desktop"]
    assert f["phone"]["screen_hours"] == 9.96, f["phone"]
    assert f["phone"]["leisure_top10_minutes"] == 268.9, f["phone"]
    assert f["location"]["stays"] == 2 and f["location"]["stayed_hours"] == 11.74, \
        f["location"]
    assert f["web"]["minutes"] == 192.5 and f["web"]["top_site"] == "chat.deepseek.com"
    assert f["labels"] == {"mood": 4, "energy": 2}, f["labels"]
    assert all(f["sources"].values()), f["sources"]

    # 什么都没采到的一天：null / False，不能是 0 或空字符串冒充「没有」
    empty = features.features_for("2026-10-11", {"day": "2026-10-11"}, {})
    assert empty["phone"] is None and empty["health"] is None, empty
    assert empty["sources"]["phone"] is False and empty["sources"]["labels"] is False
    assert empty["desktop"]["hours"] == 0.0, empty["desktop"]

    # 自评维度 → 英文键的映射，必须覆盖打分表里的维度
    import live_questions as lq
    assert set(lq.RATED_DIMENSIONS) <= set(features.LABEL_KEYS), \
        f"打分维度没有特征键：{set(lq.RATED_DIMENSIONS) - set(features.LABEL_KEYS)}"

    # 娱乐名单两边必须一致（features 从 normalize 取，reflex 是服务器副本）
    import importlib.util
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "reflex_leisure", root / "server" / "feishu-bot" / "reflex.py")
    rx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rx)
    import normalize
    assert rx.LEISURE_APPS == normalize.PHONE_LEISURE_APPS, \
        "娱乐应用名单漂了：reflex 和 normalize 必须一致"


check("features: 一天一行状态向量 / 缺源不补 0 / 版本号 / 名单不漂",
      t_features)


def t_baseline():
    """个人基线：中位数 + MAD，样本不足/零波动就闭嘴。

    这条的失败方式不是报错，是**给一个看起来很像真的偏离**。所以这里钉住：
      · 今天不参与自己的基线
      · 少于是 min_days 个历史值 → 不算
      · MAD=0（过去每天一样）→ 不算「反常」
    """
    import baseline

    def row(day, screen, steps=None):
        return {"day": day,
                "phone": {"screen_hours": screen},
                "health": {"steps": steps} if steps is not None else None}

    rows = [row(f"2026-10-{d:02d}", s) for d, s in [
        (10, 5.0), (11, 6.0), (12, 7.0), (13, 6.0), (14, 5.0),
        (15, 7.0), (16, 10.0)]]

    out = baseline.deviations(rows, "2026-10-16", min_days=5)
    assert out["baseline_days"] == 6, out
    flagged = {d["key"]: d for d in out["flagged"]}
    assert "phone_screen_hours" in flagged, out
    d = flagged["phone_screen_hours"]
    # 过去 6 天中位数 6、MAD 1 → z = 0.6745 * 4 ≈ 2.7
    assert d["median"] == 6.0 and d["z"] == 2.7, d
    assert "高于" in baseline.render(out, show_all=False)

    # 今天不能出现在自己的基线里：把今天改成 100 也不该改变中位数
    rows[-1] = row("2026-10-16", 100.0)
    out = baseline.deviations(rows, "2026-10-16", min_days=5)
    assert next(d for d in out["flagged"]
                if d["key"] == "phone_screen_hours")["median"] == 6.0

    # 历史不足 → 进 insufficient，不硬算
    out = baseline.deviations(rows[:3], "2026-10-12", min_days=5)
    assert out["flagged"] == [] and "手机亮屏" in out["insufficient"], out

    # 过去每天一样（MAD=0）→ 不算反常
    flat = [row(f"2026-10-{d:02d}", 6.0) for d in (10, 11, 12, 13, 14, 15)]
    flat.append(row("2026-10-16", 10.0))
    out = baseline.deviations(flat, "2026-10-16", min_days=5)
    assert "手机亮屏" in out["no_variance"], out

    # 目标日不在文件里 → 明确报错，不拿别的日子凑
    out = baseline.deviations(rows, "2026-11-01")
    assert out.get("error"), out


check("baseline: 中位数/MAD / 今天不进自己的基线 / 样本不足与零波动不硬算",
      t_baseline)


def t_push_health_and_render():
    """健康汇总：坏的 meta 不崩、非数字不编；渲染必须说清「这是哪一天的」。

    ⚠️ Gadgetbridge 通常隔天才导出，所以「今天走了多少步」最常见的正确
       答案是「这是昨天的」。日期不说清，用户会把昨天的步数当成今天的。
    """
    import push_health as ph
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(CFG["timezone"])
    rows = [
        ("steps", 4426.0, "", "{}"),
        ("heart_rate_avg", 79.3, "", '{"min":61,"max":150}'),
        ("heart_rate_min", 61.0, "", "{}"),
        ("workout", 20.0, "跑步",
         '{"distance_km":4.2,"avg_hr":171,"end_utc":"2026-10-20T10:00:00Z"}'),
        ("workout", 5.0, "骑行", "not json"),     # 坏 meta 不能崩
        ("vo2max", "bad", "", "{}"),              # 非数字 → 跳过
    ]
    p = ph.build_payload(rows, "2026-10-20", dt(2026, 10, 21, 10, tzinfo=tz))
    assert p["day"] == "2026-10-20" and p["pushed_at"]
    assert p["health"]["steps"] == 4426.0 and p["health"]["heart_rate_avg"] == 79.3
    assert "vo2max" not in p["health"], "非数字不能被编成指标"
    assert {w["kind"] for w in p["workouts"]} == {"跑步", "骑行"}, p["workouts"]

    import importlib.util
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "live_watch", root / "server" / "feishu-bot" / "live.py")
    lv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lv)

    snap = {
        "day": "2026-10-20",
        "pushed_at": "2026-10-21T08:00:00+08:00",
        "health": {"steps": 4426, "heart_rate_avg": 79.3, "heart_rate_min": 61},
        "workouts": [{"kind": "跑步", "minutes": 20, "distance_km": 4.2,
                      "avg_hr": 171}],
    }
    lv.relay_watch = lambda b, k: snap
    out = lv.render_watch("2026-10-21", "x", "k")
    assert "4426" in out and "79" in out, out
    assert "sleep        —" in out, f"missing sleep must be a dash: {out!r}"
    assert "跑步" in out and "4.2 km" in out, out
    assert "2026-10-20" in out, "隔天的汇总必须在标题里带日期"

    # 同一天 + 有睡眠样本 → 正常显示，不报「不是今天」
    fresh = dict(snap, day="2026-10-21", pushed_at=dt.now(tz).isoformat(),
                 health=dict(snap["health"], sleep_hours=7.5))
    lv.relay_watch = lambda b, k: fresh
    out = lv.render_watch("2026-10-21", "x", "k")
    assert "7h 30m" in out and "不是今天" not in out, out

    # 没推过 → 给一条能照着修的提示
    lv.relay_watch = lambda b, k: None
    assert lv.render_watch("2026-10-21", "x", "k") == lv.WATCH_NO_DATA

    # 中继出错 → LiveUnavailable（调用方会退回那句兜底）
    def boom(b, k):
        raise RuntimeError("relay down")
    lv.relay_watch = boom
    try:
        lv.render_watch("2026-10-21", "x", "k")
    except lv.LiveUnavailable:
        pass
    else:
        raise AssertionError("中继出错时应该抛 LiveUnavailable")


check("health: 快照构建不编数 / 隔天要说清 / 睡眠缺失不写 0",
      t_push_health_and_render)


def t_recall():
    """服务器端历史检索：中文分词、AND→OR、新文件会触发重建。

    ⚠️ 这是「上次觉得累」这类问题的唯一数据来源。它静默返回空的结果，
       比报错更常见 —— 所以这里钉住「该命中的必须命中」「不该命中的不许
       靠一个字蒙进来」。
    """
    import importlib.util
    import json
    import tempfile

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "recall_t", root / "server" / "feishu-bot" / "recall.py")
    rc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc)

    with tempfile.TemporaryDirectory() as td:
        state = Path(td)
        db = state / "memory.db"

        def write(day: str, body: str) -> None:
            (state / f"journal-output-{day}.json").write_text(
                json.dumps({"day": day, "body": body}, ensure_ascii=False),
                encoding="utf-8")

        def write_qa(day: str, rows: list[dict], questions: dict) -> None:
            (state / f"questions-live-{day}.json").write_text(
                json.dumps({"day": day, "questions": [
                    {"id": k, "text": v} for k, v in questions.items()]},
                    ensure_ascii=False), encoding="utf-8")
            (state / f"phone-{day}.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                encoding="utf-8")

        # 真实日记正文是**英文**的（close_day 的产出）—— 故意用英文，
        # 这样能测出「中文问题靠自己的中文回答命中」这条链路。
        write("2026-10-18", "Met Lao Wang, discussed an outsourcing project. "
                            "Very tired at night.")
        write("2026-10-19", "Read a book at home.")
        write_qa("2026-10-18", [
            {"domain": "question", "metric": "answer",
             "value_text": "2026-10-18-aaaa|很累，下午就撑不住了|"},
            {"domain": "manual", "metric": "note",
             "value_text": "今天见了老王，聊了外包项目"},
            # 机器人自己写的整份复盘：必须跳过，否则每天都命中一堆词
            {"domain": "manual", "metric": "note",
             "value_text": "【今日复盘】今天打橄榄球很开心"},
        ], {"2026-10-18-aaaa": "今天体力怎么样？"})

        hits = rc.search("外包", state_dir=state, db=db)
        assert hits and hits[0]["day"] == "2026-10-18", hits
        assert any(h["kind"] == "qa" for h in hits), hits
        assert "外包" in hits[0]["excerpt"] or "老王" in hits[0]["excerpt"], hits

        # 摘要里的汉字不能被切成「今 天 见 了」
        assert any("今天见了老王" in h["excerpt"] for h in hits), hits

        # 中文自然语言问句：靠中文回答命中英文日记对应的那一天
        hits = rc.search("我上次觉得累是什么时候", state_dir=state, db=db)
        assert hits and hits[0]["day"] == "2026-10-18", hits
        hits = rc.search("我上次聊外包项目是什么时候", state_dir=state, db=db)
        assert hits and hits[0]["day"] == "2026-10-18", hits

        # 复盘笔记被跳过：它的内容不能被检索到
        assert rc.search("橄榄球", state_dir=state, db=db) == []

        # 没有的词 → 空，不猜
        assert rc.search("不存在的词", state_dir=state, db=db) == []
        assert rc.search("", state_dir=state, db=db) == []

        # 新的一天写进来 → 下一次检索前自动重建
        write("2026-10-20", "Went running after class.")
        write_qa("2026-10-20", [
            {"domain": "manual", "metric": "note", "value_text": "今天去跑步了，跑完很累"},
        ], {})
        hits = rc.search("跑步", state_dir=state, db=db)
        assert hits and hits[0]["day"] == "2026-10-20", hits

        # ⚠️ 只追加 phone-*.jsonl（journal 不动）也必须触发重建 ——
        #    只盯 journal 的指纹会让新回答永远进不了索引，且不报错。
        with (state / "phone-2026-10-20.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"domain": "manual", "metric": "note",
                                "value_text": "晚上读了一本讲火箭的书"},
                               ensure_ascii=False) + "\n")
        hits = rc.search("火箭", state_dir=state, db=db)
        assert hits and hits[0]["day"] == "2026-10-20", hits


check("recall: 中文检索 / AND→OR / 新日记自动进索引", t_recall)


def t_freeform_history():
    """自由问答要**带上检索到的历史**，而且找不到时不能附一个空标题。

    历史召回是「外置大脑」的核心：模型必须能引用过去的日记，并且说清
    是哪一天；没有记录时要说没有，不能拿今天的数据冒充过去。
    """
    import importlib.util
    import tempfile

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "live_hist", root / "server" / "feishu-bot" / "live.py")
    lv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lv)

    # answer_freeform 里是 `import recall` —— 把假模块塞进 sys.modules
    rc_spec = importlib.util.spec_from_file_location(
        "recall", root / "server" / "feishu-bot" / "recall.py")
    rc = importlib.util.module_from_spec(rc_spec)
    rc_spec.loader.exec_module(rc)
    sys.modules["recall"] = rc

    captured: list[str] = []
    originals = (lv.cfg, lv._digest, lv._run_llm_turn, rc.search)
    try:
        with tempfile.TemporaryDirectory() as td:
            lv.cfg = lambda: {"state_dir": td}
            lv._digest = lambda d, b, k: "⟨今天的数据⟩"
            lv._run_llm_turn = lambda conf, counter, day, **kw: (
                captured.append(kw["user"]) or ("好的", ""))
            rc.search = lambda q, k: [
                {"day": "2026-10-18", "excerpt": "〔外包〕项目聊到很晚，晚上很累"}]
            lv.answer_freeform("我上次觉得累是什么时候",
                               day_iso="2026-10-21", base="x", key="k")
        assert "Relevant past records" in captured[0], captured[0]
        assert "2026-10-18" in captured[0] and "外包" in captured[0], captured[0]

        # 检索为空 → 不附这一节（空标题会被模型读成「必须引用历史」）
        rc.search = lambda q, k: []
        captured.clear()
        with tempfile.TemporaryDirectory() as td:
            lv.cfg = lambda: {"state_dir": td}
            lv.answer_freeform("我上次觉得累是什么时候",
                               day_iso="2026-10-21", base="x", key="k")
        assert "Relevant past records" not in captured[0], captured[0]
    finally:
        (lv.cfg, lv._digest, lv._run_llm_turn, rc.search) = originals


check("freeform: 历史召回进提示词 / 找不到时不附空标题",
      t_freeform_history)


def t_notes_into_journal():
    """飞书里记的一笔，必须真的**流进日记提示词** —— 否则存了等于白存。

    链路：机器人 → 中继 /ingest/phone（domain=manual）→ 笔记本拉取 →
    events.db → normalize.attach_other_domains → Timeline.notes →
    generate_journal.build_user_prompt。

    这里锁住最后两跳：Timeline 有 notes 字段、提示词里有它们、而且是
    单独一节并明确「优先于推断」。
    """
    from common import Day
    from generate_journal import build_user_prompt
    from normalize import Timeline, render_markdown

    day = Day.parse("2026-10-16", ZoneInfo(CFG["timezone"]))
    note = {"ts": "2026-10-16T06:00:00+00:00", "metric": "note",
            "text": "今天见了老王，聊了项目"}

    # 提示词
    tl = {"blocks": [], "notes": [note], "health": {}, "location": [],
          "phone": {}, "web": [], "by_category": {}, "total_hours": 0}
    prompt = build_user_prompt(tl, day, [])
    assert "今天见了老王" in prompt, "笔记没进提示词"
    assert "first-hand facts" in prompt, "要明确告诉模型：笔记优先于推断"
    assert "[14:00]" in prompt, f"笔记该带本地时间（UTC 06:00 → 北京 14:00）：{prompt}"

    # vault 的 timeline 页
    md = render_markdown(Timeline(day="2026-10-16", notes=[note]))
    assert "今天见了老王" in md, "笔记没进 vault 的时间轴页"


check("notes: 飞书记的一笔真的进了日记提示词和 vault",
      t_notes_into_journal)


def t_app_map_learning():
    """学到的「应用 → 分类」：查得到、不覆盖人工修正、分类时和规则同等待遇。

    ⚠️ 这里锁的是「学习」最容易出问题的地方 —— 它**静默**。
       学错一个分类，从此那段时间永远算错，而且没有人会再检查。
       所以：手改过的条目绝不能被自动学习盖回去。
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "app_map_t", root / "scripts" / "app_map.py")
    am = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(am)
    import normalize as nz

    app = "eldenring.exe"
    assert am.lookup(app) is None, "还没学过，不该查得到"

    assert am.remember(app, "Leisure", sample_titles=["ELDEN RING"],
                       minutes_seen=42.0) is True
    # 大小写不敏感（Windows 进程名）
    assert am.lookup("EldenRing.EXE") == "Leisure"
    # ⚠️ 已存在的不覆盖 —— 人工修正比模型判的可信
    assert am.remember(app, "Work", sample_titles=["x"], minutes_seen=1) is False
    assert am.lookup(app) == "Leisure", "自动学习把人工修正盖掉了"

    # 分类时和手写规则同等待遇，置信度标 learned
    cat, conf = nz.classify(app, "ELDEN RING")
    assert (cat, conf) == ("Leisure", "learned"), (cat, conf)

    # 手写规则仍然优先、仍然标 rule
    assert nz.classify("Code.exe", "normalize.py")[1] == "rule"
    # 没学过的未知应用还是 default
    assert nz.classify("totally-unknown.exe", "") == ("Other", "default")

    # 学习器的输出解析
    import learn_apps as la
    assert la.parse_category("Leisure") == "Leisure"
    assert la.parse_category("  work\n") == "Work"
    assert la.parse_category("Other") == "Other"
    assert la.parse_category("我看不出来") is None


check("app_map: 学到的分类能用 / 不覆盖人工修正 / 未知仍是 default",
      t_app_map_learning)


def t_memory_retrieval():
    """长期记忆的检索层 —— 重点钉**中文两字词**能不能查到。

    ⚠️ 实测过的坑：
      · FTS5 默认的 unicode61 把一整串汉字当一个 token → 搜「外包」命中 0。
      · trigram 只对 **≥3 个字符**有效 → 搜「外包」「熬夜」仍然命中 0。
        而中文里有意义的词大多正是两个字。
      · 逐字切分 + unicode61 才能命中两字词 —— 这是零依赖下唯一靠谱的做法。

    另一个坑：检索词是从今天的多个信号拼出来的，**AND 起来几乎永远命中 0**，
    所以必须先 AND、失败再 OR。
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    tmpdir = Path(tempfile.mkdtemp(prefix="lifelogging-mem-"))
    os.environ["LIFELOGGING_MEMORY_DB"] = str(tmpdir / "mem.db")
    spec = importlib.util.spec_from_file_location(
        "memory_t", root / "scripts" / "memory.py")
    mem = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mem)

    # ── 切分与查询构造 ──
    assert mem.seg("外包") == " 外  包 ", repr(mem.seg("外包"))
    assert '"外 包"' in mem.to_fts_query("外包"), mem.to_fts_query("外包")

    # ── 建索引（手工写入两篇，不碰真实 vault / 事件表）──
    conn = mem.connect()
    mem.ensure_schema(conn)
    docs = [
        ("journal:2026-01-01", "2026-01-01", "journal",
         "今年少接外包，把时间留给自己的项目。"),
        ("note:2026-02-02", "2026-02-02", "note",
         "又熬夜到两点，因为白天被杂事打断。"),
    ]
    for key, day, kind, text in docs:
        conn.execute("INSERT OR REPLACE INTO memory_docs"
                     " (doc_key, day, kind, text, source) VALUES (?,?,?,?,?)",
                     (key, day, kind, text, "test"))
        conn.execute("INSERT INTO memory_fts (doc_key, day, kind, seg)"
                     " VALUES (?,?,?,?)", (key, day, kind, mem.seg(text)))
    conn.commit()
    conn.close()

    # ⚠️ 核心断言：**两个字的词**必须查得到
    hits = mem.search("外包", 5)
    assert [h["doc_key"] for h in hits] == ["journal:2026-01-01"], hits
    hits = mem.search("熬夜", 5)
    assert [h["doc_key"] for h in hits] == ["note:2026-02-02"], hits

    # 两个词 AND 起来没有共同文档 → 必须退回 OR，两个都召回
    hits = mem.search("外包 熬夜", 5)
    assert len(hits) == 2, f"AND 命中 0 时该退回 OR：{hits}"

    # 查不到就是空，不猜
    assert mem.search("完全不存在的词", 5) == []


check("memory: 中文两字词查得到（逐字切分）/ AND 空时退回 OR",
      t_memory_retrieval)


def t_weekly_review_parse():
    """周整合：**没有依据的事实必须被丢掉**。

    ⚠️ 这是这条链上最重要的一道闸。一条「感觉你最近很累」这种没有依据的断言
       一旦进了周回顾，下个月的画像会继承它，再往下的日记又会继承画像 ——
       错误被一层层放大，而且越来越难发现。

    同时钉住周的边界：只处理**已经过完**的 ISO 周。
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "wr_t", root / "scripts" / "weekly_review.py")
    wr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wr)

    # ── 周的计算 ──
    sat = date(2026, 10, 17)
    y, w = wr.last_completed_week(sat)
    mon, sun = wr.week_bounds(y, w)
    assert (mon, sun) == (date(2026, 10, 5), date(2026, 10, 11)), (mon, sun)
    assert wr.week_key(mon) == f"{y}-W{w:02d}"
    assert wr.parse_week_key("2026-W42") == (2026, 42)

    # ── 解析：该留的留、该丢的丢 ──
    raw = json.dumps({
        "prose": "这周主要在搭 LifeLogging。",
        "facts": [
            {"kind": "project", "text": "在搭 LifeLogging",
             "days": ["2026-10-13", "2026-10-14"]},
            {"kind": "sleep", "text": "经常两点睡",
             "days": ["2026-09-01"]},          # 不在本周 → 丢
            {"kind": "person", "text": "见了老王"},  # 没 days → 丢
            {"kind": "bogus", "text": "x",
             "days": ["2026-10-13"]},          # kind 非法 → 丢
        ],
    }, ensure_ascii=False)

    prose, facts, dropped = wr.parse_review(
        raw, {"2026-10-13", "2026-10-14"})
    assert prose.startswith("这周"), prose
    assert len(facts) == 1, facts
    assert facts[0]["days"] == ["2026-10-13", "2026-10-14"]
    assert dropped == 3, f"该丢掉 3 条无依据的，实际 {dropped}"

    # 带代码围栏的输出也要能解析
    fenced = "```json\n" + raw + "\n```"
    assert wr.parse_review(fenced, {"2026-10-13"})[0].startswith("这周")

    # 渲染出的 markdown 必须带依据
    md = wr.render_markdown("2026-W42", date(2026, 10, 12), date(2026, 10, 18),
                            "散文", facts, 0.01)
    assert "在搭 LifeLogging" in md and "10-13" in md, md
    assert "2026-W42" in md


check("weekly: 没有依据的事实被丢掉 / 周一~周日边界正确 / 渲染带出处",
      t_weekly_review_parse)


def t_day_recap():
    """答完之后返回「今日复盘」：数据 + 你的回答 + 总结。

    ⚠️ 数据和回答是**确定性拼的**，只有总结走模型。所以即使 LLM 挂了，
       复盘里仍然有今天的事实和你自己说的话 —— 那才是主体。
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "live_recap", root / "server" / "feishu-bot" / "live.py")
    lv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lv)

    lv.data_snapshot = lambda d, b, k: ["Computer: 9.5h (Work 6h)",
                                        "Phone: 16 unlocks"]
    qa = [("今天体力怎么样？", "还行"), ("今天学到了什么？", "SQLite FTS5")]

    sections = {"summary": "今天主要在写代码。",
                "did": ["把飞书机器人的收尾题改成七个维度"],
                "advice": ["明天 15:00 前把 AE 的安装验证掉"]}
    out = lv.day_recap("2026-10-17", "http://x", "k", qa, sections)
    assert "[Data]" in out and "Computer: 9.5h" in out, out
    assert "[Your answers]" in out and "SQLite FTS5" in out, out
    assert "[Summary]" in out and "写代码" in out, out
    assert "[What you did]" in out and "七个维度" in out, out
    assert "[Suggestions for tomorrow]" in out and "1. 明天 15:00" in out, out

    # 模型什么都没给时，数据和回答照样要发
    out2 = lv.day_recap("2026-10-17", "x", "k", qa, None)
    assert "[Data]" in out2 and "[Your answers]" in out2, out2
    assert "[Summary]" not in out2 and "[Suggestions for tomorrow]" not in out2, out2

    # 解析：正常 JSON / 带围栏 / 没按 JSON 回（原文当总结，不丢内容）
    p = lv.parse_day_summary('{"summary":"s","did":["a","b"],"advice":["x"]}')
    assert p == {"summary": "s", "did": ["a", "b"], "advice": ["x"]}, p
    assert lv.parse_day_summary("```json\n{\"summary\":\"s\"}\n```")["summary"] == "s"
    p = lv.parse_day_summary("今天就这样吧。")
    assert p["summary"] == "今天就这样吧。" and p["did"] == [], p


check("feishu: 今日复盘 = 数据 + 你的回答 + 总结（没有总结也照发）",
      t_day_recap)


def t_question_prompt_enrichment():
    """问责提示词：**他自己的话、昨天的回答、漂了几天、时间黑洞**都要进去。

    ⚠️ 同时钉死一个曾经存在的自相矛盾：系统提示词要求「不能全是 yes_no」，
       而用户提示词最后一句却写着 "Every one must be answerable with yes or no"
       —— 模型照最后一句执行，整批就退化成二值题。这类矛盾不会报错，
       只会让输出悄悄变差。
    """
    import generate_questions as gq
    from common import Day

    day = Day.parse("2026-10-16", ZoneInfo(CFG["timezone"]))
    prompt = gq.build_user_prompt(
        day,
        adjustments=["明天先把书架收拾了"],
        tasks=["整理书架"],
        digest="- Recorded time: 7.5h total (Work 3h)",
        n_commit=3, n_routine=2,
        notes=["今天见了老王，聊了项目"],
        sinks=["18:22-18:48 0.4h chrome.exe — 知乎 / 夸克"],
        prev_qa=["Q: 20:11 那次节点上线了吗？  →  A: no"],
        task_ages={"整理书架": 3},
    )

    # 第一手材料排在最前
    assert prompt.index("今天见了老王") < prompt.index("### Adjustments for Tomorrow"), \
        "他自己的笔记应该排在日记的 Adjustments 之前"
    assert "Q: 20:11 那次节点上线了吗？" in prompt
    assert "已经挂了 3 天" in prompt, "任务漂了几天必须显式说出来"
    assert "18:22-18:48" in prompt, "时间黑洞没进去"
    assert "Day" in prompt and "2026-10-16" in prompt

    # ⚠️ 那个自相矛盾的指令不能再出现
    assert "Every one must be answerable" not in prompt, \
        "用户提示词又在要求「全部二值」，会和系统提示词的「混用格式」打架"
    assert "yes_no" in prompt, "仍要提醒它按格式选，而不是一律二值"

    # 没有素材时也不能崩，且要明确说「没有」
    bare = gq.build_user_prompt(day, [], [], "(nothing recorded)", 1, 1)
    assert "(nothing they wrote directly today)" in bare
    assert "(none)" in bare


check("questions: 笔记/昨日回答/漂了几天/时间黑洞都进 prompt，且不再自相矛盾",
      t_question_prompt_enrichment)


def t_live_question_expiry():
    """跨夜没答的实时题，**不能吃掉第二天的第一句话**。

    ⚠️ 这个缺陷很隐蔽：`awaiting` 只是一个题号，没有时间。昨晚的实时题一直没答，
       它就跨夜挂着；第二天你发的第一句话（哪怕是一个「?」或一条笔记）会被当成
       **那道题的答案**。而实时题是 text 格式，canonicalise 什么都收 ——
       所以不报错，只是静默地把话记到昨天，笔记也就没了。

    问责批（source=laptop）不适用这条：它本来就是「问昨天」，第二天早上答才对。
    """
    import importlib.util
    from datetime import timedelta

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "fb_exp", root / "server" / "feishu-bot" / "feishu_bot.py")
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    today = datetime.now(fb.TZ).date()
    yesterday = (today - timedelta(days=1)).isoformat()

    live_old = {"id": f"{yesterday}-aaa111", "day": yesterday, "source": "live",
                "kind": "routine", "format": "text", "text": "昨天那道实时题"}
    laptop_old = {"id": f"{yesterday}-bbb222", "day": yesterday,
                  "source": "laptop", "kind": "commitment",
                  "format": "yes_no", "text": "昨天那道问责题"}

    calls = {"note": [], "answer": [], "sent": []}
    state: dict = {}

    fb.load_state = lambda: state
    fb.save_state = lambda s: state.update(s)
    fb.relay_note = lambda t: (calls["note"].append(t) or {"inserted": 1})
    fb.relay_answer = lambda qid, v, n="": (
        calls["answer"].append((qid, v, n)) or {"inserted": 1})
    fb.send_text = lambda oid, t: calls["sent"].append(t)
    # ⚠️ 用 **kw 吞掉额外参数：advance() 现在还会带 ack（上一题的回执），
    #    写死成单参数的话，一次签名变化就会让这个测试整个炸掉 ——
    #    而它要验的是「跨夜不吃下一句话」，跟 ack 一点关系都没有。
    fb.advance = lambda oid, **kw: calls["sent"].append("ADVANCE")

    # ① 昨天没答的实时题 → 今天这句话不该被当成答案
    state.clear()
    state["awaiting"] = live_old["id"]
    fb.relay_pending = lambda: [live_old]
    fb.handle_text("ou_x", "今天见了老王")
    assert calls["answer"] == [], f"被当成答案了：{calls['answer']}"
    assert calls["note"] == ["今天见了老王"], calls["note"]
    assert state.get("awaiting") is None, "旧的 awaiting 该被清掉"

    # ①b 「记：」前缀同样不该被当成答案，而且前缀本身不进笔记正文
    #     （正挂着一道题时也一样 —— 笔记不该被题截胡）
    state.clear()
    state["awaiting"] = live_old["id"]
    calls["note"].clear()
    calls["answer"].clear()
    fb.handle_text("ou_x", "记：今天见了老王")
    assert calls["answer"] == [], f"带前缀的笔记被当成答案了：{calls['answer']}"
    assert calls["note"] == ["今天见了老王"], calls["note"]

    # ② 问责批不受影响 —— 它本来就是问昨天，第二天答是正常的
    calls["note"].clear()
    calls["answer"].clear()
    state.clear()
    state["awaiting"] = laptop_old["id"]
    fb.relay_pending = lambda: [laptop_old]
    fb.handle_text("ou_x", "做了")
    assert calls["answer"], "问责题该照常被当成答案"
    assert calls["answer"][0][0] == laptop_old["id"], calls["answer"]


check("feishu: 跨夜的实时题不会吃掉第二天的第一句话（问责批不受影响）",
      t_live_question_expiry)


def t_end_day_command():
    """「/收尾」立刻推收尾题，不等 21:30。"""
    import importlib.util
    import types

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "fb_end", root / "server" / "feishu-bot" / "feishu_bot.py")
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    today = datetime.now(fb.TZ).date().isoformat()
    qs = [
        {"id": f"{today}-sum1", "day": today, "kind": "summary",
         "format": "text", "text": "今天最值得记住的一件事是什么？"},
        {"id": f"{today}-sum2", "day": today, "kind": "summary",
         "format": "text", "text": "哪一块和你想的不一样？"},
    ]

    calls: list[str] = []
    state: dict = {}
    fb.load_state = lambda: state
    fb.save_state = lambda s: state.update(s)
    fb.send_text = lambda oid, t: calls.append(t)

    fake = types.ModuleType("live_questions")
    fake.force_summary = lambda day_iso, base, key: list(qs)
    old = sys.modules.get("live_questions")
    sys.modules["live_questions"] = fake
    try:
        fb.handle_end_day("ou_x")
    finally:
        if old is not None:
            sys.modules["live_questions"] = old
        else:
            sys.modules.pop("live_questions", None)

    assert len(calls) == 1, calls
    assert "今天最值得记住的一件事" in calls[0], calls[0]
    assert state["awaiting"] == f"{today}-sum1"
    assert f"{today}-sum1" in state["sent"]
    assert state["asked"][f"{today}-sum1"]["kind"] == "summary"

    # 都答完了要明确说一声，不能静默
    calls.clear()
    fake2 = types.ModuleType("live_questions")
    fake2.force_summary = lambda day_iso, base, key: []
    sys.modules["live_questions"] = fake2
    try:
        fb.handle_end_day("ou_x")
    finally:
        if old is not None:
            sys.modules["live_questions"] = old
        else:
            sys.modules.pop("live_questions", None)
    assert calls and "already answered" in calls[0], calls


check("feishu: /收尾 立刻出收尾题（不等 21:30），答完会明说",
      t_end_day_command)


def t_recap_command():
    """/复盘：强制重做一次复盘。

    ⚠️ 存在的理由是失败可恢复。总结那一步可能因为 LLM 挂了/额度用完而失败，
       而答完的题不会再问一遍 —— 没有这个命令，数据和回答都在，只差那段总结，
       却永远补不回来。实测过（2026-10-17，DEEPSEEK_API_KEY 没加载）。
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "fb_recap", root / "server" / "feishu-bot" / "feishu_bot.py")
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    called: dict = {}

    def fake_summary(oid, day, force=False):
        called["force"] = force
        return True

    fb.maybe_write_summary = fake_summary
    fb.load_state = lambda: {}
    fb.send_text = lambda oid, t: None

    fb.handle_text("ou_x", "/复盘")
    assert called.get("force") is True, "必须 force —— 否则有未答题时会直接返回"


check("feishu: /复盘 强制重做复盘", t_recap_command)


# ── 7d-3. 日记中继 ──────────────────────────────────────────────────
print("\n[21] Journal relay")


def t_journal_relay():
    """锁住「服务器调 AI 写正文、笔记本组装进 vault」这条链的两个约定。

    ⚠️ 这里最危险的是**终版保护**：终版输入包一旦被非终版盖掉，服务器就再也
       不会写那天的日记，而两边都不会报错。笔记本每 2 小时推一次非终版，
       顺序一乱就中招。
    """
    import importlib.util
    import tempfile

    path = (Path(__file__).resolve().parent.parent
            / "server" / "phone-relay" / "phone_relay.py")
    spec = importlib.util.spec_from_file_location("phone_relay_j", path)
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)

    # 日期形状不对不能拼出文件路径
    assert pr.journal_path("input", "../../etc/passwd") is None
    assert pr.journal_path("input", "") is None
    assert pr.journal_path("bogus", "2026-10-14") is None      # kind 白名单
    assert pr.journal_path("output", "2026-10-14") is not None

    with tempfile.TemporaryDirectory() as td:
        pr.DATA_DIR = Path(td)

        # 1. 终版保护：非终版盖不掉终版
        dest = pr.journal_path("input", "2026-10-14")
        pr._write_json(dest, {"day": "2026-10-14", "final": True, "timeline": {}})
        assert pr.final_bundle_wins(dest, {"final": False}), "非终版必须被挡"
        assert not pr.final_bundle_wins(dest, {"final": True}), "终版可以覆盖终版"
        # 还没有终版时，非终版应该正常写进去（否则当天的刷新会全被丢）
        other = pr.journal_path("input", "2026-10-15")
        assert not pr.final_bundle_wins(other, {"final": False})
        # 旧文件读不出来时不能拿它挡新的 —— 否则一次写坏就永久卡死
        dest.write_text("{ broken", encoding="utf-8")
        assert not pr.final_bundle_wins(dest, {"final": False})

        # 2. 存取往返
        out = pr.journal_path("output", "2026-10-14")
        pr._write_json(out, {"day": "2026-10-14", "body": "## Time Log\n",
                             "stats": {"cost": 0.04}})
        back = json.loads(out.read_text(encoding="utf-8"))
        assert back["body"] == "## Time Log\n" and back["stats"]["cost"] == 0.04, back

    # 3. 笔记本那侧：pulled 标记必须按 run_id 判等，不能只看「文件在不在」。
    #    否则服务器重生成一版，本机不会去取新的。
    import pull_journal_output as pjo
    assert hasattr(pjo, "pull_one"), "pull 的按天逻辑要能单独调用"


check("journal relay: final-bundle protection / path escape / round-trip",
      t_journal_relay)


# ── 7d-4. 台账合并 ─────────────────────────────────────────────────
print("\n[22] Ledger merge")


def t_ledger_merge():
    """锁住「服务器台账 → 本机台账」的合并：幂等、且**不能把哈希链拼断**。

    ⚠️ 这是整份代码里唯一会把两条哈希链接到一起的地方。接错了，
       `reconcile_cost.py` 的完整性校验会开始报「台账被人改过或丢过行」——
       而那个报错看起来像篡改，会把人引向完全错误的方向。

    ⚠️ 判重必须用**内容指纹**（不含 chain）。chain 依赖它前面是谁，同一条记录
       在服务器台账和本机台账里必然不同 —— 拿它判重永远判不出来。
    """
    import tempfile
    from datetime import date

    from common import GENESIS, CostLedger
    from merge_ledger import merge_into

    tz = ZoneInfo(CFG["timezone"])
    with tempfile.TemporaryDirectory() as td:
        led = CostLedger(Path(td) / "ledger.jsonl")
        led.append({"day": "2026-10-13", "stage": "journal", "cost": 0.05,
                    "currency": "CNY", "run_id": "local-1"})

        remote = [
            {"day": "2026-10-14", "stage": "journal", "cost": 0.0433,
             "currency": "CNY", "run_id": "srv-1",
             "ts_utc": "2026-10-15T14:48:00Z", "chain": "DEADBEEF"},
            # 远端列表**自己内部**重复 —— 也只该合并一次
            {"day": "2026-10-14", "stage": "journal", "cost": 0.0433,
             "currency": "CNY", "run_id": "srv-1",
             "ts_utc": "2026-10-15T14:48:00Z", "chain": "CAFEBABE"},
            "not a dict",          # 脏数据不许让整次合并失败
        ]
        assert len(merge_into(led, remote)) == 1, "远端内部的重复必须只合并一次"
        assert merge_into(led, remote) == [], "重复合并必须是幂等的"
        assert len(led.entries()) == 2, led.entries()

        # 本机那条链必须仍然自洽（复刻 CostLedger.append 的算法）
        prev = GENESIS
        for e in led.entries():
            payload = json.dumps({k: v for k, v in e.items() if k != "chain"},
                                 ensure_ascii=False, sort_keys=True)
            want = hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()
            assert e.get("chain") == want, "合并把本机的哈希链打断了"
            prev = e["chain"]
        # 服务器那条 chain 不能沿用 —— 它依赖服务器台账里它前面是谁
        assert all(e["chain"] not in ("DEADBEEF", "CAFEBABE") for e in led.entries())

        assert abs(led.spend_on(date(2026, 10, 14), tz) - 0.0433) < 1e-9, \
            "合并进来的花费必须能被 spend_on 算到"


check("ledger merge: idempotent / chain stays intact / spend adds up",
      t_ledger_merge)


# ── 7d-5. 作答形态 ─────────────────────────────────────────────────
print("\n[23] Answer formats")


def t_answer_formats():
    """三种作答形态：解析 → 渲染 → 回写，「算不算做到了」三处必须同一套约定。

    ⚠️ 错位的表现特别难查：题照样问出来了、手机照样答了、中继照样存了 ——
       只是「做到了」永远勾不掉任务，而且谁都不报错。
    """
    import importlib.util

    from apply_answers import answered_done
    from common import Day
    from generate_questions import parse_options, parse_questions

    # ── 解析：选项规范化 ──
    opts = parse_options([
        {"label": "  整条做完了 ", "done": True},
        {"label": "做了一半", "done": True},        # 第二个 done 不算
        {"label": ""},                              # 空标签丢
        "只写了字符串",                              # 非 dict 也认
    ])
    assert [o["label"] for o in opts] == ["整条做完了", "做了一半", "只写了字符串"], opts
    assert [o["done"] for o in opts] == [True, False, False], "done 只认第一个"
    assert parse_options("nope") == []

    # ── 解析：format 的降级与约束 ──
    day = Day.parse("2026-10-14", ZoneInfo(CFG["timezone"]))
    raw = json.dumps([
        {"kind": "commitment", "format": "choice", "text": "Q1", "ref": "t1",
         "options": [{"label": "a", "done": True}, {"label": "b"}]},       # 只有 2 个
        {"kind": "commitment", "format": "text", "text": "Q2", "ref": "不该有"},
        {"kind": "routine", "format": "bogus", "text": "Q3"},
    ])
    by_text = {i["text"]: i for i in parse_questions(raw, day, 5, 5)}
    assert by_text["Q1"]["format"] == "yes_no", "选项不足 3 个要降级"
    assert by_text["Q1"]["options"] == []
    assert by_text["Q2"]["format"] == "text" and by_text["Q2"]["ref"] == "", \
        "文字题勾不了任务，ref 必须清空"
    assert by_text["Q3"]["format"] == "yes_no", "认不出的 format 一律当二值"

    # ── 回写：「算不算做到了」 ──
    assert answered_done({"format": "yes_no"}, "yes")
    assert not answered_done({"format": "yes_no"}, "no")
    assert answered_done({}, "YES"), "缺 format 的老数据要当 yes_no 处理"

    ch = {"format": "choice",
          "options": [{"label": "整条做完了", "done": True},
                      {"label": "做了一半", "done": False}]}
    assert answered_done(ch, "整条做完了")
    assert not answered_done(ch, "做了一半")
    assert not answered_done(ch, "没见过的选项"), "对不上就别猜"
    assert not answered_done({"format": "text"}, "随便写了点什么")

    # ── 渲染：三种形态都要出得来 ──
    path = (Path(__file__).resolve().parent.parent
            / "server" / "phone-relay" / "phone_relay.py")
    spec = importlib.util.spec_from_file_location("phone_relay_f", path)
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)

    page = pr.render_ask_page({"day": "2026-10-14"}, [
        {"id": "q1", "text": "二值题", "format": "yes_no"},
        {"id": "q2", "text": "选择题", "format": "choice",
         "options": [{"label": "整条做完了", "done": True},
                     {"label": "做了一半"}, {"label": "压根没碰"}]},
        {"id": "q3", "text": "文字题", "format": "text"},
    ], "k")
    for f in ("yes_no", "choice", "text"):
        assert f'data-format="{f}"' in page, f"{f} 没渲染出来"
    assert 'data-v="整条做完了"' in page, "选项要变成按钮"
    assert 'data-v="yes"' in page, "二值题的两个按钮还在"
    assert 'class="x"' in page, "文字题要有输入框"


check("answer formats: parse / render / what-counts-as-done stay in sync",
      t_answer_formats)


# ── 7d-6. 飞书那层的答案规范化 ─────────────────────────────────────
print("\n[24] Feishu answer canonicalisation")


def t_feishu_canonicalise():
    """飞书的「中文 → 契约值」必须逐字产成 apply_answers 认的东西。

    链路上现在多了一层：**人 → 机器人 → 中继 → 事件表 → apply_answers**。
    中继只对**字面量** yes/no 转小写、选择题比的是选项原文**精确相等**，
    所以机器人要是把「做了」原样转手发出去，答案照样存得进去、任务永远
    勾不掉，而且两边都不报错 —— 这是这条链上最容易静默失败的一处。

    这个测试同时钉死两件事：
      · canonicalise 产出的值，answered_done 必须认
      · **未经规范化的中文，answered_done 必须不认**（否则上面那条就是空话）
    """
    import importlib.util

    from apply_answers import answered_done

    path = (Path(__file__).resolve().parent.parent
            / "server" / "feishu-bot" / "feishu_bot.py")
    spec = importlib.util.spec_from_file_location("feishu_bot_t", path)
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    # ── 先证明这个坑是真的 ──
    yesno = {"id": "2026-10-14-abcd1234", "format": "yes_no", "text": "做了吗"}
    assert not answered_done(yesno, "做了"), \
        "如果中文原样能认，那 canonicalise 就没有存在意义了 —— 这个前提变了"

    # ── 二值题 ──
    for said in ("做了", "是", "好", "对", "有", "嗯", "yes", "Y", "1", "✓"):
        v, note, _ = fb.canonicalise(yesno, said)
        assert v == "yes", f"{said!r} 应该归一成 yes，得到 {v!r}"
        assert note == "", f"{said!r} 后面没写东西，备注该是空的"
        assert answered_done(yesno, v), f"{said!r} → {v!r} 但 answered_done 不认"
    for said in ("没做", "没", "否", "没有", "无", "no", "N", "0", "×"):
        v, note, _ = fb.canonicalise(yesno, said)
        assert v == "no", f"{said!r} 应该归一成 no，得到 {v!r}"
        assert not answered_done(yesno, v)

    # ── **可以带自己的话**：答案在前，后半句进备注 ──
    v, note, _ = fb.canonicalise(yesno, "做了，花了两小时")
    assert (v, note) == ("yes", "花了两小时"), (v, note)
    assert answered_done(yesno, v), "带备注不影响「算不算做到」"

    v, note, _ = fb.canonicalise(yesno, "没做 在开会")
    assert (v, note) == ("no", "在开会"), (v, note)
    assert not answered_done(yesno, v)

    # 分隔符不止逗号：空格、句号、括号、问号都要认
    for said, want in (("做了。花了两小时", "花了两小时"),
                       ("做了（花了两小时）", "花了两小时"),
                       ("做了?好吧", "好吧")):
        v, note, _ = fb.canonicalise(yesno, said)
        assert v == "yes", f"{said!r} 应该认出 yes，得到 {v!r}"
        assert note == want, f"{said!r} 的备注该是 {want!r}，得到 {note!r}"

    # ⚠️ 答案**不能**出现在后半句 —— 那意味着要猜「哪段才是答案」，
    #    猜错会把任务勾反，而且不可逆。
    v, note, hint = fb.canonicalise(yesno, "花了两小时，做了")
    assert v is None, f"答案不在最前面时不能猜，实际 {v!r} / {note!r}"
    assert hint

    # 认不出来必须返回 None（重问），**不能猜**
    for said in ("大概吧", "", "  ", "42"):
        v, note, hint = fb.canonicalise(yesno, said)
        assert v is None, f"{said!r} 不该被猜成一个答案"
        assert hint, "认不出来时要给出重问的话"

    # ── 选择题：回的必须是**选项原文**，不是数字 ──
    ch = {"id": "2026-10-14-beef5678", "format": "choice", "text": "做到哪一步",
          "options": [{"label": "整条做完了", "done": True},
                      {"label": "做了一半", "done": False},
                      {"label": "压根没碰", "done": False}]}
    v, note, _ = fb.canonicalise(ch, "1")
    assert v == "整条做完了" and note == "", (v, note)
    assert answered_done(ch, v), "选了标 done 的那项，应该勾掉任务"

    v, note, _ = fb.canonicalise(ch, "2")
    assert v == "做了一半" and not answered_done(ch, v)

    # 直接回原文也认（但要逐字对上）
    v, note, _ = fb.canonicalise(ch, "整条做完了")
    assert v == "整条做完了" and note == "" and answered_done(ch, v)

    # 带自己的话：数字或原文后面接一段
    v, note, _ = fb.canonicalise(ch, "1 其实只做了一半")
    assert (v, note) == ("整条做完了", "其实只做了一半"), (v, note)
    v, note, _ = fb.canonicalise(ch, "整条做完了，但很勉强")
    assert (v, note) == ("整条做完了", "但很勉强"), (v, note)

    # 越界 / 认不出的选项 → None
    for said in ("4", "0", "做完了", "第一个"):
        v, note, hint = fb.canonicalise(ch, said)
        assert v is None, f"{said!r} 不该被认成某个选项（不做模糊匹配）"
        assert hint

    # ── 文字题：原样透传（备注为空，内容本身就是值）──
    tx = {"id": "2026-10-14-cafe9012", "format": "text", "text": "随便说点什么"}
    v, note, _ = fb.canonicalise(tx, "  今天有点累  ")
    assert v == "今天有点累" and note == "", (v, note)
    assert not answered_done(tx, v), "文字题永远勾不了任务，这是有意的"
    assert fb.canonicalise(tx, "   ")[0] is None, "空白的文字题不算答案"

    # ── 缺 format 的老数据要当二值处理（和 answered_done 的默认一致）──
    v, note, _ = fb.canonicalise({"id": "x"}, "做了")
    assert v == "yes"

    # ── 渲染：三种形态都得给出**可回答**的提示 ──
    assert "做了" in fb.render_question(yesno, 3), "二值题要告诉人怎么回"
    assert "1-3" in fb.render_question(ch, 2), "选择题要报出可回的数字范围"
    assert "整条做完了" in fb.render_question(ch, 2), "选项要列出来"


check("feishu: 中文答案归一成契约值，且未归一化的中文确实不被认",
      t_feishu_canonicalise)


def t_feishu_skip():
    """`/skip` 必须**真的**跳过 —— 而不是让下一轮把同一题问回来。

    ⚠️ 「什么算还没答」永远只有中继一份实现，所以被跳过的那题在中继眼里
       仍然待答。本地不额外记一笔，`/skip` 就是个假动作：用户看到「跳过了」，
       紧接着同一题又被推过来，比不支持跳过还糟。
    """
    import importlib.util

    path = (Path(__file__).resolve().parent.parent
            / "server" / "feishu-bot" / "feishu_bot.py")
    spec = importlib.util.spec_from_file_location("feishu_bot_s", path)
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    qs = [{"id": "d-1", "format": "yes_no", "text": "Q1"},
          {"id": "d-2", "format": "yes_no", "text": "Q2"}]
    fb.relay_pending = lambda: list(qs)          # 不碰网络

    today = fb.datetime.now(fb.TZ).date().isoformat()

    # 全新的一天：两题都在
    st = {}
    assert [q["id"] for q in fb.pending_filtered(st)] == ["d-1", "d-2"]
    assert st["skipped"] == [] and st["skipped_day"] == today

    # 跳过 d-1 之后它就不该再出现
    st["skipped"] = ["d-1"]
    assert [q["id"] for q in fb.pending_filtered(st)] == ["d-2"]

    # 换一天要自动清空 —— 否则昨天跳过的题今天会被永久吞掉
    st["skipped_day"] = "2000-01-01"
    assert [q["id"] for q in fb.pending_filtered(st)] == ["d-1", "d-2"], \
        "跨天必须重置，不能把昨天的跳过带到今天"
    assert st["skipped"] == []

    # 全跳过 → 空列表（advance 会据此说「今天没有待答的问题了」）
    st["skipped"] = ["d-1", "d-2"]
    assert fb.pending_filtered(st) == []


check("feishu: /skip 真的跳过（跨天重置，且不影响中继的判定）",
      t_feishu_skip)


def t_feishu_live_routing():
    """「问我今天」的分流：**先说不，再说能**。

    ⚠️ 「我今天在电脑上干了什么」里既有「电脑」也像在问活动。要是位置/手机
       那两条先命中，机器人会一本正经地回一段手机数据 —— 答非所问，而且
       看着像答对了。所以顺序是有意的：先判那几类**服务器上没有的**数据。

    顺带钉住「认不出来时不能默默去出题」：用户在问数据，却收到一串提问，
    同样像坏了。
    """
    import importlib.util

    path = (Path(__file__).resolve().parent.parent
            / "server" / "feishu-bot" / "live.py")
    spec = importlib.util.spec_from_file_location("live_t", path)
    lv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lv)

    calls: list[str] = []
    lv.render_location = lambda day: (calls.append("loc") or "⟨位置⟩")
    lv.relay_events = lambda day, base, key: (calls.append("relay") or [])
    lv.render_phone = lambda evs, day_iso=None: (calls.append("phone") or "⟨手机⟩")
    lv.render_desktop = lambda day, base, key: (calls.append("desktop") or "⟨电脑⟩")
    lv.render_watch = lambda day, base, key: (calls.append("watch") or "⟨健康⟩")

    def ask(t: str) -> str | None:
        calls.clear()
        return lv.answer_query(t, day_iso="2026-10-16",
                               base="http://x", key="k")

    # ── 仍然答不了的：一条数据都不该去拉 ──
    for q in ("今天的日记写了吗", "我的 vault 里有什么"):
        assert ask(q) == lv.JOURNAL_ANSWER, f"{q!r} 应该老实说 vault 没推上来"
        assert calls == [], f"{q!r} 不该去拉任何数据，实际拉了 {calls}"

    # 手表数据现在有快照了 —— 必须走 render_watch，而不是那句过期的「还没接」
    assert ask("今天走了多少步") == "⟨健康⟩", "步数应该走健康快照"
    assert calls == ["watch"], calls
    assert ask("心率多少") == "⟨健康⟩"
    assert calls == ["watch"], calls

    # ── 电脑活动现在答得了（笔记本会推快照）──
    for q in ("我今天在电脑上干了什么", "我今天写代码了吗",
              "我今天浏览器开了哪些网页"):
        assert ask(q) == "⟨电脑⟩", f"{q!r} 应该走电脑快照"
        assert calls == ["desktop"], calls

    # 「电脑」优先于同在句中的其它关键词
    assert ask("我在电脑上手机用了多久") == "⟨电脑⟩", \
        "句中同时出现「电脑」时，应该答电脑而不是手机"

    # 而「日记」比「电脑」更具体 —— 反过来会被电脑快照顶掉
    assert ask("我今天在电脑上写的日记呢") == lv.JOURNAL_ANSWER, \
        "日记那条更具体，必须赢过电脑活动"

    # ── 答得了的：只拉该拉的 ──
    assert ask("我今天去哪了") == "⟨位置⟩"
    assert calls == ["loc"], calls

    assert ask("手机用了多久") == "⟨手机⟩"
    assert calls == ["relay", "phone"], calls

    assert ask("今天解锁几次") == "⟨手机⟩"
    assert calls == ["relay", "phone"], calls

    # 两个都问到 → 两个都答，不猜他想问哪个
    got = ask("今天去哪了，手机用了多久")
    assert got == "⟨位置⟩\n\n⟨手机⟩", got
    assert calls == ["loc", "relay", "phone"], calls

    # ⚠️ 一边挂了不该拖垮另一边：两份数据、两条链路，各自独立。
    def _boom(day):
        calls.append("loc")
        raise lv.LiveUnavailable("Dawarich 挂了")

    lv.render_location = _boom
    got = ask("今天去哪了，手机用了多久")
    assert "Dawarich 挂了" in got and "⟨手机⟩" in got, got

    # ── 随手记不能被关键词吃掉 ──
    # 「电脑 / 网页 / 窗口 / 应用」都是日常名词。只靠关键词判断的话，
    # 一句随手记会被当成查数据：用户收到一整份电脑报表，而他写的那句
    # **根本没存下来**。丢的是他亲手写的东西 —— 所以不像在问的一律当笔记。
    for note in ("今天用电脑画了张图", "这个网页不错，收藏了",
                 "把应用都整理了一下", "在窗口里调了半天参数",
                 "电脑上那个 bug 还没修",
                 "今天几乎没开电脑",     # 「几(?!乎)」：「几乎」不是问句
                 "哪怕电脑卡也不换"):    # 「哪(?!怕)」：「哪怕」不是问句
        assert ask(note) is None, f"{note!r} 是随手记，不该被当成查询"
        assert calls == [], f"{note!r} 不该去拉任何数据，实际拉了 {calls}"

    # ── 问「过去」→ 交给检索 + 模型，不能拿今天的数据冒充 ──
    for q in ("我上次觉得累是什么时候", "以前那几天手机用了多久",
              "上个月写过什么"):
        assert ask(q) == lv.UNKNOWN_ANSWER, \
            f"{q!r} 是历史问题，不该走今天的模板"
        assert calls == [], f"{q!r} 不该去拉今天的数据，实际拉了 {calls}"
    # 不像在问的「上次」还是笔记，不能因为一个词就被吃掉
    assert ask("上次买了牛奶") is None, "不是问句，应该当笔记"

    # ── 认不出来时**不能**默默去出题 ──
    assert ask("这是啥？") == lv.UNKNOWN_ANSWER, "带问号又没命中的，要明说能答什么"
    assert calls == []

    # ── 「随便一句就出题」这个手感要保住（返回 None = 交给调用方去出题）──
    for t in ("", "   ", "?", "在吗", "给我题"):
        assert ask(t) is None, f"{t!r} 应该落到「给我题」，而不是被当成查询"

    # ── 那两句「答不了」必须说清**为什么**，不能是干巴巴的「不支持」──
    assert "Obsidian" in lv.JOURNAL_ANSWER
    assert "没有推到服务器" in lv.JOURNAL_ANSWER
    # 「没推过」时要把人引到那个能修的地方去，不能只说一句「没有数据」
    assert "push_desktop.py" in lv.DESKTOP_NO_DATA
    # 没推过健康汇总时，也要指到那个能修的地方
    assert "push_health.py" in lv.WATCH_NO_DATA
    # 中继出错时的兜底话不能再说「还没接」—— 那是过期信息
    assert "拉不到" in lv.HEALTH_ANSWER


check("feishu: 路由从窄到宽 —— 日记/手表说答不了，电脑/位置/手机各拉各的",
      t_feishu_live_routing)


def t_feishu_slash_only():
    """数据只走斜杠命令；普通长文一律记成笔记。

    背景：一段随笔里出现「手机」和几个问号，就被旧的 answer_query 当成
    「手机用了多久」，于是用户收到一份手机报表，而他写的那段话没被存下来。
    现在只有 /phone /where /desktop /watch /qa /today /ask 会查数据。
    """
    import importlib.util

    path = (Path(__file__).resolve().parent.parent
            / "server" / "feishu-bot" / "feishu_bot.py")
    spec = importlib.util.spec_from_file_location("feishu_bot_slash", path)
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    calls = {"sent": [], "notes": [], "cmds": []}
    state: dict = {}
    fb.load_state = lambda: state
    fb.save_state = lambda s: state.update(s)
    fb.send_text = lambda oid, t: calls["sent"].append(t)
    fb.save_note = lambda oid, t: calls["notes"].append(t)
    fb.handle_data_command = lambda oid, cmd, arg: calls["cmds"].append((cmd, arg))

    long_note = ("Something happened today that drained me. "
                 "Everyone had their heads down, playing on their phones. "
                 "What exactly are you trying to express? Why does anyone do anything?")
    fb.handle_text("ou_x", long_note)
    assert calls["notes"] == [long_note], calls
    assert calls["cmds"] == [], f"普通消息不该触发查数据：{calls['cmds']}"

    # 斜杠命令才查数据；日期参数原样传下去
    fb.handle_text("ou_x", "/phone")
    assert calls["cmds"] == [("/phone", "")], calls["cmds"]
    fb.handle_text("ou_x", "/where 2026-10-20")
    assert calls["cmds"][-1] == ("/where", "2026-10-20"), calls["cmds"]
    fb.handle_text("ou_x", "/ask 今天手机用了多久")
    assert calls["cmds"][-1] == ("/ask", "今天手机用了多久"), calls["cmds"]

    # /help 必须把命令列全；大小写不敏感
    calls["sent"].clear()
    fb.handle_text("ou_x", "/HELP")
    assert calls["sent"] and "/phone" in calls["sent"][-1], calls["sent"]
    assert "/where" in calls["sent"][-1] and "/qa" in calls["sent"][-1], calls["sent"]


check("feishu: 只有斜杠命令查数据；长随笔不会被「手机」关键词吃掉",
      t_feishu_slash_only)


def t_feishu_note_prefix():
    """「记：」前缀必须无条件当笔记 —— 它是猜错时的逃生口。

    live.answer_query 靠关键词 + 疑问语气猜「这句话是在问数据还是在记一笔」，
    而「这个电脑配置怎么样」既是问句、又命中「电脑」，但它明明只是一条想法。
    没有前缀的话这种话没法表达成笔记：查数据那条路会截胡，而笔记那条路
    只收「没被认出来」的话。
    """
    import importlib.util

    path = (Path(__file__).resolve().parent.parent
            / "server" / "feishu-bot" / "feishu_bot.py")
    spec = importlib.util.spec_from_file_location("feishu_bot_np", path)
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    assert fb.split_note_prefix("记：今天用电脑画了张图") == "今天用电脑画了张图"
    assert fb.split_note_prefix("记: 在窗口里调了半天") == "在窗口里调了半天"
    assert fb.split_note_prefix("记一笔：这个网页不错") == "这个网页不错"
    assert fb.split_note_prefix("笔记本：这个网页不错") is None, \
        "只有明确的前缀才算，别把「笔记本：」也吃掉"
    # 前缀后面空着 → ""，必须和「没带前缀」的 None 分开
    assert fb.split_note_prefix("记：") == ""
    assert fb.split_note_prefix("记：   ") == ""
    for t in ("今天见了老王", "记下了什么吗", ""):
        assert fb.split_note_prefix(t) is None, f"{t!r} 不该被当成带前缀"


check("feishu: 「记：」前缀无条件当笔记（猜错时的逃生口）",
      t_feishu_note_prefix)


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
