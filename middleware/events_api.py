"""统一事件表 —— 所有采集源的归一化落点。

为什么需要它：
  ActivityWatch 有 REST、Dawarich 有 REST、
  Gadgetbridge 什么都没有、Health Connect 只有加密 ZIP。
  形状各异，喂给 LLM 之前必须先归一成一种，否则每天要写 N 个解析分支。

运行：
  uvicorn middleware.events_api:app --host 127.0.0.1 --port 8787
  或  python -m middleware.events_api

鉴权：除 /health 外全部需要 X-API-Key header。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import ROOT, load_config  # noqa: E402

CFG = load_config()
DB_PATH = Path(CFG["state_dir"]) / "events.db"
API_KEY = CFG["sources"]["events_api"]["api_key"]

app = FastAPI(title="LifeLogging Events API", version="1.0")

# ── 表结构 ──────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,          -- ISO8601 UTC, 事件发生时间
    day_local   TEXT NOT NULL,          -- YYYY-MM-DD, 本地日期分区键
    domain      TEXT NOT NULL,          -- screen | location | health | phone | manual | ai
    metric      TEXT NOT NULL,          -- app_usage | visit | sleep | unlock | ...
    value_num   REAL,
    value_text  TEXT,
    source      TEXT NOT NULL,          -- activitywatch | dawarich | gadgetbridge | tasker | ...
    meta_json   TEXT NOT NULL DEFAULT '{}',
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_day    ON events(day_local, domain);
CREATE INDEX IF NOT EXISTS idx_events_metric ON events(domain, metric, ts_utc);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts_utc);

-- 幂等去重：同一个源的同一条记录重复上报不会重复入库
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup
    ON events(source, domain, metric, ts_utc, COALESCE(value_text,''));

-- ── 分类后的时间块 ──────────────────────────────────────────────────
-- 为什么要单独一张表、而不是只留 state/timeline-*.json：
--   「工作 4 小时 / 休闲 1.5 小时」这一层才是可视化和长期分析真正要用的数据。
--   放在每天一个 JSON 文件里，跨月跨年的查询就得先写代码把文件拼起来，
--   Grafana 之类的工具更是完全读不到。
--   进表之后，一行 SQL 就能出「过去 90 天每天各分类时长」。
CREATE TABLE IF NOT EXISTS blocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day_local   TEXT NOT NULL,          -- YYYY-MM-DD
    start_utc   TEXT NOT NULL,
    end_utc     TEXT NOT NULL,
    hours       REAL NOT NULL,
    category    TEXT NOT NULL,
    app         TEXT,
    title       TEXT,
    url         TEXT,
    confidence  TEXT,
    blackhole   INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'normalize',
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_day      ON blocks(day_local, category);
CREATE INDEX IF NOT EXISTS idx_blocks_category ON blocks(category, day_local);
-- 同一天同一时段同分类只留一条：重跑 normalize 不会翻倍
CREATE UNIQUE INDEX IF NOT EXISTS idx_blocks_dedup
    ON blocks(day_local, start_utc, category);
"""


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")   # 采集与查询并发
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@app.on_event("startup")
def _startup() -> None:
    init_db()
    print(f"[events-api] DB: {DB_PATH}")
    print(f"[events-api] key loaded from config.yaml ({len(API_KEY)} chars)")


# ── 鉴权 ────────────────────────────────────────────────────────────


def auth(x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> None:
    # 写成可选再手动 401，而不是把 header 声明成必填 ——
    # 必填 header 缺失时 FastAPI 返回 422，但鉴权失败在语义上应该是 401。
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="missing or bad api key")


# ── 模型 ────────────────────────────────────────────────────────────


class EventIn(BaseModel):
    ts_utc: str
    domain: str
    metric: str
    value_num: float | None = None
    value_text: str | None = None
    source: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class BatchIn(BaseModel):
    events: list[EventIn]


# ── 写入 ────────────────────────────────────────────────────────────


def _day_local(ts_utc: str) -> str:
    dt = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    tzname = CFG["timezone"]
    try:
        from zoneinfo import ZoneInfo

        return dt.astimezone(ZoneInfo(tzname)).date().isoformat()
    except Exception:
        return dt.date().isoformat()


