"""归一化 + 确定性归类 —— 把原始事件压成人能读的「时间块」。

设计取向：**能用规则解决的就不要喂给 LLM。**
应用名 → 分类 用规则表（免费、可解释、可审计）；
只有规则判不出来的（比如「你在 VSCode 里待了 4 小时到底在写什么」）才交给 LLM 看窗口标题去判。

这直接决定了每天的成本。规则覆盖率越高，账单越低。

用法：
  python scripts/normalize.py --date today
输出：
  state/timeline-YYYY-MM-DD.json   给 generate_journal.py 消费
  state/timeline-YYYY-MM-DD.md     给人看，用于排查规则是否合理
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Day, EventsClient, header, load_config  # noqa: E402

CFG = load_config()
TZ = ZoneInfo(CFG["timezone"])

# 合并阈值：相邻两块间隔小于这个值就并成一块
MERGE_GAP = timedelta(minutes=5)
# 小于这个时长的块不进日记（碎片太多反而看不清）
MIN_BLOCK = timedelta(minutes=6)

# ── 规则表：应用名/窗口标题关键词 → 分类 ────────────────────────────
# 匹配是「小写子串包含」，按顺序第一个命中生效，所以把更具体的放前面。
APP_RULES: list[tuple[str, list[str]]] = [
    ("Work", [
        "visual studio", "vscode", "code.exe", "pycharm", "intellij", "idea64",
        "goland", "webstorm", "clion", "rider", "android studio", "xcode",
        "terminal", "powershell", "cmd.exe", "windowsterminal", "git bash", "wsl",
        "docker", "postman", "insomnia", "dbeaver", "navicat", "pgadmin", "datagrip",
        "excel", "winword", "powerpnt", "wps", "outlook", "foxmail",
        "slack", "teams", "dingtalk", "feishu", "lark", "zoom", "tencent meeting",
        "notion", "obsidian", "typora", "confluence", "jira", "trello",
        "figma", "photoshop", "illustrator", "blender", "premiere", "after effects",
        "github", "gitlab", "bitbucket", "sourcegraph",
        "claude", "cursor", "copilot", "chatgpt", "chat.openai",
    ]),
    ("Study", [
        "anki", "zotero", "mendeley", "endnote", "calibre", "kindle",
        "coursera", "udemy", "edx", "bilibili", "youtube",  # 这些要 LLM 复核，见下
        "arxiv", "jstor", "scholar", "pubmed", "wikipedia",
        "duolingo", "baicizhan", "shanbay",
        "acrobat", "sumatrapdf", "foxit", "pdf",
    ]),
    ("Transport", [
        "amap", "gaode", "baidumap", "google maps", "maps", "didi", "uber",
        "12306", "ctrip", "trip.com", "metro", "subway",
    ]),
    ("Leisure", [
        "steam", "epic games", "battle.net", "riot", "genshin", "原神",
        "netflix", "iqiyi", "youku", "tencent video", "spotify", "netease music",
        "qqmusic", "music", "potplayer", "vlc", "mpv",
        "weibo", "zhihu", "douban", "reddit", "twitter", "x.com",
        "instagram", "tiktok", "douyin", "threads",
        "wechat", "weixin", "qq", "telegram", "discord",  # 社交/休闲边界模糊，LLM 复核
    ]),
    ("Health", [
        "keep", "strava", "garmin", "zepp", "mi fitness", "huawei health",
        "myfitnesspal", "薄荷健康", "薄荷", "apple health", "health connect",
    ]),
    ("Life", [
        "taobao", "jd.com", "jingdong", "pinduoduo", "tmall", "amazon",
        "alipay", "wechat pay", "meituan", "ele.me", "饿了么", "cainiao",
        "bank", "招商银行", "工商银行", "银行", "wallet",
        "settings", "控制面板", "explorer.exe", "文件资源管理器",
    ]),
    ("Social", [
        "wechat", "weixin", "telegram", "whatsapp", "signal", "line",
        "linkedin", "facebook", "instagram dm",
    ]),
]

# 系统/无意义的窗口，直接丢弃
NOISE = [
    "lockapp", "logonui", "screensaver", "widgets.exe", "searchapp",
    "shellexperiencehost", "startmenuexperiencehost",
]

# ── 浏览器单独处理 ──────────────────────────────────────────────────
# 浏览器是最常见的应用，但它**本质上是有歧义的**：
# 同一个 Edge 窗口里可能开着 GitHub，也可能开着 B 站。
# 按应用名分类没意义，必须看**标题**。
# 标题也没线索的，标成 ambiguous 交给 LLM —— 而不是丢进「其他」然后
# 变成 unmatched 列表里的常驻噪音。
BROWSERS = [
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "arc.exe", "chromium.exe", "360se.exe", "qqbrowser.exe",
]

# 标题关键词 → 分类。顺序有意义：先匹配到的赢。
TITLE_HINTS: list[tuple[str, list[str]]] = [
    ("Work", [
        "github", "gitlab", "bitbucket", "jira", "confluence", "notion",
        "figma", "pull request", "merge request", "issue #", "dashboard",
        "admin console", "控制台", "后台管理", "gmail", "outlook",
        "docs.google", "飞书", "钉钉", "slack", "企业微信",
        "stack overflow", "stackoverflow", "localhost",
    ]),
    ("Study", [
        "教程", "课程", "讲义", "课件", "论文", "文献", "复习",
        "documentation", "docs", "tutorial", "lecture", "handbook",
        "arxiv", "pubmed", "wikipedia", "维基", "手册", "reference",
        "how to", "如何", "指南", "入门", "api reference",
    ]),
    ("Leisure", [
        "bilibili", "哔哩", "youtube", "netflix", "爱奇艺", "腾讯视频",
        "优酷", "芒果", "抖音", "微博", "知乎", "小红书", "贴吧",
        "reddit", "twitter", "x.com", "instagram", "twitch", "steam",
        "小说", "漫画", "直播", "游戏", "视频",
    ]),
    ("Life", [
        "淘宝", "京东", "天猫", "拼多多", "亚马逊", "amazon",
        "支付宝", "美团", "饿了么", "银行", "账单", "缴费",
        "订单", "购物车", "快递", "菜谱",
    ]),
]


@dataclass
class Block:
    start: datetime
    end: datetime
    category: str
    app: str
    titles: list[str]
    seconds: float
    source: str = "activitywatch"
    blackhole: bool = False
    confidence: str = "rule"   # rule | llm | default
    note: str = ""
    url: str = ""              # 浏览器块才有（来自 aw-watcher-web）

    @property
    def hhmm(self) -> str:
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"

    @property
    def hours(self) -> float:
        return round(self.seconds / 3600.0, 2)


@dataclass
class Timeline:
    day: str
    blocks: list[Block] = field(default_factory=list)
    by_category: dict[str, float] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    location: list[dict[str, Any]] = field(default_factory=list)
    phone: dict[str, Any] = field(default_factory=dict)
    unmatched_apps: dict[str, float] = field(default_factory=dict)
    web: list[dict[str, Any]] = field(default_factory=list)
    track: dict[str, Any] = field(default_factory=dict)
    total_hours: float = 0.0


# ── 规则匹配 ────────────────────────────────────────────────────────


def classify(app: str, title: str) -> tuple[str, str]:
    """返回 (分类, 置信度)。

    置信度含义：
      rule      规则确定
      ambiguous 规则判不准，**必须**由 LLM 看标题重新判断
      default   规则完全不认识的应用
    """
    hay = f"{app} {title}".lower()

    if any(n in hay for n in NOISE):
        return ("__drop__", "rule")

    # 浏览器先走标题判断。这是覆盖率最高的一条路径 —— 大多数人的大部分
    # 时间都在浏览器里，而浏览器只看应用名是判不出来的。
    app_l = app.lower()
    if any(b in app_l for b in BROWSERS):
        title_l = title.lower()
        for cat, hints in TITLE_HINTS:
            if any(h in title_l for h in hints):
                return (cat, "rule")
        # 标题也没线索 → 交给 LLM，别猜
        return ("Other", "ambiguous")

    for cat, keys in APP_RULES:
        for k in keys:
            if k in hay:
                # 工作类里出现 bilibili/youtube/wechat 这种，规则判不准，标成待复核
                ambiguous = k in ("bilibili", "youtube", "wechat", "weixin", "qq",
                                  "telegram", "discord", "reddit", "zhihu", "weibo")
                return (cat, "ambiguous" if ambiguous else "rule")

    return ("Other", "default")


# ── 从 ActivityWatch 拉原始切片 ─────────────────────────────────────


def fetch_window_slices(day: Day) -> list[dict[str, Any]]:
    """直接读 ActivityWatch 的原始窗口事件 —— 带时间戳，才能拼成时间块。

    注意：collect_desktop.py 写进事件表的是**按应用聚合的日总量**（给面板用），
          这里读的是**原始切片**（给时间轴用）。两者用途不同，不重复。
    """
    base = CFG["sources"]["activitywatch"]["api_base"].rstrip("/")
    try:
        buckets = httpx.get(f"{base}/api/0/buckets/", timeout=15.0).json()
    except Exception as e:  # noqa: BLE001
        print(f"  WARN Cannot reach ActivityWatch: {e}")
        return []

    out: list[dict[str, Any]] = []
    for bucket in [b for b in buckets if b.startswith("aw-watcher-window_")]:
        try:
            evs = httpx.get(
                f"{base}/api/0/buckets/{bucket}/events",
                params={
                    "start": day.start_local.astimezone(timezone.utc).isoformat(),
                    "end": day.end_local.astimezone(timezone.utc).isoformat(),
                    "limit": -1,
                },
                timeout=60.0,
            ).json()
        except Exception:  # noqa: BLE001
            continue

        for ev in evs:
            d = ev.get("data") or {}
            app = (d.get("app") or "").strip()
            title = (d.get("title") or "").strip()
            dur = float(ev.get("duration") or 0)
            if not app or dur <= 0 or dur > 4 * 3600:
                continue
            ts = _parse(ev.get("timestamp"))
            if not ts:
                continue
            # AW 的 timestamp 是切片开始时间，duration 是持续秒数
            if ts.date() != day.d or ts < day.start_local or ts > day.end_local:
                continue
            out.append({"ts": ts, "dur": dur, "app": app, "title": title})

    out.sort(key=lambda x: x["ts"])
    return out


def _parse(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


# ── 浏览器页面切片（需要 aw-watcher-web 扩展）────────────────────────


def fetch_web_slices(day: Day) -> list[dict[str, Any]]:
    """页面级切片，带 URL。

    为什么值得单独拉：窗口标题在浏览器里经常只有标签名甚至只有浏览器名，
    而 URL 是强得多的分类信号 —— `github.com/acme/pull/12` 比任何标题都明确。
    分类准确率的提升主要来自这里，不是来自标题。
    """
    base = CFG["sources"]["activitywatch"]["api_base"].rstrip("/")
    try:
        buckets = httpx.get(f"{base}/api/0/buckets/", timeout=15.0).json()
    except Exception:  # noqa: BLE001
        return []

    out: list[dict[str, Any]] = []
    for bucket in [b for b in buckets if b.startswith("aw-watcher-web")]:
        try:
            evs = httpx.get(
                f"{base}/api/0/buckets/{bucket}/events",
                params={
                    "start": day.start_local.astimezone(timezone.utc).isoformat(),
                    "end": day.end_local.astimezone(timezone.utc).isoformat(),
                    "limit": -1,
                },
                timeout=60.0,
            ).json()
        except Exception:  # noqa: BLE001
            continue

        for ev in evs:
            d = ev.get("data") or {}
            if d.get("incognito"):
                continue                       # 隐私窗口显式跳过
            ts = _parse(ev.get("timestamp"))
            dur = float(ev.get("duration") or 0)
            if not ts or dur <= 0 or dur > 4 * 3600:
                continue
            out.append({
                "ts": ts,
                "end": ts + timedelta(seconds=dur),
                "url": (d.get("url") or "").strip(),
                "title": (d.get("title") or "").strip(),
            })

    out.sort(key=lambda x: x["ts"])
    return out


def attach_web_urls(slices: list[dict[str, Any]], web: list[dict[str, Any]]) -> int:
    """把 URL 贴到浏览器时间块上。

    两个 bucket 是各自独立采样的，时间戳天然差几秒，**不能按时间戳直接 join**。

    ⚠️ 特别注意：不要要求 web 记录「严格覆盖」切片的开始时刻。
       window 在 T 开始、web 在 T+3s 开始是常态，严格覆盖这时不成立，
       结果是 URL 一条都贴不上 —— 而且不报错，只是静静地什么都没发生。
       正确做法是取与整个切片**重叠最多**的那条。

    两边都已排序 → 用一个只前进的指针，每次只在指针附近看 3 条，O(n+m)。
    """
    if not web or not slices:
        return 0

    j = 0
    attached = 0
    for s in slices:
        if not any(b in s["app"].lower() for b in BROWSERS):
            continue

        start = s["ts"]
        end = start + timedelta(seconds=s.get("dur", 0))
        while j + 1 < len(web) and web[j + 1]["ts"] <= start:
            j += 1

        best = None
        best_overlap = timedelta(0)
        for k in (j - 1, j, j + 1):
            if not (0 <= k < len(web)):
                continue
            w = web[k]
            overlap = min(end, w["end"]) - max(start, w["ts"])
            if overlap > best_overlap:
                best, best_overlap = w, overlap

        if best is None:
            continue
        if best["title"]:
            s["title"] = best["title"]
        s["url"] = best["url"]
        attached += 1

    return attached


# ── 切片 → 时间块 ───────────────────────────────────────────────────


def build_blocks(slices: list[dict[str, Any]]) -> tuple[list[Block], dict[str, float]]:
    blocks: list[Block] = []
    unmatched: dict[str, float] = {}

    for s in slices:
        # 把 URL 一起喂给规则 —— TITLE_HINTS 里的 github/arxiv/stackoverflow
        # 这些关键词在 URL 里比在标题里可靠得多
        cat, conf = classify(s["app"], f"{s.get('title', '')} {s.get('url', '')}")
        if cat == "__drop__":
            continue
        if conf == "default":
            unmatched[s["app"]] = unmatched.get(s["app"], 0.0) + s["dur"]

        start = s["ts"]
        end = start + timedelta(seconds=s["dur"])

        if blocks:
            last = blocks[-1]
            gap = start - last.end
            # 同类 且 间隔小 → 合并
            if last.category == cat and gap <= MERGE_GAP:
                last.end = max(last.end, end)
                last.seconds += s["dur"]
                if s["title"] and s["title"] not in last.titles:
                    last.titles.append(s["title"])
                if not last.url and s.get("url"):
                    last.url = s["url"]
                if conf == "ambiguous":
                    last.confidence = "ambiguous"
                continue

        blocks.append(
            Block(
                start=start,
                end=end,
                category=cat,
                app=s["app"],
                titles=[s["title"]] if s["title"] else [],
                seconds=s["dur"],
                confidence=conf if conf == "rule" else ("ambiguous" if conf == "ambiguous" else "default"),
                url=s.get("url", ""),
            )
        )

    return blocks, unmatched


def attach_other_domains(tl: Timeline, day: Day) -> None:
    """把位置、健康、手机事件挂到时间轴上。"""
    client = EventsClient(CFG)
    if not client.ping():
        print("  ⚠️  events API not running, skipping multi-source merge")
        return

    try:
        evs = client.query(start=day.start_utc, end=day.end_utc, limit=200_000)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Events query failed: {e}")
        return

    for e in evs:
        dom, met = e["domain"], e["metric"]
        if dom == "location" and met == "visit":
            tl.location.append({
                "place": e.get("value_text") or "",
                "minutes": e.get("value_num") or 0,
                "ts": e.get("ts_utc"),
            })
        elif dom == "location" and met == "tracked_points":
            # 原始点的日汇总。
            # ⚠️ 必须单独处理：visits 是「停留检测」推导的，点位太少时是空的。
            #    只认 visit 的话，出门但没形成停留的一天，位置分区会莫名其妙是空的 ——
            #    而实际上你确实出去过。这正是 01-Location 一直是空文件的原因。
            meta = e.get("meta") or {}
            tl.track = {
                "points": int(e.get("value_num") or 0),
                "main_place": e.get("value_text") or "",
                "span_km": meta.get("span_km"),
                "lat_center": meta.get("lat_center"),
                "lon_center": meta.get("lon_center"),
                "first_utc": meta.get("first_point_utc"),
                "last_utc": meta.get("last_point_utc"),
            }
        elif dom == "health":
            # 同一指标取最后一次（当天汇总值）
            if met in ("sleep_hours", "steps"):
                tl.health[met] = e.get("value_num")
            elif met.endswith("_avg"):
                tl.health[met] = e.get("value_num")
        elif dom == "phone":
            tl.phone[met] = tl.phone.get(met, 0) + 1
        elif dom == "web" and met == "site_usage":
            tl.web.append({
                "site": e.get("value_text") or "",
                "minutes": round((e.get("value_num") or 0) / 60.0, 1),
            })

    tl.location.sort(key=lambda x: x["minutes"], reverse=True)
    tl.web.sort(key=lambda x: x["minutes"], reverse=True)


# ── 输出 ────────────────────────────────────────────────────────────


def render_markdown(tl: Timeline) -> str:
    lines = [f"# Timeline {tl.day}", ""]

    if tl.health:
        lines.append("## Health")
        for k, v in tl.health.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    if tl.location:
        lines.append("## Location (by time spent)")
        for loc in tl.location[:12]:
            lines.append(f"- {loc['minutes']:.0f} min @ {loc['place']}")
        lines.append("")

    if tl.track:
        t = tl.track
        lines.append("## Raw tracking")
        lines.append(f"- {t.get('points', 0)} points"
                     + (f", span {t['span_km']} km" if t.get("span_km") is not None else "")
                     + (f", main place {t['main_place']}" if t.get("main_place") else ""))
        lines.append("")

    if tl.phone:
        lines.append("## Phone")
        for k, v in tl.phone.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    if tl.web:
        lines.append("## Top sites (browser extension)")
        for w in tl.web[:20]:
            lines.append(f"- {w['minutes']:.0f} min @ {w['site']}")
        lines.append("")

    lines.append("## Time blocks")
    lines.append("")
    lines.append("| Time | Duration | Category | App | Confidence | Window title |")
    lines.append("|---|---|---|---|---|---|")
    for b in tl.blocks:
        if b.end - b.start < MIN_BLOCK:
            continue
        title = " / ".join(b.titles[:2])[:60].replace("|", "/")
        lines.append(
            f"| {b.hhmm} | {b.hours}h | {b.category} | {b.app[:20]} | "
            f"{b.confidence} | {title} |"
        )

    if tl.unmatched_apps:
        lines.append("")
        lines.append("## Apps the rules didn't recognise (consider adding to APP_RULES)")
        lines.append("")
        for app, secs in sorted(tl.unmatched_apps.items(), key=lambda x: -x[1])[:20]:
            lines.append(f"- {app}: {secs/3600:.1f}h")

    return "\n".join(lines)


def post_blocks_to_db(day: Day, blocks: list[Block]) -> None:
    """把分类结果写进事件表 —— 这是长期可视化的数据源。

    为什么不能只留 state/timeline-*.json：
      跨月跨年的分析要先写代码把一堆文件拼起来，Grafana 之类的工具更是完全读不到。
      进表之后「过去 90 天每天各分类多少小时」就是一行 SQL 的事。

    尽力而为：事件表没在跑就算了，不阻断主流程。
    """
    client = EventsClient(CFG)
    if not client.ping():
        print("  . events API not running; blocks not stored (this day will be missing from graphs)")
        return

    payload = []
    for b in blocks:
        if b.end - b.start < MIN_BLOCK:
            continue
        payload.append({
            "day_local": day.iso,
            "start_utc": b.start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc": b.end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hours": b.hours,
            "category": b.category,
            "app": b.app[:80],
            "title": " / ".join(b.titles[:2])[:200],
            "url": b.url[:300] or None,
            "confidence": b.confidence,
            "blackhole": b.blackhole,
        })

    if not payload:
        return
    try:
        r = httpx.post(
            f"{client.base}/blocks",
            headers=client.headers,
            json={"blocks": payload},
            timeout=60.0,
        )
        r.raise_for_status()
        res = r.json()
        print(f"  OK  Blocks stored: {res.get('inserted', 0)} rows"
              f" (replaced {res.get('deleted', 0)} old rows)")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  Failed to store blocks: {e}")


def main() -> None:
    from common import cli_day

    day = Day.parse(cli_day(), TZ)
    header(f"Normalize · {day.iso}")

    print("-> Reading ActivityWatch raw slices")
    slices = fetch_window_slices(day)
    if not slices:
        print("  ⚠️  No window slices. ActivityWatch is off, or you have not used the computer today.")
    else:
        print(f"  ✓ {len(slices)} slices, span "
              f"{slices[0]['ts'].strftime('%H:%M')} → {slices[-1]['ts'].strftime('%H:%M')}")

    print("-> Reading browser page slices")
    web = fetch_web_slices(day)
    if not web:
        print("  . No aw-watcher-web data (extension not installed). "
              "Classification falls back to window titles only; accuracy will be lower.")
    else:
        n = attach_web_urls(slices, web)
        print(f"  ✓ {len(web)} page records, attached URLs to {n} browser blocks")

    blocks, unmatched = build_blocks(slices)
    print(f"  ✓ Merged into {len(blocks)} time blocks")

    tl = Timeline(day=day.iso, blocks=blocks, unmatched_apps=unmatched)

    print("-> Attaching location / health / phone data")
    attach_other_domains(tl, day)

    # 统计（只算够长的块）
    by_cat: dict[str, float] = {}
    for b in blocks:
        if b.end - b.start < MIN_BLOCK:
            continue
        by_cat[b.category] = by_cat.get(b.category, 0.0) + b.hours
    tl.by_category = {k: round(v, 2) for k, v in
                      sorted(by_cat.items(), key=lambda x: -x[1])}
    tl.total_hours = round(sum(tl.by_category.values()), 2)

    state = Path(CFG["state_dir"])
    state.mkdir(parents=True, exist_ok=True)

    json_path = state / f"timeline-{day.iso}.json"
    payload = {
        "day": tl.day,
        "total_hours": tl.total_hours,
        "by_category": tl.by_category,
        "health": tl.health,
        "location": tl.location,
        "phone": tl.phone,
        "web": tl.web,
        "track": tl.track,
        "unmatched_apps": tl.unmatched_apps,
        "blocks": [
            {
                "time": b.hhmm,
                "hours": b.hours,
                "category": b.category,
                "app": b.app,
                "titles": b.titles[:3],
                "confidence": b.confidence,
                "blackhole": b.blackhole,
            }
            for b in blocks
            if b.end - b.start >= MIN_BLOCK
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = state / f"timeline-{day.iso}.md"
    md_path.write_text(render_markdown(tl), encoding="utf-8")

    print("-> Storing classified blocks (the data source for visualization)")
    post_blocks_to_db(day, blocks)

    print(f"\n-> Category totals:")
    for cat, h in tl.by_category.items():
        bar = "█" * int(h * 2)
        print(f"    {cat:6s} {bar} {h}h")
    print(f"    TOTAL {tl.total_hours}h")

    ambiguous = [b for b in blocks if b.confidence == "ambiguous"]
    if ambiguous:
        print(f"\n-> {len(ambiguous)} blocks the rules could not decide; they go to the AI")

    if unmatched:
        top = sorted(unmatched.items(), key=lambda x: -x[1])[:5]
        print(f"\n-> Apps the rules did not recognise (adding them to APP_RULES saves money):")
        for app, secs in top:
            print(f"    {app}: {secs/3600:.1f}h")
        print("  Tip: each app you cover with a rule is data you stop paying to send to the LLM.")

    print(f"\nOK  Written:\n   {json_path}\n   {md_path}")


if __name__ == "__main__":
    main()
