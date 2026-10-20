"""把 LifeLogging 的**全量原始数据**发布到个人站点（liveinpassion.me/life/archive）。

和 publish_life_site.py 的分工：
  · publish_life_site.py  → 聚合数字（图表用，脱敏）
  · publish_life_raw.py   → 原始数据（归档用，**不脱敏**）

发送的内容：
  journal  完整日记 Markdown（含 Time Log / Tasks / Review）
  events   原始事件长表（窗口标题、URL、坐标、meta_json 全保留）
  blocks   分类时间块（title / url / category / app 全保留）
  notes    随手记（manual.note）
  tasks    ## Tasks 解析出的任务（done / text）
  qa       问答原始 JSON（question / answer / note）
  weekly   周总结 JSON（有就发）

唯一会改动的是**凭证**：URL 里像 token/state/code/part 的 query 值、
sk-/ghp_/AKIA 这类 key、Bearer 头和 password=... 会被替换成 [redacted]。
日记、随手记、坐标、窗口标题、URL 路径、查询词的其余部分全部原样。

用法：
  python scripts/publish_life_raw.py --all                 # 回填全部
  python scripts/publish_life_raw.py --days 30             # 每晚更新近 30 天
  python scripts/publish_life_raw.py --date 2026-10-19
  python scripts/publish_life_raw.py --all --dry-run --dump C:/tmp/life-raw.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import header, load_config  # noqa: E402
from publish_life_site import open_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")

MAX_DOC_BYTES = 4 * 1024 * 1024
MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CHUNK_DOCS = 100


# ── 凭证遮盖 ──────────────────────────────────────────────────
#
# 注意：这不是“隐私脱敏”。日记、随手记、坐标、标题、URL 都原样保留；
# 这里只处理登录凭证，避免把 OAuth state / session token / 真正的 API key
# 永久公开在公网上。

SENSITIVE_EXACT = {
    "access_token", "refresh_token", "id_token", "api_key", "apikey",
    "client_secret", "authorization", "password", "passwd", "pwd",
    "session", "sessionid", "session_id", "jwt", "cookie", "otp",
    "x-amz-credential", "x-amz-signature", "x-amz-security-token",
}
SENSITIVE_SEGMENTS = {
    "token", "secret", "password", "passwd", "pwd", "auth", "authorization",
    "session", "sessionid", "sid", "state", "code", "key", "apikey",
    "signature", "sig", "credential", "credentials", "cookie", "jwt",
    "otp", "part",
}


def is_sensitive_key(key: str) -> bool:
    k = (key or "").strip().lower()
    if not k:
        return False
    if k in SENSITIVE_EXACT:
        return True
    if k.startswith(("x-amz-", "x-goog-")):
        return True
    parts = re.split(r"[_\-\s]+", k)
    return any(p in SENSITIVE_SEGMENTS for p in parts)


URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

SECRET_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{10,}"), "sk-[redacted]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[redacted]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[redacted]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*"), "Bearer [redacted]"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"client[_-]?secret|password|passwd|secret|token)\b\s*[:=]\s*"
            r"[\"']?([^\s\"'，。；,;]{6,})"
        ),
        r"\1=[redacted]",
    ),
]


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    netloc = parts.netloc
    if "@" in netloc:
        _, host = netloc.rsplit("@", 1)
        netloc = f"[redacted]@{host}"

    query = parts.query
    if query:
        changed = False
        pairs = []
        for key, value in parse_qsl(query, keep_blank_values=True):
            if is_sensitive_key(key):
                pairs.append((key, "REDACTED"))
                changed = True
            else:
                pairs.append((key, value))
        if changed:
            query = urlencode(pairs, doseq=True)

    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    # 先把文本里的 URL 找出来，逐个遮 query；再处理裸的 key/token
    value = URL_RE.sub(lambda m: redact_url(m.group(0)), value)
    for pattern, repl in SECRET_PATTERNS:
        value = pattern.sub(repl, value)
    return value


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    return value


# ── 数据读取 ──────────────────────────────────────────────────

def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def read_journal(vault_dir: Path, day: str) -> str | None:
    path = vault_dir / "05-Journal" / f"{day}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return redact_text(text)


TASK_RE = re.compile(r"^\s*-\s*\[(x|X| )\]\s*(.*)$")


def parse_tasks(journal: str | None) -> list[dict[str, Any]]:
    if not journal:
        return []
    match = re.search(r"^##\s+Tasks\s*$([\s\S]*?)(?=^##\s+|\Z)", journal, re.M)
    if not match:
        return []
    tasks = []
    for line in match.group(1).splitlines():
        m = TASK_RE.match(line)
        if not m:
            continue
        text = m.group(2).strip()
        if text:
            tasks.append({"done": m.group(1).lower() == "x", "text": text})
    return tasks


def build_events(conn: sqlite3.Connection, day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT ts_utc, domain, metric, value_num, value_text, source,
                  meta_json, ingested_at
           FROM events WHERE day_local = ?
           ORDER BY ts_utc""",
        (day,),
    ).fetchall()

    out = []
    for row in rows:
        try:
            meta = json.loads(row["meta_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        out.append({
            "ts_utc": row["ts_utc"],
            "domain": row["domain"],
            "metric": row["metric"],
            "value_num": row["value_num"],
            "value_text": redact_text(row["value_text"]),
            "source": row["source"],
            "meta": redact_obj(meta),
            "ingested_at": row["ingested_at"],
        })
    return out


