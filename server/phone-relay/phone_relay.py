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

**部署**：见同目录的 README.md。
"""

from __future__ import annotations

import json
import os
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

    def _authed(self) -> bool:
        got = self.headers.get("X-API-Key") or ""
        # 用常数时间比较，别用 ==（会被计时侧信道摸出 key）
        if not API_KEY or not secrets.compare_digest(got, API_KEY):
            self._send(401, {"error": "bad or missing X-API-Key"})
            return False
        return True

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

        if u.path != "/ingest/phone":
            self._send(404, {"error": "not found"})
            return
        if not self._authed():
            return

        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "bad Content-Length"})
            return
        if n <= 0 or n > MAX_BODY:
            self._send(400, {"error": f"body must be 1..{MAX_BODY} bytes"})
            return

        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._send(400, {"error": f"bad JSON: {e}"})
            return

        # 单条事件和批量都收 —— Tasker 一次发一条，别的工具可能批量发
        events = payload.get("events") if isinstance(payload, dict) else None
        if events is None and isinstance(payload, dict):
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
