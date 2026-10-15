#!/usr/bin/env python3
"""手机使用事件中继 —— 跑在常驻的 Linux 服务器上。

**为什么需要它**

    手机要上报「解锁」「应用切换」，但**不能直接发给笔记本**：
    笔记本一天只醒几小时（实测 2026-10-13：24 小时里只醒 3.4 小时），
    Tasker 的 HTTP 请求没有队列，电脑睡着时那一发就失败了，数据静默丢失。

    服务器是 24 小时开着的（手机的 GPS 位置本来就一直往它上面传），
    所以让手机只对着服务器说话，笔记本在自己醒着的时候（23:00 流水线）
    主动来拉。跟位置数据是同一套形状。

       位置：手机 ──随时──> Dawarich（本服务器） ──23:00──> 笔记本 collect_phone.py
       使用：手机 ──随时──> 本中继   （本服务器） ──23:00──> 笔记本 collect_phone_events.py

**它做了什么**

    只做两件事，刻意做得很笨：

      POST /ingest/phone   收事件，按**本地日期**追加到一个 JSONL 文件里
      GET  /events?day=…   把那一天的事件原样吐回去

    没有数据库、没有依赖（纯标准库）、没有 ORM。
    去重不在这里做 —— 笔记本电脑拉过去时事件表会用唯一索引幂等去重，
    所以这里重复写入不会造成重复数据。中继保持「写进去就不丢」就够了。

**每日提问也搭在这里**（2026-10-15 加）

    笔记本生成的「每日提问」需要有地方放，好让手机在笔记本睡着时也能取。
    服务器 24 小时开着，本来就有这个职责。四条路由：

      POST /questions      笔记本把当天的问题推上来（JSON）
      GET  /ask/pending    手机轮询「有没有题」—— **纯文本，就一个数字**
      GET  /ask            手机浏览器打开的表单页（是/否 + 备注）
      POST /answer         答案落成事件，随手机事件一起被笔记本拉走

    ⚠️ `/ask/pending` 返回纯文本是**刻意的**。Tasker 收得到 HTTP 响应体，
       但不会解析 JSON —— 那要挂 JavaScriptlet，是整个手机配置里最容易
       出错的一步（见 to_utc_iso 的注释）。返回一个数字，Tasker 直接比字符串。

    ⚠️ 中继**没有**、也不打算有任何「服务器 → 手机」的推送。手机永远是发起方。
       所以「早上问你」是手机定时来取，不是服务器推过去。

**部署**：见同目录的 README.md。
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── 配置（环境变量）─────────────────────────────────────────────────
API_KEY = os.environ.get("RELAY_API_KEY", "")
DATA_DIR = Path(os.environ.get("RELAY_DATA_DIR", "./data")).resolve()
HOST = os.environ.get("RELAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("RELAY_PORT", "8788"))
TZ_NAME = os.environ.get("RELAY_TZ", "Asia/Shanghai")
MAX_BODY = int(os.environ.get("RELAY_MAX_BODY", str(4 * 1024 * 1024)))  # 4 MB
# 文件上传（Gadgetbridge 导出的 .db）单独一个上限。
# 现在这个库 1.7 MB，但它是**只增不减**的，几年下来会到几十 MB。
MAX_UPLOAD = int(os.environ.get("RELAY_MAX_UPLOAD", str(64 * 1024 * 1024)))  # 64 MB

# 允许上传的文件名白名单。
# ⚠️ 不做白名单就会变成任意路径写入 —— 一个对外的端点拿到写权限，
#    等于把服务器交出去。这里只认死几个名字。
UPLOAD_NAMES = {"gadgetbridge.db"}

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo(TZ_NAME)
except Exception:  # noqa: BLE001
    # 服务器上没装 tzdata 就退回 UTC。只是分天的边界会偏，不能因此起不来。
    print(f"WARN: timezone {TZ_NAME!r} unavailable, falling back to UTC", file=sys.stderr)
    TZ = timezone.utc

_write_lock = threading.Lock()
# 当天已写过的 (ts_utc, metric, value_text)，用来挡掉 Tasker 的重复上报。
# 只活在内存里：进程重启后失效，那种重复交给笔记本侧的幂等索引兜住。
_seen: set[tuple[str, str, str]] = set()
_seen_day: str = ""


# JSON 是没法表达注释的，所以这里把「接受哪几种时间戳」写在函数里。
_TS_MIN = 946684800      # 2000-01-01T00:00:00Z
_TS_MAX = 4102444800     # 2100-01-01T00:00:00Z


def to_utc_iso(e: dict) -> str | None:
    """把事件里的时间戳统一成 ISO8601 UTC 字符串。

    接受两种写法，谁在就用谁：

      · ``ts_utc`` —— ISO8601，如 ``"2026-10-14T08:00:00Z"``
      · ``ts``     —— Unix **秒**（整数或浮点）

    ⚠️ **为什么要收 Unix 秒。**
       Tasker 的 ``%TIMES`` 就是 Unix 秒，而且它能**直接**用在 HTTP body 里
       做变量替换（``{"ts":%TIMES,...}``）。如果只收 ISO，手机上就得多挂一个
       JavaScriptlet 去转 —— 那是整个手机配置里最容易出错、最难排查的一步
       （Tasker 的 JS 环境里怎么取内置变量 ``%TIMES`` 坑很多）。
       让中继来转，手机侧就只剩一行 JSON。

    ⚠️ **只收秒，不做毫秒猜测。** Tasker 的 ``%TIMES`` 是秒；毫秒值会大 1000 倍。
       这里用范围检查（2000–2100）挡住明显不对的值，越界就丢掉这个事件 ——
       宁可不记，也不要静默写进去一个 1970 年的时间戳。
       丢掉的条数会在响应的 ``received`` / ``inserted`` 差额上体现出来。
    """
    raw = e.get("ts_utc")
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    raw = e.get("ts")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        secs = float(raw)
    except (ValueError, TypeError):
        return None
    if not (_TS_MIN <= secs <= _TS_MAX):
        return None
    return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_of(ts_utc: str) -> str | None:
    """UTC 时间戳 → **本地**日期。必须和笔记本侧 middleware/events_api.py 的
    `_day_local()` 算法一致，否则两边分出来的「天」不一样，拉取会漏事件。"""
    try:
        dt = datetime.fromisoformat(str(ts_utc).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).date().isoformat()


def _path_for(day: str) -> Path:
    return DATA_DIR / f"phone-{day}.jsonl"


def append_events(events: list[dict]) -> int:
    """把事件按本地日期写进当天的 JSONL。返回实际写入条数。"""
    global _seen_day
    today = datetime.now(TZ).date().isoformat()
    if today != _seen_day:
        _seen_day = today
        _seen.clear()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    with _write_lock:
        # 按天分组，一天只开一次文件
        by_day: dict[str, list[dict]] = {}
        for e in events:
            ts = to_utc_iso(e)
            day = day_of(ts) if ts else None
            if not ts or not day or not e.get("metric"):
                continue                      # 缺时间戳或 metric 的丢掉，别写坏文件
            key = (ts, str(e.get("metric")), str(e.get("value_text") or ""))
            if key in _seen:
                continue
            _seen.add(key)
            # 统一成 ts_utc 再落盘：下游（笔记本）只认这一种形状，
            # 别让它去猜这一行的时间是秒还是 ISO
            row = {k: v for k, v in e.items() if k != "ts"}
            row["ts_utc"] = ts
            by_day.setdefault(day, []).append(row)

        for day, rows in by_day.items():
            with _path_for(day).open("a", encoding="utf-8") as f:
                for e in rows:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
                    written += 1
    return written


def read_events(day: str) -> list[dict]:
    p = _path_for(day)
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # 断电写坏的半行：跳过，不要因为一行坏数据让整天的拉取失败
                continue
    return out


def upload_path(name: str) -> Path | None:
    """白名单校验 + 路径拼接。不在白名单里返回 None。"""
    if name not in UPLOAD_NAMES:
        return None
    return DATA_DIR / name


def extract_upload(body: bytes, content_type: str) -> bytes:
    """从请求体里取出文件内容。

    Tasker 的 HTTP Request 发文件有两种写法，**两种都认**：

      · 用「File To Send」+ 设 Content-Type: application/octet-stream → 裸字节
      · 用「File To Send」不设 Content-Type → Tasker 可能按 multipart/form-data 发

    第二种不认的话会静默存下一个**包着 multipart 外壳的坏文件** ——
    它看起来有几十 KB、时间戳也对，但 SQLite 打不开。
    所以这里显式解一层。

    认不出来就原样返回：宁可存一份原样的（人还能 `file` 一下看是什么），
    也好过静默存个空文件。
    """
    if "multipart/form-data" not in (content_type or "").lower():
        return body
    import email

    # 把 Content-Type 补成一个完整的 MIME 报文再解析 —— 这是标准库的正确用法
    raw = (b"Content-Type: " + content_type.encode("utf-8", "replace")
           + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    try:
        msg = email.message_from_bytes(raw)
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            payload = part.get_payload(decode=True)
            if payload:
                return payload
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[warn] multipart parse failed, storing raw body: {e}\n")
    return body


def save_upload(name: str, body: bytes) -> tuple[bool, str]:
    """原子写入，并保留上一版。

    先写 `.tmp` 再 rename —— 上传中途断线时，**旧的那份完整的还在**。
    直接覆盖的话，一次截断的传输就会把好文件换成坏文件，
    而笔记本那边只会看到「SQLite 打不开」，看不出是传输断的。

    上一版留成 `.prev`：万一新传上来的那份是坏的，还有东西可回退。
    """
    dest = upload_path(name)
    if dest is None:
        return False, f"{name} is not in the upload allow-list"

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with _write_lock:
        with tmp.open("wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        if dest.exists():
            prev = dest.with_suffix(dest.suffix + ".prev")
            try:
                prev.unlink(missing_ok=True)
                os.replace(dest, prev)
            except OSError:
                pass                       # 留不下旧版不是致命问题
        os.replace(tmp, dest)
    return True, f"{len(body)} bytes"


# ── 每日提问 ────────────────────────────────────────────────────────
#
# 笔记本生成问题 → 推到这里 → 手机早上来取 → 答完提交 → 答案变成事件，
# 随手机使用事件一起被笔记本拉走。详见模块开头的说明。
#
# 存放形状（都在 DATA_DIR 下）：
#   questions-<day>.json    当天要问的问题（笔记本推上来的）
#   questions-<day>.seen    「已经提醒过手机了」的标记，见 pending 路由
#   phone-<day>.jsonl       答案就混在手机事件里（domain=question）
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_NOTE = 500


def questions_path(day: str) -> Path | None:
    """问题文件的路径。day 形状不对就返回 None —— 这是挡路径穿越的那道门。"""
    if not _DAY_RE.match(day or ""):
        return None
    return DATA_DIR / f"questions-{day}.json"


def save_questions(day: str, payload: dict) -> tuple[bool, str]:
    """原子写入当天的问题。同 save_upload：先写 .tmp 再 replace。"""
    dest = questions_path(day)
    if dest is None:
        return False, "day must be YYYY-MM-DD"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    tmp = dest.with_suffix(".json.tmp")
    with _write_lock:
        with tmp.open("wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    return True, f"{len(data)} bytes"


def load_latest_questions() -> dict | None:
    """取**最新**的一份问题。没有就返回 None。

    取最新而不是取今天：笔记本可能昨晚生成、手机今天才来取。
    """
    files = sorted(DATA_DIR.glob("questions-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def answered_ids(days: int = 7) -> set[str]:
    """最近若干天里已答过的题号。

    答案落在「作答那天」的事件文件里（不伪造成提问那天的），所以这里要往回
    扫几天。题号本身已经带了提问日期（``<day>-<hash>``），不会撞。
    """
    today = datetime.now(TZ).date()
    out: set[str] = set()
    for i in range(days):
        day = (today - timedelta(days=i)).isoformat()
        for e in read_events(day):
            if e.get("domain") == "question" and e.get("metric") == "answer":
                parts = str(e.get("value_text") or "").split("|", 2)
                if parts and parts[0]:
                    out.add(parts[0])
    return out


def answers_to_events(answers: list) -> list[dict]:
    """把表单交上来的答案转成事件。

    抽成独立函数是为了能测：这段编码是**手机和笔记本之间唯一的约定**，
    一旦和 apply_answers.py 的解析对不上，答案就会静默变成一堆没人认得的
    字符串（而且两边都不会报错）。
    """
    now = int(datetime.now(timezone.utc).timestamp())
    events: list[dict] = []
    for a in answers:
        if not isinstance(a, dict):
            continue
        qid = str(a.get("id") or "").strip()
        val = str(a.get("value") or "").strip().lower()
        if not qid or val not in ("yes", "no"):
            continue
        # 备注压成一行，并把竖线换成斜杠 —— value_text 用竖线分段，
        # 让备注里的竖线或换行混进去会把编码弄坏。
        note = " ".join(str(a.get("note") or "").replace("|", "/").split())
        events.append({
            "ts": now,
            "domain": "question",
            "metric": "answer",
            # 形状：<题号>|<yes|no>|<备注>。题号里自带提问日期（<day>-<hash>），
            # 所以不用再单独存 day。答案落的是**作答那天**，不伪造成提问那天
            # —— 见 answered_ids() 的注释。
            "value_text": f"{qid}|{val}|{note[:MAX_NOTE]}",
            "source": "relay",
        })
    return events


def pending_questions() -> tuple[dict | None, list[dict]]:
    """返回 (最新那份问题, 其中还没答的)。"""
    q = load_latest_questions()
    if not q:
        return None, []
    done = answered_ids()
    items = [x for x in (q.get("questions") or []) if isinstance(x, dict)
             and x.get("id") and x.get("id") not in done]
    return q, items


# 问答页模板。用占位符 + str.replace 而不是 f-string —— 里面全是 CSS/JS 的
# 大括号，f-string 会把它们当成插值，改一次错一次。
_ASK_PAGE = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日提问 __QDAY__</title>
<style>
 :root { color-scheme: light dark; }
 body { font: 16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
        margin: 0 auto; max-width: 42rem; padding: 1rem 1rem 7rem; }
 h1 { font-size: 1.1rem; }
 .hint { font-size: .82rem; opacity: .65; }
 ul { list-style: none; padding: 0; margin: 0; }
 .q { border: 1px solid rgba(128,128,128,.35); border-radius: .6rem;
      padding: .8rem; margin: 0 0 .8rem; }
 .t { margin: 0 0 .35rem; }
 .e { margin: 0 0 .6rem; font-size: .82rem; opacity: .65; }
 .r { display: flex; gap: .5rem; margin-bottom: .5rem; }
 .r button { flex: 1; padding: .7rem; font-size: 1rem; border-radius: .5rem;
             border: 1px solid rgba(128,128,128,.5); background: transparent;
             color: inherit; font: inherit; }
 .r button[aria-pressed="true"] { background: #2f7d32; color: #fff; border-color: #2f7d32; }
 .r button.no[aria-pressed="true"] { background: #8a3b3b; border-color: #8a3b3b; }
 .n { width: 100%; box-sizing: border-box; padding: .5rem; font: inherit;
      border-radius: .5rem; border: 1px solid rgba(128,128,128,.5);
      background: transparent; color: inherit; }
 .bar { position: fixed; left: 0; right: 0; bottom: 0; padding: .7rem 1rem;
        background: canvas; border-top: 1px solid rgba(128,128,128,.35); }
 .bar button { width: 100%; padding: .9rem; font-size: 1rem; border: 0;
               border-radius: .5rem; background: #2f7d32; color: #fff; font: inherit; }
 #msg { text-align: center; margin: .5rem 0 0; font-size: .88rem; min-height: 1.2em; }
</style>
<h1>每日提问 · __QDAY__</h1>
<p class="hint">答完点最下面的「提交」。没作答的题不会提交，下次还会问。</p>
<ul id="qs">__ITEMS__</ul>
<div class="bar"><button id="send">提交</button><p id="msg"></p></div>
<script>
var KEY = __KEY__, QDAY = __QDAY_JSON__;
document.querySelectorAll('.q').forEach(function (li) {
  li.querySelectorAll('.r button').forEach(function (b) {
    b.addEventListener('click', function () {
      li.querySelectorAll('.r button').forEach(function (o) {
        o.setAttribute('aria-pressed', 'false');
      });
      b.setAttribute('aria-pressed', 'true');
    });
  });
});
document.getElementById('send').addEventListener('click', function () {
  var answers = [];
  document.querySelectorAll('.q').forEach(function (li) {
    var picked = li.querySelector('.r button[aria-pressed="true"]');
    if (!picked) { return; }
    answers.push({ id: li.dataset.id, value: picked.dataset.v,
                   note: li.querySelector('.n').value.trim() });
  });
  var msg = document.getElementById('msg');
  if (!answers.length) { msg.textContent = '还没有作答。'; return; }
  msg.textContent = '提交中…';
  fetch('/answer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-API-Key': KEY },
    body: JSON.stringify({ day: QDAY, answers: answers })
  }).then(function (r) {
    return r.json().then(function (j) { return { ok: r.ok, status: r.status, j: j }; });
  }).then(function (res) {
    if (!res.ok) { msg.textContent = '失败：' + (res.j.error || res.status); return; }
    msg.textContent = '已提交 ' + res.j.inserted + ' 条，可以关掉了。';
    document.querySelectorAll('.q').forEach(function (li) {
      if (li.querySelector('.r button[aria-pressed="true"]')) { li.remove(); }
    });
  }).catch(function (e) { msg.textContent = '失败：' + e; });
});
</script>
</html>
"""