def build_blocks(conn: sqlite3.Connection, day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT start_utc, end_utc, hours, category, app, title, url,
                  confidence, blackhole, source
           FROM blocks WHERE day_local = ?
           ORDER BY start_utc""",
        (day,),
    ).fetchall()
    return [{
        "start_utc": row["start_utc"],
        "end_utc": row["end_utc"],
        "hours": row["hours"],
        "category": row["category"],
        "app": row["app"],
        "title": redact_text(row["title"]),
        "url": redact_url(row["url"]) if row["url"] else None,
        "confidence": row["confidence"],
        "blackhole": bool(row["blackhole"]),
        "source": row["source"],
    } for row in rows]


def build_notes(conn: sqlite3.Connection, day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT ts_utc, value_text, source
           FROM events
           WHERE day_local = ? AND domain = 'manual' AND metric = 'note'
           ORDER BY ts_utc""",
        (day,),
    ).fetchall()
    return [{
        "ts_utc": row["ts_utc"],
        "text": redact_text(row["value_text"]),
        "source": row["source"],
    } for row in rows]


def read_qa(state_dir: Path, day: str) -> Any | None:
    path = state_dir / f"qa-{day}.json"
    if not path.exists():
        return None
    try:
        return redact_obj(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"  ⚠️  qa-{day}.json 读取失败：{exc}")
        return None


def normalize_week_id(value: str) -> str | None:
    value = (value or "").strip()
    if WEEK_RE.match(value):
        return value
    m = re.match(r"^(\d{4})W(\d{2})$", value)
    if m:
        return f"{m.group(1)}-W{m.group(2)}"
    return None


def build_weekly(state_dir: Path) -> list[dict[str, Any]]:
    docs = []
    folder = state_dir / "weeks"
    if not folder.exists():
        return docs
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  ⚠️  {path.name} 读取失败：{exc}")
            continue
        week_id = normalize_week_id(str(data.get("week") or path.stem))
        if not week_id:
            print(f"  ⚠️  {path.name} 的 week 字段不认识，跳过")
            continue
        docs.append({
            "kind": "weekly",
            "id": week_id,
            "format": "json",
            "content": json_text(redact_obj(data)),
        })
    return docs


def available_days(conn: sqlite3.Connection, state_dir: Path, vault_dir: Path) -> list[str]:
    days = {row[0] for row in conn.execute("SELECT DISTINCT day_local FROM events")}
    for path in (vault_dir / "05-Journal").glob("*.md"):
        if DAY_RE.match(path.stem):
            days.add(path.stem)
    for path in state_dir.glob("qa-*.json"):
        day = path.stem[3:]
        if DAY_RE.match(day):
            days.add(day)
    return sorted(d for d in days if DAY_RE.match(d))


def build_day(conn: sqlite3.Connection, day: str, state_dir: Path, vault_dir: Path) -> list[dict[str, Any]]:
    docs = []

    journal = read_journal(vault_dir, day)
    if journal:
        docs.append({"kind": "journal", "id": day, "format": "md", "content": journal})

    tasks = parse_tasks(journal)
    if tasks:
        docs.append({"kind": "tasks", "id": day, "format": "json", "content": json_text(tasks)})

    events = build_events(conn, day)
    if events:
        docs.append({"kind": "events", "id": day, "format": "json", "content": json_text(events)})

    blocks = build_blocks(conn, day)
    if blocks:
        docs.append({"kind": "blocks", "id": day, "format": "json", "content": json_text(blocks)})

    notes = build_notes(conn, day)
    if notes:
        docs.append({"kind": "notes", "id": day, "format": "json", "content": json_text(notes)})

    qa = read_qa(state_dir, day)
    if qa is not None:
        docs.append({"kind": "qa", "id": day, "format": "json", "content": json_text(qa)})

    return docs


# ── 发送 ──────────────────────────────────────────────────────

def doc_bytes(doc: dict[str, Any]) -> int:
    return len(doc["content"].encode("utf-8")) + 256


def chunks(docs: list[dict[str, Any]]):
    current: list[dict[str, Any]] = []
    size = 0
    for doc in docs:
        n = doc_bytes(doc)
        if n > MAX_DOC_BYTES:
            raise ValueError(f"{doc['kind']}/{doc['id']} 单份 {n} bytes 超过上限")
        if current and (size + n > MAX_CHUNK_BYTES or len(current) >= MAX_CHUNK_DOCS):
            yield current
            current, size = [], 0
        current.append(doc)
        size += n
    if current:
        yield current


