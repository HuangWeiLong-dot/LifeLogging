#!/usr/bin/env python3
"""学到的「应用名 → 分类」映射 —— 手写规则表的自动补充。

**它解决什么**

    `normalize.APP_RULES` 是一张手写的表。没写进去的应用（新装的、游戏本体、
    小众工具）一律落到 `Other` —— 时间照样记下来，但分类是空的。

    这个模块是那张表的**自动补充**：对累积够久的未知应用，问一次模型，
    把答案存进 `state/app_map.json`。之后它和手写规则**完全一样** ——
    免费、确定、可审计。

**为什么是「学一次 + 缓存」，不是「每个切片都问模型」**

    一天约 1000 个窗口切片，去重后也有几百个不同的「应用 + 标题」。
    每个都问一次模型，一天就是几十上百次调用，而写一整篇日记才一次。
    而且同一个应用今天判 Work、明天判 Study，日记就没法对账了。

    所以：**按应用学一次**，学到之后走本地字典查询。一周大概几个新应用，
    成本可以忽略，而分类结果从此是确定的、可复盘的。

**为什么它不是一个「模型」**

    没有标注数据，训练一个分类器不现实。这里的「学习」是：
    模型判一次 → 人可读的 JSON 缓存 → 下次当规则用。
    学错了能一眼看出来（`source: "learned"`），改一行就永久生效。

**文件形状**（`state/app_map.json`，gitignored）

    {
      "eldenring.exe": {
        "category": "Leisure",
        "confidence": "learned",
        "learned_at": "2026-10-17T21:03:00",
        "sample_titles": ["ELDEN RING", "..."],
        "minutes_seen": 42.0
      }
    }

    `sample_titles` / `minutes_seen` 是**判定依据**，留着是为了将来能回答
    「它当初凭什么是 Leisure」。删掉它们不影响分类。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ⚠️ 允许用环境变量指走 —— 冒烟测试要用临时文件，
#    而且将来服务器侧也可能用到同一份映射。
_ENV_PATH = "LIFELOGGING_APP_MAP"

_cache: dict[str, dict] | None = None
_cache_mtime: float | None = None


def map_path() -> Path:
    explicit = os.environ.get(_ENV_PATH)
    if explicit:
        return Path(explicit)
    # 延迟 import：common 会读 config，而 app_map 被 normalize 在模块级 import，
    # 不想让「配置文件缺失」变成「整个 normalize 起不来」。
    from common import load_config

    return Path(load_config()["state_dir"]) / "app_map.json"


def normalise_key(app: str) -> str:
    """进程名做键：小写、去空白。Windows 的进程名不区分大小写。"""
    return (app or "").strip().lower()


def load() -> dict[str, dict]:
    """读整张表。按 mtime 缓存 —— classify 每个切片都会调用它。"""
    global _cache, _cache_mtime
    p = map_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _cache, _cache_mtime = {}, None
        return _cache
    if _cache is not None and _cache_mtime == mtime:
        return _cache
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        _cache = raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        # 表坏了不该让整条流水线停下来 —— 退回「只有手写规则」。
        print(f"WARN app_map 读不了，忽略学到的映射：{e}", file=sys.stderr)
        _cache = {}
    _cache_mtime = mtime
    return _cache


def lookup(app: str) -> str | None:
    """查一个应用学到的分类。没有就返回 None（调用方继续按 Other 处理）。"""
    entry = load().get(normalise_key(app))
    if not isinstance(entry, dict):
        return None
    cat = str(entry.get("category") or "").strip()
    return cat or None


def remember(app: str, category: str, *, sample_titles: list[str] | None = None,
             minutes_seen: float | None = None) -> bool:
    """把一次判定写进表。返回是否**新增**（已存在的不覆盖）。

    ⚠️ **已存在的绝不覆盖。** 手动改过的条目比模型判的更可信；让自动学习
       把人工修正盖回去，是最容易让人放弃这套东西的一种失败。
    """
    key = normalise_key(app)
    if not key or not category:
        return False
    table = load()
    if key in table:
        return False

    table[key] = {
        "category": category,
        "confidence": "learned",
        "learned_at": _now_iso(),
        "sample_titles": [str(t)[:120] for t in (sample_titles or [])][:8],
        "minutes_seen": round(float(minutes_seen or 0.0), 1),
    }
    p = map_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(table, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)
    # 让下一次 load() 重新读盘（mtime 变了也行，但缓存指针要清掉更稳）
    global _cache, _cache_mtime
    _cache, _cache_mtime = None, None
    return True


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")