def render_ask_page(q: dict, items: list[dict], key: str) -> str:
    """把问题渲染成一个自包含的表单页（无外部资源，手机浏览器直接打开）。"""
    chunks = []
    for it in items:
        qid = html.escape(str(it.get("id") or ""), quote=True)
        text = html.escape(str(it.get("text") or ""))
        ev = html.escape(str(it.get("evidence") or ""))
        ev_html = f'<p class="e">{ev}</p>' if ev else ""
        chunks.append(
            f'<li class="q" data-id="{qid}">'
            f'<p class="t">{text}</p>{ev_html}'
            '<div class="r">'
            '<button type="button" data-v="yes">做了</button>'
            '<button type="button" class="no" data-v="no">没做</button>'
            '</div>'
            f'<input class="n" type="text" placeholder="备注（可选）" maxlength="{MAX_NOTE}">'
            "</li>"
        )
    qday = str(q.get("day") or "")
    return (_ASK_PAGE
            .replace("__ITEMS__", "\n".join(chunks) or '<li class="q">没有问题。</li>')
            .replace("__QDAY_JSON__", json.dumps(qday))
            .replace("__QDAY__", html.escape(qday))
            .replace("__KEY__", json.dumps(key)))


class Handler(BaseHTTPRequestHandler):
    server_version = "lifelogging-phone-relay/1.0"

    def log_message(self, fmt, *args):        # noqa: A002
        # 默认实现会把请求打到 stderr（systemd 日志）。保留但加时间戳。
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        got = self.headers.get("X-API-Key") or ""
        # 用常数时间比较，别用 ==（会被计时侧信道摸出 key）
        if not API_KEY or not secrets.compare_digest(got, API_KEY):
            self._send(401, {"error": "bad or missing X-API-Key"})
            return False
        return True

    def _authed_query(self, query: str) -> bool:
        """同 _authed，但额外认 ``?key=``。**只给 /ask 用。**

        为什么要开这个口子：`/ask` 是在**手机浏览器**里打开的，
        普通导航请求没法带自定义请求头。所以那一页只能靠查询参数带 key。
        代价是 key 会进浏览器历史 —— 走的是 Tailscale 内网，可以接受，
        但要写进 README。其它端点一律仍然只认请求头。
        """
        got = self.headers.get("X-API-Key") or ""
        if not got:
            got = (parse_qs(query).get("key") or [""])[0]
        if not API_KEY or not secrets.compare_digest(got, API_KEY):
            self._send(401, {"error": "bad or missing X-API-Key"})
            return False
        return True

    def _json_body(self, limit: int = MAX_BODY) -> tuple[dict, str | None]:
        """读并解析 JSON 请求体。返回 (payload, 出错信息)。

        `/ingest/phone` `/questions` `/answer` 三个端点共用 —— 它们对
        「多大算太大」「坏 JSON 怎么办」的判断必须一致，抄三份迟早抄歪。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}, "bad Content-Length"
        if n <= 0 or n > limit:
            return {}, f"body must be 1..{limit} bytes"
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return {}, f"bad JSON: {e}"
        if not isinstance(payload, dict):
            return {}, "expected a JSON object"
        return payload, None

    def do_GET(self) -> None:                 # noqa: N802
        u = urlparse(self.path)

        if u.path == "/health":
            # ⚠️ health 不鉴权：要能被 Tailscale / systemd 的探活直接打。
            #    它只暴露「今天有几条」和「上传的文件多新」，不含任何内容。
            today = datetime.now(TZ).date().isoformat()
            payload = {
                "status": "ok",
                "tz": TZ_NAME,
                "today": today,
                "today_events": len(read_events(today)),
            }
            # 手机有没有在传文件 —— 排查时第一眼要看的就是这个
            for name in sorted(UPLOAD_NAMES):
                p = upload_path(name)
                if p and p.exists():
                    st = p.stat()
                    payload[f"file_{name}"] = {
                        "bytes": st.st_size,
                        "updated": datetime.fromtimestamp(st.st_mtime, tz=TZ)
                                           .strftime("%Y-%m-%d %H:%M"),
                    }
            self._send(200, payload)
            return

        if u.path == "/events":
            if not self._authed():
                return
            day = (parse_qs(u.query).get("day") or [""])[0]
            if not day or len(day) != 10:
                self._send(400, {"error": "?day=YYYY-MM-DD is required"})
                return
            evs = read_events(day)
            self._send(200, {"day": day, "count": len(evs), "events": evs})
            return

        if u.path.startswith("/files/"):
            if not self._authed():
                return
            p = upload_path(u.path[len("/files/"):])
            if p is None or not p.exists():
                self._send(404, {"error": "no such file"})
                return
            data = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if u.path == "/ask/pending":
            # ⚠️ **纯文本，就一个数字。** Tasker 收得到响应体但不解析 JSON，
            #    要挂 JavaScriptlet —— 那是手机配置里最容易出错的一步。
            #    返回一个数字，Tasker 直接比字符串，不需要任何 JS。
            if not self._authed():
                return
            q, left = pending_questions()
            if not q or not left:
                self._send_text(200, "0")
                return
            qday = str(q.get("day") or "")
            marker = DATA_DIR / f"questions-{qday}.seen"
            if (parse_qs(u.query).get("again") or [""])[0] == "1":
                marker.unlink(missing_ok=True)
            if marker.exists():
                # 已经提醒过一次了。**读一次就标记** —— 这样 Tasker 每 30 分钟
                # 轮询也不会重复弹通知，而手机侧不需要保存任何状态。
                self._send_text(200, "0")
                return
            try:
                marker.write_text(datetime.now(TZ).isoformat(), encoding="utf-8")
            except OSError:
                pass                      # 留不下标记不是致命的，最多多提醒一次
            self._send_text(200, str(len(left)))
            return

        if u.path == "/ask":
            if not self._authed_query(u.query):
                return
            q, left = pending_questions()
            if not q:
                self._send(404, {"error": "no questions have been pushed yet"})
                return
            # 页面里的 JS 要用 key 去 POST /answer，所以把它嵌进去。
            # 头部鉴权进来时（curl 调试）就用配置里那把。
            key = (parse_qs(u.query).get("key") or [""])[0] or API_KEY
            body = render_ask_page(q, left, key).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:                # noqa: N802
        u = urlparse(self.path)

        # ── 文件上传（Gadgetbridge 导出的 .db）─────────────────────
        if u.path.startswith("/upload/"):
            if not self._authed():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send(400, {"error": "bad Content-Length"})
                return
            if n <= 0 or n > MAX_UPLOAD:
                self._send(400, {"error": f"body must be 1..{MAX_UPLOAD} bytes"})
                return
            body = self.rfile.read(n)
            ok, detail = save_upload(
                u.path[len("/upload/"):],
                extract_upload(body, self.headers.get("Content-Type") or ""),
            )
            self._send(200 if ok else 400, {"ok": ok, "detail": detail})
            return

        if u.path == "/questions":
            if not self._authed():
                return
            payload, err = self._json_body()
            if err:
                self._send(400, {"error": err})
                return
            day = str(payload.get("day") or "")
            qs = payload.get("questions")
            if not isinstance(qs, list) or not qs:
                self._send(400, {"error": "questions must be a non-empty list"})
                return
            ok, detail = save_questions(day, payload)
            self._send(200 if ok else 400,
                       {"ok": ok, "detail": detail, "count": len(qs)})
            return

        if u.path == "/answer":
            if not self._authed():
                return
            payload, err = self._json_body()
            if err:
                self._send(400, {"error": err})
                return
            answers = payload.get("answers")
            if not isinstance(answers, list) or not answers:
                self._send(400, {"error": "answers must be a non-empty list"})
                return
            events = answers_to_events(answers)
            if not events:
                self._send(400, {"error": "no usable answers (each needs id + yes/no)"})
                return
            try:
                written = append_events(events)
            except OSError as e:
                self._send(500, {"error": f"write failed: {e}"})
                return
            self._send(200, {"inserted": written, "received": len(events)})
            return

        if u.path != "/ingest/phone":
            self._send(404, {"error": "not found"})
            return
        if not self._authed():
            return

        payload, err = self._json_body()
        if err:
            self._send(400, {"error": err})
            return

        # 单条事件和批量都收 —— Tasker 一次发一条，别的工具可能批量发
        events = payload.get("events")
        if events is None:
            events = [payload]
        if not isinstance(events, list):
            self._send(400, {"error": "expected an event object or {'events': [...]}"})
            return

        try:
            written = append_events([e for e in events if isinstance(e, dict)])
        except OSError as e:
            self._send(500, {"error": f"write failed: {e}"})
            return

        self._send(200, {"inserted": written, "received": len(events)})


def main() -> None:
    if not API_KEY:
        print("FATAL: RELAY_API_KEY is not set.", file=sys.stderr)
        print("       生成一个：python3 -c \"import secrets;print(secrets.token_urlsafe(32))\"",
              file=sys.stderr)
        sys.exit(1)
    if API_KEY == "REPLACE_WITH_THE_RELAY_KEY":
        print("FATAL: RELAY_API_KEY is still the placeholder from the README.", file=sys.stderr)
        sys.exit(1)

    if HOST in ("0.0.0.0", "::"):
        # 手机走 Tailscale 就够了，绑通配地址会把公网和局域网一起开出去。
        print("WARN: binding to a wildcard address. Prefer the Tailscale IP; "
              "0.0.0.0 also exposes this on the public interface.", file=sys.stderr)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"phone-relay listening on {HOST}:{PORT}  tz={TZ_NAME}  data={DATA_DIR}",
          file=sys.stderr)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