def post_docs(url: str, token: str, docs: list[dict[str, Any]], timeout: float) -> int:
    sent = 0
    for chunk in chunks(docs):
        body = {"v": 1, "docs": chunk}
        for attempt in range(4):
            try:
                res = httpx.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise RuntimeError(f"network: {exc}") from exc
                time.sleep(2 ** attempt)
                continue

            if res.status_code == 200:
                sent += len(chunk)
                break

            if res.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                retry_after = res.headers.get("Retry-After", "")
                wait = min(float(retry_after), 10.0) if retry_after.isdigit() else float(2 ** attempt)
                time.sleep(wait)
                continue

            raise RuntimeError(f"HTTP {res.status_code}: {res.text[:200]}")
    return sent


def resolve_raw_url(site: dict[str, Any], override: str | None) -> str:
    base = (
        override
        or site.get("raw_ingest_url")
        or site.get("ingest_url")
        or "https://cms.liveinpassion.me/api/life/raw/ingest"
    ).strip()
    if base.endswith("/api/life/raw/ingest"):
        return base
    if base.endswith("/api/life/ingest"):
        return base[: -len("/api/life/ingest")] + "/api/life/raw/ingest"
    return base.rstrip("/") + "/api/life/raw/ingest"


def resolve_days(args: argparse.Namespace, all_days: list[str], tz: ZoneInfo, default_days: int) -> list[str]:
    if args.date:
        out = []
        for value in args.date:
            if not DAY_RE.match(value):
                raise SystemExit(f"✗ --date 需要 YYYY-MM-DD，收到 {value!r}")
            out.append(value)
        return sorted(set(out))

    if args.all:
        return all_days

    end = date.fromisoformat(args.end) if args.end else datetime.now(tz).date()
    n = max(1, min(3650, args.days or default_days))
    window = {(end - timedelta(days=i)).isoformat() for i in range(n)}
    return [d for d in all_days if d in window]


def main() -> int:
    parser = argparse.ArgumentParser(description="发布 LifeLogging 全量原始数据到个人站点")
    parser.add_argument("--all", action="store_true", help="回填所有已有日期")
    parser.add_argument("--days", type=int, default=None, help="最近 N 天（默认取 life_site.days，30）")
    parser.add_argument("--end", default=None, help="截止日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--date", action="append", default=None, help="只发布这些日期，可重复")
    parser.add_argument("--api-base", default=None, help="覆盖 raw ingest 地址")
    parser.add_argument("--dry-run", action="store_true", help="只聚合不发送")
    parser.add_argument("--dump", default=None, help="把完整 {v,docs} 写到文件（调试/本地测试）")
    args = parser.parse_args()

    cfg = load_config()
    site = cfg.get("life_site") or {}
    if not site.get("enabled", False):
        print("· life_site 未启用，跳过原始数据发布")
        return 0

    tz = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    state_dir = Path(cfg.get("state_dir") or (ROOT / "state"))
    vault_dir = Path(cfg.get("vault_path") or (ROOT / "vault"))

    try:
        conn = open_db(state_dir)
    except FileNotFoundError as exc:
        print(f"✗ {exc}")
        return 2

    all_days = available_days(conn, state_dir, vault_dir)
    days = resolve_days(args, all_days, tz, int(site.get("days") or 30))

    header(f"publish life raw · {days[0] if days else '—'} → {days[-1] if days else '—'}")
    docs: list[dict[str, Any]] = []
    for day in days:
        try:
            day_docs = build_day(conn, day, state_dir, vault_dir)
        except Exception as exc:  # noqa: BLE001 — 单日失败不影响其它天
            print(f"  ⚠️  {day} 聚合失败：{exc}")
            continue
        if not day_docs:
            continue
        docs.extend(day_docs)
        counts = {}
        for d in day_docs:
            counts[d["kind"]] = counts.get(d["kind"], 0) + 1
        total = sum(doc_bytes(d) for d in day_docs)
        print(
            f"  ✓ {day}  {total / 1024:6.1f} KiB  "
            + "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )

    weekly = build_weekly(state_dir)
    if weekly:
        docs.extend(weekly)
        print(f"  ✓ weekly  {len(weekly)} 份")

    conn.close()

    if not docs:
        print("· 没有可发布的原始数据")
        return 0

    total_bytes = sum(doc_bytes(d) for d in docs)
    print(f"  · {len(docs)} 份文档，共 {total_bytes / 1024 / 1024:.2f} MiB")

    if args.dump:
        Path(args.dump).write_text(
            json.dumps({"v": 1, "docs": docs}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  · 已写出 {args.dump}")

    if args.dry_run:
        print("· dry-run：未发送")
        return 0

    token_env = site.get("token_env") or "LIFE_INGEST_TOKEN"
    token = os.environ.get(token_env, "").strip()
    if not token:
        print(f"✗ 环境变量 {token_env} 未设置（应与 Worker secret 相同）")
        return 2

    url = resolve_raw_url(site, args.api_base)
    try:
        sent = post_docs(url, token, docs, float(site.get("timeout_s") or 60))
    except (RuntimeError, ValueError) as exc:
        print(f"✗ {exc}")
        return 1

    print(f"✓ 已发布 {sent} 份原始文档 → {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
