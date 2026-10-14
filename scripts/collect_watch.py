"""手表数据：从服务器中继取回 Gadgetbridge 导出的 SQLite。

手机上的 Gadgetbridge 把数据库导出后，由 Tasker 传到**服务器**上的中继
（和手机使用事件走同一个服务），本脚本在流水线里把它取回本地，
紧接着 `middleware/ingest_gadgetbridge.py` 读它、归一进事件表。

**为什么绕服务器而不直连**

    本机是笔记本，一天只醒几小时。手机传文件的时刻本机多半没开，
    直连必然丢掉一大半。服务器 24 小时开着 —— 手机只对它说话，
    本机醒着的时候来拉。跟位置数据、手机使用事件是同一套形状。

**为什么漏传一次不要紧**

    Gadgetbridge 的导出是**全量快照**（整个库），不是增量流。
    所以某次上传失败不会丢数据 —— 下一次传上去的还是完整的。
    这也是「Tasker 没有队列」这个短板在这里不成立的原因。

**原子性**

    先下到 `.tmp` 再替换。下载中途断了的话，**本地那份完整的还在** ——
    直接覆盖会让本机只剩半个 SQLite，而下游只会报「数据库打不开」，
    看不出是传输断的。

用法：
  python scripts/collect_watch.py --date today
"""

from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Day, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])

# 中继上的文件名（服务端有白名单，只认这几个）
REMOTE_NAME = "gadgetbridge.db"


def relay_client() -> tuple[str, str] | None:
    """返回 (base_url, api_key)，没配好返回 None。"""
    cfg = (CFG.get("sources") or {}).get("phone_relay") or {}
    if not cfg.get("enabled"):
        print("-> phone relay disabled (config.yaml -> sources.phone_relay.enabled)")
        return None
    base = (cfg.get("api_base") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    if not base or "REPLACE_WITH" in base or "REPLACE_WITH" in key:
        print("  ⚠️  phone_relay is enabled but still has placeholder values in config.yaml.")
        return None
    return base, key


def fetch(base: str, key: str) -> bytes | None:
    """从中继取回文件。返回 None 表示**取不到**（没传过 / 连不上 / 鉴权失败）。

    ⚠️ 「取不到」和「取到了但是空的」必须分开，别都返回 `b""`。
       否则「手机还没传过」和「服务器挂了」在输出上长得一样，
       跟这个项目里踩过的其它坑是同一类。
    """
    try:
        r = httpx.get(f"{base}/files/{REMOTE_NAME}",
                      headers={"X-API-Key": key}, timeout=120.0)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Could not reach the phone relay at {base}: {e}")
        print("      On the server:  sudo systemctl status lifelogging-relay")
        return None
    if r.status_code == 401:
        print("  ⚠️  phone relay rejected the API key (401).")
        return None
    if r.status_code == 404:
        print("  . Relay has no gadgetbridge.db yet — the phone has not uploaded one.")
        print("    (Normal before the Tasker upload task is set up.)")
        return None
    if r.status_code != 200:
        print(f"  ⚠️  phone relay returned {r.status_code}: {r.text[:200]}")
        return None
    return r.content


def save(data: bytes, dest: Path) -> str:
    """原子写入。返回 'written' 或 'unchanged'。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.read_bytes() == data:
        return "unchanged"
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return "written"


def main() -> None:
    from common import cli_day

    day = Day.parse(cli_day(), TZ)
    header(f"Collect watch · {day.iso}")

    conn = relay_client()
    if conn is None:
        return
    base, key = conn

    data = fetch(base, key)
    if data is None:
        return

    if not data.startswith(b"SQLite format 3\x00"):
        # 中继存下来的东西不是 SQLite。最可能是 Tasker 发送方式不对
        # （比如把文件当普通文本字段发了），这时候早点说出来，
        # 好过让下游报一句「数据库打不开」。
        print(f"  ⚠️  Downloaded {len(data):,} bytes but it is NOT a SQLite file.")
        print(f"      First bytes: {data[:32]!r}")
        print("      Check the Tasker upload task: it must send the file as a file")
        print("      ('File To Send'), not paste its contents into the Body field.")
        return

    dest = Path(CFG["sources"]["gadgetbridge"]["db_path"])
    result = save(data, dest)
    if result == "unchanged":
        print(f"  . Local copy already up to date ({len(data):,} bytes)")
    else:
        print(f"  OK  Saved {len(data):,} bytes -> {dest}")


if __name__ == "__main__":
    main()
