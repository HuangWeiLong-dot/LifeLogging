#!/usr/bin/env python3
"""服务器上的长期记忆检索 —— 把**服务器已经有的**日记正文变成可检索的。

**为什么不是直接用笔记本上的 memory.py**

    `scripts/memory.py` 索引的是 Obsidian 里的日记和笔记本的 events.db ——
    那两样都不在服务器上。但服务器**有**一样东西：`close_day.py` 写好的
    日记正文（`journal-output-<day>.json`），而且它和机器人共用同一个
    `/var/lib/lifelogging`。所以这里不搬 vault，只索引它已经在的那部分。

    分词规则和 FTS5 表结构**照抄** `memory.py`（`seg` / `to_fts_query`），
    不另写一套 —— 否则「上个月」在两个入口里会搜出不同的东西。

**检索到什么就用什么，检索不到就说没有**

    这里不做「相似度猜测」：FTS5 先 AND 后 OR，都空就返回 []，
    由提示词负责说「记录里没有」。和机器人其它部分的「答不了比答错好」一致。

**只读、可重建**

    `memory-server.db` 是纯派生数据：删掉、换机器、重建都行。原始正文在
    `journal-output-*.json` 里。

用法（服务器上调试）：
  python recall.py refresh
  python recall.py search "上次觉得累"
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# 复用笔记本那套的分词/查询构造 —— 见文件头。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from memory import seg, to_fts_query  # noqa: E402

STATE_DIR = Path(os.environ.get("FEISHU_STATE_DIR", "/var/lib/lifelogging"))
DB = STATE_DIR / "memory-server.db"

EXCERPT_CHARS = 240
MAX_DOCS = 400

_DAY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# seg() 把汉字逐字切开（每个字前后各一个空格），snippet() 返回的也是切开
# 的那份。显示前要把汉字/中文标点之间的空格收回去，否则摘要是
# 「今  天  见  了  老  王 ， 聊  了」。英文单词之间的空格保留。
_CJK_CHARS = r"[一-鿿，。！？；：、（）《》「」『』…—～]"
_CJK_GAP = re.compile(rf"(?<={_CJK_CHARS}) +(?={_CJK_CHARS})")


def _pretty(s: str) -> str:
    return _CJK_GAP.sub("", s or "")


def _signature(state_dir: Path) -> str:
    """源文件的「指纹」：文件名 + mtime + 大小。变了才重建索引。

    ⚠️ 必须包含**所有**被索引的来源。只盯 journal-output 的话，新写进
       phone-*.jsonl 的中文回答永远进不了索引 —— 而且不会报错，只会
       表现为「日记搜得到、回答搜不到」。2026-10-21 实测踩到。
    """
    h = hashlib.sha1()
    for pattern in ("journal-output-*.json", "phone-*.jsonl", "questions-*.json"):
        for p in sorted(state_dir.glob(pattern)):
            try:
                st = p.stat()
            except OSError:
                continue
            h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
    return h.hexdigest()


def _load_docs(state_dir: Path) -> list[dict]:
    docs: list[dict] = []
    days: set[str] = set()

    for p in sorted(state_dir.glob("journal-output-*.json"))[-MAX_DOCS:]:
        m = _DAY_RE.search(p.name)
        if not m:
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        body = str(payload.get("body") or "").strip()
        if not body:
            continue
        day = str(payload.get("day") or m.group(1))
        days.add(day)
        docs.append({"doc_key": f"journal-{day}", "day": day,
                     "kind": "journal", "text": body})

    # ⚠️ **日记正文是英文写的**（close_day 的产出），而用户是用中文问的。
    #    只索引日记的话，「我上次觉得累是什么时候」一个字都匹配不上 ——
    #    2026-10-21 在服务器上实测就是 0 条。所以把中继上他自己的**中文回答
    #    和笔记**也索引进来：那才是和问题同一种语言的原话。
    for p in sorted(state_dir.glob("phone-*.jsonl"))[-MAX_DOCS:]:
        m = _DAY_RE.search(p.name)
        if not m:
            continue
        day = m.group(1)
        days.add(day)
        text = _qa_doc(state_dir, day)
        if text:
            docs.append({"doc_key": f"qa-{day}", "day": day,
                         "kind": "qa", "text": text})
    return docs


def _question_texts(state_dir: Path, day: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in (f"questions-{day}.json", f"questions-live-{day}.json"):
        p = state_dir / name
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for q in payload.get("questions") or []:
            if isinstance(q, dict) and q.get("id"):
                out[str(q["id"])] = str(q.get("text") or "")
    return out


def _qa_doc(state_dir: Path, day: str) -> str:
    """把一天的中文回答/笔记拼成一篇可检索的文档。

    ⚠️ 跳过机器人自己写的「【今日复盘】」笔记：那是整份日报（上千字、
       包含当天所有回答），索引进去会让每一天都命中一大堆关键词，
       把真正的原话淹没。
    """
    p = state_dir / f"phone-{day}.jsonl"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    qtexts = _question_texts(state_dir, day)
    parts: list[str] = []
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        dom, met = e.get("domain"), e.get("metric")
        if dom == "manual" and met == "note":
            text = str(e.get("value_text") or "").strip()
            if text and not text.startswith("【今日复盘】"):
                parts.append(f"笔记：{text}")
        elif dom == "question" and met == "answer":
            qid, _, rest = str(e.get("value_text") or "").partition("|")
            value, _, note = rest.partition("|")
            if not value.strip():
                continue
            q = qtexts.get(qid.strip(), "")
            item = f"问：{q}\n答：{value.strip()}" if q else f"答：{value.strip()}"
            if note.strip():
                item += f"（备注：{note.strip()}）"
            parts.append(item)
    return "\n".join(parts)


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_docs (
               doc_key TEXT PRIMARY KEY,
               day     TEXT NOT NULL,
               kind    TEXT NOT NULL,
               text    TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
               doc_key UNINDEXED,
               day     UNINDEXED,
               kind    UNINDEXED,
               seg,
               tokenize='unicode61'
           )"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS memory_meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.commit()


def _meta_get(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT v FROM memory_meta WHERE k=?", (key,)).fetchone()
    return str(row[0]) if row else ""


def refresh(*, state_dir: Path | None = None, db: Path | None = None) -> int:
    """源变了就重建。返回索引里的文档数。"""
    state_dir = state_dir or STATE_DIR
    db = db or DB
    sig = _signature(state_dir)
    try:
        conn = _connect(db)
    except sqlite3.Error:
        return 0
    try:
        _ensure_schema(conn)
        if _meta_get(conn, "signature") == sig:
            return int(_meta_get(conn, "count") or 0)
        docs = _load_docs(state_dir)
        with conn:
            conn.execute("DELETE FROM memory_docs")
            conn.execute("DELETE FROM memory_fts")
            for d in docs:
                conn.execute(
                    "INSERT OR REPLACE INTO memory_docs(doc_key, day, kind, text) "
                    "VALUES (?,?,?,?)",
                    (d["doc_key"], d["day"], d["kind"], d["text"]))
                conn.execute(
                    "INSERT INTO memory_fts(doc_key, day, kind, seg) VALUES (?,?,?,?)",
                    (d["doc_key"], d["day"], d["kind"], seg(d["text"])))
            conn.execute("INSERT OR REPLACE INTO memory_meta(k,v) VALUES ('signature',?)",
                         (sig,))
            conn.execute("INSERT OR REPLACE INTO memory_meta(k,v) VALUES ('count',?)",
                         (str(len(docs)),))
        return len(docs)
    except sqlite3.Error as e:
        print(f"WARN recall 索引失败：{e}", file=sys.stderr)
        return 0
    finally:
        conn.close()


# 问句里的功能词。**只删不承载信息的词**，不删「写 / 手机 / 累」这类
# 内容词 —— 删多了查询就空了，检索会静默变成「什么都没找到」。
_STOP = (
    "上个月", "那几天", "前阵子", "前段时间", "为什么", "什么", "时候",
    "上次", "以前", "之前", "过去", "几天", "今天", "昨天", "前天", "现在",
    "看看", "一下", "记得", "是不是", "有没有", "多久", "哪天",
    "我", "你", "他", "她", "它", "的", "了", "是", "在", "有", "和", "与",
    "就", "都", "也", "还", "很", "太", "不", "没", "吗", "呢", "吧", "啊",
    "呀", "嘛", "这", "那", "哪", "谁", "么", "过", "给",
)
_RUN_RE = re.compile(r"[一-鿿]+|[A-Za-z0-9]+")


def _query_variants(text: str) -> tuple[str, str]:
    """把一句自然语言问句变成 (精确查询, 宽松查询)。

    ⚠️ 不能把整句直接塞进 FTS5：中文逐字切开后，「我上次觉得累是什么时候」
       会变成一个要求 12 个字连续相邻的短语 —— 几乎永远命中 0。
       所以先删功能词，再拿剩下的词做 AND；AND 空了就退到 OR。
    """
    clean = text or ""
    for w in sorted(_STOP, key=len, reverse=True):
        clean = clean.replace(w, " ")
    runs = [r for r in _RUN_RE.findall(clean) if r.strip()]
    if not runs:
        return "", ""

    and_q = to_fts_query(" ".join(runs), joiner="AND")
    # 宽松查询：整词 + 单字。单字很吵，但 OR + bm25 只把它们当弱证据；
    # 对「觉得累」这种和日记里「很累」措辞对不上的情况，单字是唯一能命中的路。
    chars = [ch for r in runs if not r.isascii() for ch in r]
    ascii_terms = [r for r in runs if r.isascii()]
    terms: list[str] = []
    for t in runs + chars + ascii_terms:
        if t not in terms:
            terms.append(t)
    or_q = to_fts_query(" ".join(terms[:12]), joiner="OR")
    return and_q, or_q


def search(query: str, k: int = 5, *, state_dir: Path | None = None,
           db: Path | None = None) -> list[dict]:
    """按关键词召回过去的日记。查不到返回 []（不猜）。"""
    if not (query or "").strip():
        return []
    state_dir = state_dir or STATE_DIR
    db = db or DB
    refresh(state_dir=state_dir, db=db)
    try:
        conn = _connect(db)
    except sqlite3.Error:
        return []
    try:
        _ensure_schema(conn)
        rows: list = []
        # 先 AND 后 OR，和 memory.py 同一条策略：AND 精确但容易 0，
        # OR 宽松但会把只共享一个常见字的文档也拉进来。
        for fts_q in _query_variants(query):
            if not fts_q:
                continue
            try:
                rows = conn.execute(
                    """SELECT d.doc_key, d.day, d.kind,
                              snippet(memory_fts, 3, '〔', '〕', '…', ?) AS excerpt
                       FROM memory_fts f
                       JOIN memory_docs d ON d.doc_key = f.doc_key
                       WHERE memory_fts MATCH ?
                       ORDER BY bm25(memory_fts)
                       LIMIT ?""",
                    (EXCERPT_CHARS // 4, fts_q, max(1, k)),
                ).fetchall()
            except sqlite3.OperationalError as e:
                print(f"WARN recall 检索失败：{e}", file=sys.stderr)
                return []
            if rows:
                break
        return [{"doc_key": r[0], "day": r[1], "kind": r[2],
                 "excerpt": _pretty(r[3])} for r in rows]
    finally:
        conn.close()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="服务器端长期记忆检索")
    ap.add_argument("cmd", choices=("refresh", "search"))
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("-k", "--top-k", type=int, default=5)
    args = ap.parse_args()

    if args.cmd == "refresh":
        n = refresh()
        print(f"索引已重建：{n} 篇日记 → {DB}")
        return 0

    hits = search(args.query, args.top_k)
    if not hits:
        print("没有召回任何东西。")
        return 0
    for h in hits:
        print(f"[{h['day']}] {h['excerpt']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