@app.post("/events", dependencies=[Depends(auth)])
def post_event(ev: EventIn) -> dict[str, Any]:
    return {"inserted": _insert_many([ev])}


@app.post("/events/batch", dependencies=[Depends(auth)])
def post_batch(batch: BatchIn) -> dict[str, Any]:
    return {"inserted": _insert_many(batch.events)}


def _insert_many(events: list[EventIn]) -> int:
    if not events:
        return 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        (
            e.ts_utc,
            _day_local(e.ts_utc),
            e.domain,
            e.metric,
            e.value_num,
            e.value_text,
            e.source,
            json.dumps(e.meta, ensure_ascii=False),
            now,
        )
        for e in events
    ]
    with closing(db()) as conn:
        # 用 total_changes 的增量算实际写入数。
        # 不用 cursor.rowcount：executemany 的 rowcount 在不同 Python 版本上
        # 语义不一致（旧版返回 -1），而 total_changes 是 sqlite3 层面的事实。
        before = conn.total_changes
        # INSERT OR IGNORE 配合唯一索引实现幂等：重复上报静默忽略
        conn.executemany(
            """INSERT OR IGNORE INTO events
               (ts_utc, day_local, domain, metric, value_num, value_text,
                source, meta_json, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
        return conn.total_changes - before


# ── 查询 ────────────────────────────────────────────────────────────


@app.get("/events", dependencies=[Depends(auth)])
def get_events(
    # ⚠️ 必须用 Annotated + 真实默认值这种写法。
    # 老写法 `frm: str | None = Query(None, alias="from")` 下，走 HTTP 时
    # FastAPI 会把默认值解析掉，但**直接调用这个函数**（脚本里复用、
    # 单元测试）拿到的默认值是 Query 对象而不是 None，塞进 SQL 参数会炸。
    domain: Annotated[str | None, Query()] = None,
    metric: Annotated[str | None, Query()] = None,
    day: Annotated[str | None, Query(description="local date YYYY-MM-DD")] = None,
    frm: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = 100_000,
) -> dict[str, Any]:
    where, params = [], []
    if domain:
        where.append("domain = ?")
        params.append(domain)
    if metric:
        where.append("metric = ?")
        params.append(metric)
    if day:
        where.append("day_local = ?")
        params.append(day)
    if frm:
        where.append("ts_utc >= ?")
        params.append(frm)
    if to:
        where.append("ts_utc <= ?")
        params.append(to)

    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts_utc ASC LIMIT ?"
    params.append(min(int(limit), 500_000))

    with closing(db()) as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    for r in rows:
        try:
            r["meta"] = json.loads(r.pop("meta_json"))
        except Exception:
            r["meta"] = {}
    return {"count": len(rows), "events": rows}


@app.get("/aggregate", dependencies=[Depends(auth)])
def aggregate(
    domain: Annotated[str, Query()],
    metric: Annotated[str | None, Query()] = None,
    day: Annotated[str | None, Query()] = None,
    frm: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """按 value_text 分组求和。拿来直接做「今天各 App 用时」这类汇总。"""
    where = ["domain = ?"]
    params: list[Any] = [domain]
    if metric:
        where.append("metric = ?")
        params.append(metric)
    if day:
        where.append("day_local = ?")
        params.append(day)
    if frm:
        where.append("ts_utc >= ?")
        params.append(frm)
    if to:
        where.append("ts_utc <= ?")
        params.append(to)

    sql = f"""SELECT COALESCE(value_text,'') AS k,
                     SUM(COALESCE(value_num,0)) AS total,
                     COUNT(*) AS n
              FROM events WHERE {' AND '.join(where)}
              GROUP BY k ORDER BY total DESC"""
    with closing(db()) as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    return {
        "total": sum(r["total"] for r in rows),
        "groups": rows,
    }


@app.get("/days", dependencies=[Depends(auth)])
def days() -> dict[str, Any]:
    """有哪些天的数据（排查采集是否断档）。"""
    with closing(db()) as conn:
        rows = conn.execute(
            """SELECT day_local, domain, COUNT(*) n
               FROM events GROUP BY day_local, domain
               ORDER BY day_local DESC LIMIT 500"""
        ).fetchall()
    return {"days": [dict(r) for r in rows]}


# ── 时间块（分类结果）────────────────────────────────────────────────


class BlockIn(BaseModel):
    day_local: str
    start_utc: str
    end_utc: str
    hours: float
    category: str
    app: str | None = None
    title: str | None = None
    url: str | None = None
    confidence: str | None = None
    blackhole: bool = False


class BlocksIn(BaseModel):
    blocks: list[BlockIn]


@app.post("/blocks", dependencies=[Depends(auth)])
def post_blocks(payload: BlocksIn) -> dict[str, Any]:
    """归一化脚本把当天的分类结果写进来。

    先按天删旧再插 —— 重跑 normalize 不会翻倍，
    也不需要 normalize 自己记住「这天写过了没有」。
    """
    if not payload.blocks:
        return {"inserted": 0, "deleted": 0}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    days = sorted({b.day_local for b in payload.blocks})
    rows = [
        (
            b.day_local, b.start_utc, b.end_utc, b.hours, b.category,
            b.app, b.title, b.url, b.confidence, 1 if b.blackhole else 0,
            "normalize", now,
        )
        for b in payload.blocks
    ]

    with closing(db()) as conn:
        deleted = 0
        for d in days:
            cur = conn.execute("DELETE FROM blocks WHERE day_local = ?", (d,))
            deleted += cur.rowcount
        before = conn.total_changes
        conn.executemany(
            """INSERT OR IGNORE INTO blocks
               (day_local, start_utc, end_utc, hours, category, app, title, url,
                confidence, blackhole, source, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
        return {"inserted": conn.total_changes - before, "deleted": deleted}


@app.get("/blocks", dependencies=[Depends(auth)])
def get_blocks(
    day: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    frm: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = 100_000,
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if day:
        where.append("day_local = ?")
        params.append(day)
    if category:
        where.append("category = ?")
        params.append(category)
    if frm:
        where.append("day_local >= ?")
        params.append(frm[:10])
    if to:
        where.append("day_local <= ?")
        params.append(to[:10])

    sql = "SELECT * FROM blocks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY start_utc ASC LIMIT ?"
    params.append(min(int(limit), 500_000))

    with closing(db()) as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    return {"count": len(rows), "blocks": rows}


@app.get("/blocks/daily", dependencies=[Depends(auth)])
def blocks_daily(
    frm: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """按 日 × 分类 汇总 —— 可视化最常用的那个查询。

    直接对应的就是「过去 90 天每天各分类多少小时」这类图。
    """
    where: list[str] = []
    params: list[Any] = []
    if frm:
        where.append("day_local >= ?")
        params.append(frm[:10])
    if to:
        where.append("day_local <= ?")
        params.append(to[:10])

    sql = """SELECT day_local, category,
                    ROUND(SUM(hours), 2) AS hours,
                    COUNT(*) AS block_count,
                    SUM(blackhole) AS sinks
             FROM blocks"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY day_local, category ORDER BY day_local DESC, hours DESC"

    with closing(db()) as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    return {"count": len(rows), "rows": rows}


@app.delete("/day", dependencies=[Depends(auth)])
def delete_day(day: str, source: str | None = None) -> dict[str, Any]:
    """重跑某天时先清掉旧的，避免重复。"""
    with closing(db()) as conn:
        if source:
            cur = conn.execute(
                "DELETE FROM events WHERE day_local=? AND source=?", (day, source)
            )
        else:
            cur = conn.execute("DELETE FROM events WHERE day_local=?", (day,))
            # 只清 events 不清 blocks 的话，重跑会留下孤儿时间块
            conn.execute("DELETE FROM blocks WHERE day_local=?", (day,))
        conn.commit()
        return {"deleted": cur.rowcount}


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        with closing(db()) as conn:
            n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {"ok": True, "events": n, "db": str(DB_PATH)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ── 采集端的专用入口（省得每个客户端自己拼 payload）────────────────


@app.post("/ingest/gadgetbridge", dependencies=[Depends(auth)])
def ingest_gadgetbridge(payload: dict[str, Any]) -> dict[str, Any]:
    """接收 Gadgetbridge 导出的 SQLite（base64）或已解析的 JSON。

    Gadgetbridge 完全没有 REST API，所以要么手机端导出后同步过来，
    要么用 Tasker 在导出完成广播后读文件上传。见 ingest_gadgetbridge.py。
    """
    events = payload.get("events", [])
    return {"inserted": _insert_many([EventIn(**e) for e in events])}


@app.post("/ingest/phone", dependencies=[Depends(auth)])
def ingest_phone(payload: dict[str, Any]) -> dict[str, Any]:
    """Tasker / MacroDroid 的通用上报口。

    Tasker 里配 HTTP Request → POST 到这里，body 形如：
      {"ts_utc":"2026-10-13T04:12:00Z","domain":"phone","metric":"unlock",
       "value_text":"com.tencent.mm","source":"tasker"}
    """
    events = payload.get("events") or [payload]
    cleaned = [
        EventIn(
            ts_utc=e["ts_utc"],
            domain=e.get("domain", "phone"),
            metric=e["metric"],
            value_num=e.get("value_num"),
            value_text=e.get("value_text"),
            source=e.get("source", "tasker"),
            meta=e.get("meta", {}),
        )
        for e in events
        if e.get("ts_utc") and e.get("metric")
    ]
    return {"inserted": _insert_many(cleaned)}


@app.post("/ingest/apple-health", dependencies=[Depends(auth)])
def ingest_apple_health(payload: dict[str, Any]) -> dict[str, Any]:
    """Health Auto Export 的 REST API 落点。

    HAE 的 payload 形如 {"data": {"metrics": [...], "workouts": [...]}}，
    这里只做最小映射，字段随 HAE 版本会变，建议先打开日志跑一次看结构。
    """
    metrics = (payload.get("data") or {}).get("metrics") or []
    events: list[EventIn] = []
    for m in metrics:
        name = m.get("name", "")
        for pt in m.get("data", []):
            events.append(
                EventIn(
                    ts_utc=(pt.get("date") or "").replace("+0000", "Z"),
                    domain="health",
                    metric=name.lower().replace(" ", "_"),
                    value_num=pt.get("qty"),
                    value_text=pt.get("source"),
                    source="apple_health",
                    meta={k: v for k, v in pt.items() if k not in ("qty", "date")},
                )
            )
    return {"inserted": _insert_many(events)}


def _bind() -> tuple[str, int]:
    """监听地址和端口，读 config.yaml 的 sources.events_api.api_base。

    ⚠️ **默认只绑 127.0.0.1，也就是只有本机能连。** 手机（Tasker）要上报
       手机使用事件，就得让它能连上 —— 但那是**把端口开出去**，属于安全决定，
       所以不默认改，要你自己在 config.yaml 里写。

    推荐做法是绑 **Tailscale 地址**而不是 0.0.0.0：

        sources:
          events_api:
            api_base: "http://100.x.y.z:8787"   # tailscale ip -4 拿到的地址

        改完两处都要跟着改（它们指向同一个服务）：
          · config.yaml 的 sources.events_api.api_base
          · run_daily.ps1 里拉起服务用的 --host（它按 api_base 解析，自动跟随）

    绑 Tailscale 地址 = 只有你 tailnet 里的设备能访问，公网和局域网都碰不到。
    绑 0.0.0.0 会把局域网也开出去，别这么干。

    ⚠️ 开出去之前先把 `api_key` 从 `dev-local-key-please-change` 换成随机串 ——
       这个端点收的是任意写入，占位 key 等于没有 key。
    """
    base = (CFG.get("sources", {}).get("events_api", {}).get("api_base")
            or "http://127.0.0.1:8787")
    from urllib.parse import urlparse
    u = urlparse(base)
    host = u.hostname or "127.0.0.1"
    port = u.port or 8787
    return host, port


if __name__ == "__main__":
    import uvicorn

    init_db()
    host, port = _bind()
    if host not in ("127.0.0.1", "localhost"):
        print(f"⚠️  events API is binding to {host}:{port} — reachable from outside this machine.")
        print("    Make sure sources.events_api.api_key is not the placeholder value.")
    uvicorn.run(app, host=host, port=port)
