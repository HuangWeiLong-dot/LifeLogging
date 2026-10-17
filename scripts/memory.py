#!/usr/bin/env python3
"""长期记忆 · 第一层：检索（按需取回相关的几条，不是全塞）。

**它解决什么**

    写日记的模型每次只拿到**当天**的 timeline，历史是断的 —— 它不知道你三个月前
    说过「今年要少接外包」。这个模块把历史建成可检索的索引，在调用模型前按
    当下的话题取回 top-k 条，塞进 prompt。

**为什么是 FTS5，不是向量库**

    一年三百多篇日记。FTS5 在 SQLite 里是毫秒级，零额外进程、零额外依赖，
    而且结果**可解释** —— 你能看到它召回了哪几条、为什么。向量检索要到
    千级、万级文档才明显占优，现在上是给自己加负担。

**⚠️ 中文必须逐字切分，这是实测出来的**

    - `tokenize='unicode61'` 默认把一整串汉字当成**一个 token** —— 搜「外包」
      命中 0 条。
    - `tokenize='trigram'` 只对 **≥3 个字符**的查询有效 —— 搜「外包」「熬夜」
      仍然命中 0 条。而中文里有意义的词大多正是两个字。
    - **逐字插入空格 + unicode61**：两字、三字、跨词全都能命中，且零依赖。
      所以存进 FTS 的是切分后的文本，查的时候也要用同一种切分。

**索引放哪**

    `state/events.db`（已开 WAL，采集与查询本来就并发）。
    新加两张表：`memory_docs`（原文）和 `memory_fts`（切分后的全文索引）。
    整套索引都是**派生物**：删掉重跑一次就有，不参与备份。

用法：
  python scripts/memory.py reindex            # 重建索引（跑一次几毫秒）
  python scripts/memory.py search "外包"      # 手动看看会召回什么
  python scripts/memory.py search "外包" -k 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import load_config  # noqa: E402

CFG = load_config()

# 日记正文里 frontmatter 之后的部分才是「内容」。frontmatter 里的
# total_hours / ai_cost 这些东西索引进去只会制造噪音。
_FM_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)

# 中日韩统一表意文字（基本区 + 扩展 A）。逐字切开，其余原样。
_CJK_RE = re.compile(r"([㐀-䶿一-鿿])")

# 每条记忆取多少字进 prompt。**宁缺毋滥** —— 一份两万字的历史塞进去，
# 模型的注意力会被稀释，主任务（写今天）反而变差。
EXCERPT_CHARS = 400


def db_path() -> Path:
    # ⚠️ 允许用环境变量指走 —— 冒烟测试要用临时库，否则测试会往你真实的
    #    事件表里写索引，跑一次就把测试数据留在了生产库里。
    explicit = os.environ.get("LIFELOGGING_MEMORY_DB")
    if explicit:
        return Path(explicit)
    return Path(CFG["state_dir"]) / "events.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.execute("PRAGMA busy_timeout=5000;")   # 和 events_api 并发时的礼貌
    return conn


def seg(text: str) -> str:
    """把中文逐字切开，其余（英文/数字/标点）原样。

    `我今年想少接外包` → `我 今 年 想 少 接 外 包`
    这样 FTS5 的 unicode61 会把它切成单字 token，两个字以上的查询才查得到。
    """
    return _CJK_RE.sub(r" \1 ", text or "")


def to_fts_query(text: str, joiner: str = "AND") -> str:
    """把输入变成 FTS5 查询：每个词单独成短语，词之间用 `joiner` 连接。

    ⚠️ 用**短语**（引号）而不是散开的词：中文里 `外` 和 `包` 分开之后到处都能
       匹配上，做检索会召回一堆噪音。`"外 包"` 要求它们相邻。

    ⚠️ `joiner` 默认 AND，但**检索时通常要先用 AND、失败了再用 OR**（见
       search()）。因为检索词是从今天的多个信号拼出来的（几个 app 名、地点、
       笔记），要求一篇文档同时包含全部几乎永远命中 0 —— 实测踩到过：
       7 个 app 名 AND 起来，一条都召回不了。
    """
    terms = [t for t in (text or "").split() if t.strip()]
    parts: list[str] = []
    for t in terms[:8]:                       # 最多 8 个词，别让查询无限长
        s = seg(t).replace('"', '""').strip()
        s = " ".join(s.split())
        if s:
            parts.append(f'"{s}"')
    return f" {joiner} ".join(parts)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_docs (
               doc_key TEXT PRIMARY KEY,
               day     TEXT NOT NULL,
               kind    TEXT NOT NULL,
               text    TEXT NOT NULL,
               source  TEXT
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_day ON memory_docs(day)")
    conn.commit()


def _journal_docs() -> list[dict[str, Any]]:
    """vault 里的日记正文。

    ⚠️ 日记**不在** events.db 里 —— 它是 Obsidian 库里的 markdown。所以这里直接
       扫文件。少了这一块，检索就等于把最重要的历史全漏掉。
    """
    vault = Path(CFG["vault_path"]) / "05-Journal"
    out: list[dict[str, Any]] = []
    if not vault.exists():
        return out
    for p in sorted(vault.glob("*.md")):
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError:
            continue
        body = _FM_RE.sub("", raw).strip()
        if not body:
            continue
        out.append({"doc_key": f"journal:{p.stem}", "day": p.stem,
                    "kind": "journal", "text": body, "source": str(p)})
    return out


def _event_docs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """飞书里记的笔记 + 问答答案 —— **第一手材料**，价值最高。"""
    out: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            """SELECT day_local, metric, value_text, ts_utc FROM events
               WHERE domain = 'manual' ORDER BY ts_utc"""
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for day, metric, text, ts in rows:
        text = str(text or "").strip()
        if text:
            out.append({"doc_key": f"note:{ts}", "day": str(day),
                        "kind": "note", "text": text, "source": "feishu"})

    try:
        rows = conn.execute(
            """SELECT day_local, value_text, ts_utc FROM events
               WHERE domain = 'question' AND metric = 'answer'
               ORDER BY ts_utc"""
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for day, value_text, ts in rows:
        parts = str(value_text or "").split("|", 2)
        if len(parts) < 2:
            continue
        qid, value = parts[0], parts[1]
        note = parts[2] if len(parts) > 2 else ""
        text = f"{value} {note}".strip()
        if text:
            out.append({"doc_key": f"answer:{qid}", "day": str(day),
                        "kind": "answer", "text": text, "source": qid})
    return out


def reindex() -> tuple[int, int]:
    """重建整份索引。返回 (文档数, 日记数)。

    ⚠️ **全量重建**，不做增量。三百来篇文档重建一次是毫秒级，而增量要维护
       「哪些变了」的状态 —— 那个状态一旦和现实对不上，索引就会静默地缺内容，
       比重建麻烦得多。
    """
    conn = connect()
    try:
        ensure_schema(conn)
        journals = _journal_docs()
        docs = journals + _event_docs(conn)

        conn.execute("DELETE FROM memory_docs")
        conn.execute("DELETE FROM memory_fts")
        for d in docs:
            conn.execute(
                "INSERT OR REPLACE INTO memory_docs"
                " (doc_key, day, kind, text, source) VALUES (?,?,?,?,?)",
                (d["doc_key"], d["day"], d["kind"], d["text"], d["source"]),
            )
            conn.execute(
                "INSERT INTO memory_fts (doc_key, day, kind, seg)"
                " VALUES (?,?,?,?)",
                (d["doc_key"], d["day"], d["kind"], seg(d["text"])),
            )
        conn.commit()
        return len(docs), len(journals)
    finally:
        conn.close()


def search(query: str, k: int = 5) -> list[dict[str, Any]]:
    """按关键词召回 top-k 条记忆。查不到就返回空列表（不猜）。"""
    if not to_fts_query(query):
        return []
    conn = connect()
    try:
        ensure_schema(conn)
        rows: list = []
        # 先 AND（精确），召回为空再 OR（宽松）。直接 OR 会把「只共享一个
        # 常见词」的文档也拉进来；直接 AND 又几乎永远命中 0（检索词是从今天
        # 多个信号拼出来的）。两步走兼得两者。
        for joiner in ("AND", "OR"):
            fts_q = to_fts_query(query, joiner=joiner)
            try:
                rows = conn.execute(
                    """SELECT f.doc_key, f.day, f.kind,
                              substr(d.text, 1, ?) AS excerpt
                       FROM memory_fts f
                       JOIN memory_docs d ON d.doc_key = f.doc_key
                       WHERE memory_fts MATCH ?
                       ORDER BY bm25(memory_fts)
                       LIMIT ?""",
                    (EXCERPT_CHARS, fts_q, max(1, k)),
                ).fetchall()
            except sqlite3.OperationalError as e:
                print(f"WARN 检索失败：{e}", file=sys.stderr)
                return []
            if rows:
                break
        return [{"doc_key": r[0], "day": r[1], "kind": r[2], "excerpt": r[3]}
                for r in rows]
    finally:
        conn.close()


def build_query(tl: dict, notes: list[str] | None = None) -> str:
    """从「今天」拼一个检索词。

    优先用**你自己的话**（笔记），其次地点、应用名 —— 它们是最能区分
    「今天和哪段历史像」的东西。窗口标题太碎，不用。
    """
    parts: list[str] = []
    for n in (notes or [])[:2]:
        parts.append(str(n))
    for l in (tl.get("location") or [])[:2]:
        if l.get("place"):
            parts.append(str(l["place"]))
    for b in (tl.get("blocks") or [])[:30]:
        # ⚠️ 去掉 `.exe` —— 日记正文里不会写扩展名，留着它等于永远匹配不上。
        app = re.sub(r"\.exe$", "", str(b.get("app") or "").strip(), flags=re.I)
        if app and app not in parts:
            parts.append(app)
        if len(parts) >= 6:
            break
    return " ".join(parts[:6])


def main() -> int:
    ap = argparse.ArgumentParser(description="长期记忆 · 检索层")
    ap.add_argument("cmd", choices=("reindex", "search"))
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("-k", "--top-k", type=int, default=5)
    args = ap.parse_args()

    if args.cmd == "reindex":
        n, j = reindex()
        print(f"索引已重建：{n} 篇文档（其中日记 {j} 篇）→ {db_path()}")
        return 0

    rows = search(args.query, args.top_k)
    if not rows:
        print("没有召回任何东西。")
        return 0
    for r in rows:
        print(f"[{r['day']}] ({r['kind']}) {r['doc_key']}")
        print(f"    {r['excerpt'][:160].replace(chr(10), ' ')}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
