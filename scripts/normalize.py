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
# 小于这个时长的块并进相邻块（见 fold_short_blocks），**不是被删掉**。
#
# ⚠️ 这个值管的是「时间块颗粒度」，不是「保留多少时间」——折叠后总量不变。
#    原来它是 6 分钟，而且是个**过滤器**：直接 continue 丢掉，于是实测一天
#    2.34 小时的活动只剩 0.41 小时入账，日记模型只能靠编造时长填空。
#
#    定成 2 分钟的依据：实测这台机器的窗口切片平均每 13 秒一变（最长的一段
#    才 88 秒），6 分钟以上连续同分类的块几乎不存在。2 分钟能留下约 20 个块、
#    覆盖整段活动；再高就会把几段不同的事塌成一块，反而看不出当天做了什么。
MIN_BLOCK = timedelta(minutes=2)

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

    @property
    def duration(self) -> timedelta:
        """**实际活动时长**（切片秒数之和），不是首尾跨度。

        ⚠️ 阈值必须比这个，不能比 `end - start`。跨度里含着块内部的空档，
           一块 4 分钟的记录只要夹着 3 分钟没动鼠标就能冒充 7 分钟过关。
           而且 `hours` 报的就是 `seconds` —— 比 `seconds` 才和报出去的数字自洽。
        """
        return timedelta(seconds=self.seconds)


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
    # 用户在飞书里随手记下的一笔（domain=manual / metric=note）。
    # ⚠️ 这是**第一手事实** —— 比任何推断出来的都可靠，日记提示词里要单独说清。
    notes: list[dict[str, Any]] = field(default_factory=list)
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


def _is_unknown(cat: str, conf: str) -> bool:
    """这一段是不是「规则没认出来」。

    ⚠️ **`Other` 不是一种分类，是「没有信息」。**

    原来的合并判据要求「分类完全相同」。于是这种交替：

        Work → Other → Work → Other → Other → Work → ...

    会把一段连续活动切成上百个碎片，最后几乎全部短于 `MIN_BLOCK` 而被丢掉。
    实测 2026-10-13：窗口记录 2.34 小时，只剩 0.41 小时入账，**90% 的时间凭空消失**。

    浏览器最容易触发：标题里没有 `TITLE_HINTS` 关键词就返回 `Other`，
    而同一个站点相邻页面的标题经常一个命中一个不命中。
    实测那些碎片长这样（同一应用、同一站点、间隔几十秒）：

        18:47  1.0s  Work   msedge  '腾讯云 - 控制台 …'
        18:47 56.8s  Other  msedge  '腾讯云搜索 …'
        18:48 39.5s  Work   msedge  '总览 - 控制台 - 腾讯云 …'
        18:49 45.7s  Other  msedge  '腾讯云会员中心 …'

    把「不知道」当成「换了件事」，就等于让信息缺失变成了一次活动切换。
    """
    return cat == "Other" and conf in ("ambiguous", "default")


# 短于这个秒数的块算「焦点闪了一下」，不足以构成一次活动切换。
#
# 实测这台机器的窗口切片：1.0s 的切片大量存在（通知弹一下、误触、
# 或者切过去看了一眼就切回来），而真实活动段在 25–90s。
# 一条 1 秒的 explorer 焦点就足以把一段连续的 Work 拦腰截断 ——
# 它占的时间可以忽略，破坏力却最大。
BLIP_SECONDS = 15.0


def _should_merge(last: "Block", cat: str, conf: str, app: str,
                  dur: float, gap: timedelta) -> bool:
    """新切片该不该并进前一个块。"""
    if gap > MERGE_GAP:
        return False
    if last.category == cat:
        return True
    # 「不知道」不构成「换了件事」——同一应用且时间连续，就算延续当前活动。
    # 要求同一应用是为了兜住误判：认不出来的**别的**应用不该被吞进来。
    if app == last.app and (_is_unknown(cat, conf)
                            or _is_unknown(last.category, last.confidence)):
        return True
    # 焦点闪动：这一段或前一段短到不构成一次活动切换，并进去
    if dur < BLIP_SECONDS or last.seconds < BLIP_SECONDS:
        return True
    return False


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
            if _should_merge(last, cat, conf, s["app"], s["dur"], gap):
                # 之前不知道、现在认出来了 → 采用认出来的那个分类
                if (_is_unknown(last.category, last.confidence)
                        and not _is_unknown(cat, conf)):
                    last.category = cat
                    last.confidence = conf
                last.end = max(last.end, end)
                last.seconds += s["dur"]
                if s["title"] and s["title"] not in last.titles:
                    last.titles.append(s["title"])
                if not last.url and s.get("url"):
                    last.url = s["url"]
                # 只有「认出来了但判不准」才把整块降级成待复核。
                #
                # ⚠️ 不能连 ("Other","ambiguous") 一起降级 —— 那是「标题里没线索」，
                #    它根本没提供分类，却会把一整块已经由规则判定的活动标成「判不准」。
                #    实测：一段连续工作里只要混进一条认不出的页面标题，整块就推给 LLM，
                #    结果 22 个块**全部**是 ambiguous，规则引擎等于白跑，
                #    而且日记的分类又变回模型说了算 —— 这正是我们要修的问题。
                if (conf == "ambiguous" and not _is_unknown(cat, conf)
                        and s["dur"] >= BLIP_SECONDS):
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


def fold_short_blocks(blocks: list[Block],
                      min_block: timedelta = MIN_BLOCK) -> list[Block]:
    """把短于阈值的块并进相邻块，而不是让它们消失。

    ⚠️ 阈值该管的是「日记里塞多少细节」，**不是「删掉多少时间」**。

    原来这四个地方都是直接 `continue` 丢掉：

        if b.end - b.start < MIN_BLOCK:
            continue

    于是 2026-10-13 一天 2.34 小时的活动只有 0.41 小时入账，
    **82% 的时间无声消失**，而下游（日记、Grafana、以后的情绪相关性分析）
    看到的都是那个残缺数字，没有任何地方提示少了什么。
    这也是日记模型只能靠编造时长的直接原因 —— 喂给它的料本来就不够。

    并入**前一个**块：经过上面的模糊合并，前一个块通常就是这段活动的主体。
    只在前一个块离得太远时（间隔 > MERGE_GAP）才不动 —— 那种情况硬并进去
    会让 `hhmm` 显示成一段跨越空档的连续活动，那才是真的失真。
    """
    if not blocks:
        return blocks

    out: list[Block] = []
    for b in blocks:
        if b.seconds >= min_block.total_seconds() or not out:
            out.append(b)
            continue
        prev = out[-1]
        if b.start - prev.end <= MERGE_GAP:
            prev.end = max(prev.end, b.end)
            prev.seconds += b.seconds
            for t in b.titles:
                if t and t not in prev.titles:
                    prev.titles.append(t)
            if b.confidence == "ambiguous":
                prev.confidence = "ambiguous"
        else:
            out.append(b)
    return out


# ── 时间黑洞 ────────────────────────────────────────────────────────
#
# 「黑洞」= 事后回想不起来、也不觉得值的那段时间。
#
# ⚠️ 这个判定**必须可解释**，所以只用看得见的东西：应用名和窗口标题里的关键词。
#    不用「时长 > 2 小时」这种纯时间规则 —— 一次 3 小时的专注工作时长更长，
#    但它恰恰是黑洞的反面。
#
# ⚠️ 规则放在 config.yaml 的 `blackhole:` 节里，不写死在代码里：
#    什么算黑洞因人因时而异（刷短视频算，看行业新闻可能不算）。
#    写死等于替用户下结论，而被冤枉的时间会直接出现在面板上。
#
# ⚠️ **没有配置就一个都不标**，不是「全标」—— 面板空着比标错好。
#    这也让老配置文件（没有 blackhole 节）不会因为这个功能而行为突变。


def mark_blackholes(blocks: list[Block], rules: dict[str, Any]) -> int:
    """按配置里的关键词给块打黑洞标记，返回标记了几个。

    匹配的是**子串**（小写化后），不是全等：窗口标题千变万化
    （「抖音 - Google Chrome」「Bilibili - 哔哩哔哩」），全等永远匹配不上。

    ⚠️ 除了应用名和标题，还要看 **URL**。
       浏览器里的黑洞基本都发生在**站点**上，而窗口标题常常是
       「✳ 某文档 / New tab」这种看不出内容的串 —— 光看标题会漏掉
       整片该标的东西。`sites` 那一列就是给这个用的。

    `min_minutes` 用来放过太短的块：刷 30 秒手机不是「黑洞」，
    那连一次活动切换都算不上。
    """
    if not rules or not rules.get("enabled", False):
        return 0

    apps = [str(a).lower() for a in (rules.get("apps") or []) if str(a).strip()]
    titles = [str(t).lower() for t in (rules.get("titles") or []) if str(t).strip()]
    sites = [str(s).lower() for s in (rules.get("sites") or []) if str(s).strip()]
    if not apps and not titles and not sites:
        return 0

    try:
        min_seconds = float(rules.get("min_minutes", 6)) * 60.0
    except (TypeError, ValueError):
        min_seconds = 360.0

    marked = 0
    for b in blocks:
        if b.seconds < min_seconds:
            continue
        app = (b.app or "").lower()
        url = (b.url or "").lower()
        if any(k in app for k in apps):
            b.blackhole = True
            marked += 1
            continue
        if any(k in url for k in sites):
            b.blackhole = True
            marked += 1
            continue
        if any(k in (t or "").lower() for t in b.titles for k in titles):
            b.blackhole = True
            marked += 1
    return marked


# ── 手机应用名 ──────────────────────────────────────────────────────
#
# Tasker 上报的通常是**包名**（`%app_package` → com.tencent.mm），
# 但也可能是**应用名**（有人会改成别的变量），
# 取决于任务怎么配的。两种都认：看着像包名的查表，否则原样用。
#
# ⚠️ 不查表的话，日记里会写一堆 com.tencent.mm 这种包名，模型还得去猜那是什么
#    （猜错就写进日记了）。这张表是**可扩展**的：加一行就多认一个应用。
#
# 命名一律英文，和 config.categories 的「所有记录一律英文」保持一致，
# 也让英文日记读起来不混语种。想换成中文名就改右边那一列，一个地方改完两边都生效。
PHONE_APPS = {
    # 国内常用
    "com.tencent.mm": "WeChat",
    "com.tencent.mobileqq": "QQ",
    "com.tencent.wework": "WeCom",
    "com.tencent.mtt": "QQ Browser",
    "com.tencent.weread": "WeRead",
    "com.tencent.news": "Tencent News",
    "com.eg.android.AlipayGphone": "Alipay",
    "com.taobao.taobao": "Taobao",
    "com.jingdong.app.mall": "JD",
    "com.sina.weibo": "Weibo",
    "com.zhihu.android": "Zhihu",
    "com.ss.android.ugc.aweme": "Douyin",
    "tv.danmaku.bili": "Bilibili",
    "com.netease.cloudmusic": "NetEase Music",
    "com.netease.newsreader.activity": "NetEase News",
    "com.xingin.xhs": "Xiaohongshu",
    "com.alibaba.android.rimet": "DingTalk",
    "com.sankuai.meituan": "Meituan",
    "me.ele": "Ele.me",
    "com.dianping.v1": "Dianping",
    "com.autonavi.minimap": "Amap",
    "com.baidu.BaiduMap": "Baidu Maps",
    "com.hellobike.hey": "HelloBike",
    "com.UCMobile": "UC Browser",
    # 2026-10 实测这台手机上出现的（不是猜的）
    "com.app.shanghai.metro": "Shanghai Metro",
    "com.deepseek.chat": "DeepSeek",
    "com.taobao.idlefish": "Xianyu",
    "com.alibaba.aliyun": "Alibaba Cloud",
    "net.dinglisch.android.taskerm": "Tasker",
    "com.tailscale.ipn": "Tailscale",
    "org.fdroid.fdroid": "F-Droid",
    "app.hiddify.com": "Hiddify",
    "com.miui.securitycenter": "Security",
    "nodomain.freeyourgadget.gadgetbridge": "Gadgetbridge",
    # 2026-10-15 实测这台手机上出现的（同上，不是猜的）
    "md.obsidian": "Obsidian",
    "mark.via": "Via Browser",
    "com.quark.browser": "Quark Browser",
    "com.xunmeng.pinduoduo": "Pinduoduo",
    "com.kugou.android.lite": "Kugou Lite",
    "com.github.android": "GitHub",
    "au.com.shiftyjelly.pocketcasts": "Pocket Casts",
    # 没有通行英文名的，按拼音/通称写（后面注释是中文名）
    "com.chaoxing.mobile": "Chaoxing",                          # 学习通
    "com.max.xiaoheihe": "Xiaoheihe",                           # 小黑盒
    "com.bf.sgs.hdexp.mi": "Sanguosha",                         # 三国杀（边锋）
    "com.larus.nova": "Doubao",                                 # 豆包
    "com.greenpoint.android.mc10086.activity": "China Mobile",  # 中国移动
    # 小米 / 安卓自带
    "com.xiaomi.market": "Mi Store",
    "com.xiaomi.shop": "Mi Shop",
    "com.miui.mediaeditor": "Mi Gallery Editor",
    "com.mfashiongallery.emag": "Mi Wallpaper Carousel",
    "com.xiaomi.gamecenter.sdk.service": "Mi Game Center",
    "com.xiaomi.bluetooth": "Bluetooth",
    "com.miui.securitycore": "Security Core",
    "com.android.deskclock": "Clock",
    "com.mendhak.gpslogger": "GPSLogger",
    # 通用 / 国际
    "com.android.chrome": "Chrome",
    "com.microsoft.emmx": "Edge",
    "org.mozilla.firefox": "Firefox",
    "com.google.android.youtube": "YouTube",
    "org.telegram.messenger": "Telegram",
    "com.whatsapp": "WhatsApp",
    "com.discord": "Discord",
    "com.google.android.gms": "Google Play services",
    # 系统
    "com.android.settings": "Settings",
    "com.android.camera": "Camera",
    "com.android.gallery3d": "Gallery",
    "com.miui.gallery": "Gallery",
    "com.android.vending": "Play Store",
    "com.android.dialer": "Phone",
    "com.android.mms": "Messages",
    "com.android.systemui": "System UI",
}

# 看着像 Android 包名吗？至少两段、全小写字母数字下划线、点分隔。
_PKG_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

# 这些包名代表「**没有**应用在前台」，不是「在用某个应用」。
#
# 最典型的是桌面：从微信退回桌面、再从桌面点开浏览器，中间那一跳的
# value_text 就是桌面。它占了真实的时间，但那次时间和「用了什么」无关。
# 不过滤的话「用得最多的应用」里桌面会永远名列前茅 —— 那个排名就没用了。
#
# ⚠️ 只影响**统计**（top_apps），不影响时长计算：一段前台的时长是记在
#    「前一个应用」头上的，所以跳过桌面不会让谁的时间凭空变少。
PHONE_NOT_AN_APP = {
    "com.miui.home", "com.android.launcher", "com.android.launcher3",
    "com.google.android.apps.nexuslauncher", "com.huawei.android.launcher",
    "com.hihonor.android.launcher", "com.oppo.launcher", "com.bbk.launcher2",
    "com.sec.android.app.launcher", "com.android.systemui",
    # 系统弹窗：装 App、授权、配对时蹦出来的界面。
    # 和桌面同理 —— 它是「过程」不是「在用的应用」，进排名只会挤掉真东西。
    # （实测装 Tasker 那一阵，这几个能刷出一大批 0 分钟的记录）
    "com.android.permissioncontroller", "com.miui.packageinstaller",
    "com.android.companiondevicemanager", "com.android.packageinstaller",
}

# 两条相邻 App 切换之间的间隔上限（兜底，不是主要判据）。
#
# 主要判据是**解锁事件**：解锁意味着屏幕之前是灭的，那段时间不属于任何应用，
# 所以遇到解锁就把这段前台截到解锁时刻（见 summarize_phone）。
# 有了它，正常的「在一个应用里读十分钟没切」不会被误杀。
#
# 这个上限只防最后一种情况：没有解锁、也没有切换，却隔了很久 ——
# 那是 Tasker 被系统杀了一段时间，不该把整段都算成那个应用的前台。
MAX_FOREGROUND_GAP = timedelta(minutes=60)


def phone_app_name(raw: str) -> str:
    """包名 → 人话；已经是人话的原样返回。"""
    raw = (raw or "").strip()
    if not raw:
        return "unknown"
    if raw in PHONE_APPS:
        return PHONE_APPS[raw]
    return raw


# 两条同种的屏幕事件挨得比这个近，就当成重复上报（Tasker 会连发）。
SCREEN_DEBOUNCE = timedelta(seconds=5)


def _screen_intervals(
    events: list[tuple[datetime, str]],
) -> list[tuple[datetime, datetime]]:
    """把 screen_on / screen_off 配成「屏幕亮着」的区间。

    ⚠️ 配不上的**直接丢掉，不猜**：

      · 只有 on 没有 off（导出那一刻屏幕还亮着）→ 结束时间未知
      · 连续两个 on（漏了一次 off）→ 前一段的终点未知，丢掉它

    宁可少算，也不要凭空补一段时间。跟这个项目其它地方的取舍一致。
    """
    out: list[tuple[datetime, datetime]] = []
    open_at: datetime | None = None
    for ts, kind in sorted(events, key=lambda x: x[0]):
        if kind == "screen_on":
            # ⚠️ 去抖。实测 Tasker 会**连发同一种事件**（见过 3 条 screen_off
            #    相隔 3~4 秒）。off 连发无害 —— 后面几条没有对应的 on，自然被忽略。
            #    但 on 连发是**有害的**：第二条会把前一段还没闭合的窗口作废，
            #    等于直接从「屏幕时间」里挖掉一段时间。
            #    所以紧挨着的重复 on 只认第一条。
            if open_at is not None and (ts - open_at) <= SCREEN_DEBOUNCE:
                continue
            open_at = ts
        elif kind == "screen_off" and open_at is not None:
            if ts > open_at:
                out.append((open_at, ts))
            open_at = None
    return out


def _unlock_seconds(start: datetime, end: datetime, unlocks: list[datetime]) -> float:
    """按解锁事件裁：解锁意味着屏幕之前是灭的，那段时间不属于任何应用。"""
    if end <= start:
        return 0.0
    for u in unlocks:
        if start < u <= end:
            return (u - start).total_seconds()
    return (end - start).total_seconds()


def _active_seconds(
    start: datetime, end: datetime,
    unlocks: list[datetime], screen_iv: list[tuple[datetime, datetime]],
    covered: tuple[datetime, datetime] | None = None,
) -> float:
    """``[start, end]`` 里**真正在用手机**的秒数。

    ⚠️ **屏幕事件只对它自己覆盖的那一段负责。**

       有屏幕数据的时段 → 按屏幕区间裁（准确：屏幕亮着才可能是在用）
       没有屏幕数据的时段 → 退回按解锁事件裁（老数据、手机还没加 Display 事件、
                            或者中途才加上的那一段时间）

       **不能因为「屏幕区间里没有这段」就把它归零。**

       实测踩到过：下午 19:40 才加上 Display 两个 Profile，而应用切换从
       17:05 就有了 —— 于是 17:05~19:40 之间那 200 多次切换**全部被裁成 0**，
       「用得最多的应用」整个榜单变成一串 0 分钟，看起来像功能没生效。
    """
    if not screen_iv or covered is None:
        return _unlock_seconds(start, end, unlocks)

    cov_lo, cov_hi = covered
    total = 0.0
    # 屏幕数据开始之前的那一截
    if start < cov_lo:
        total += _unlock_seconds(start, min(end, cov_lo), unlocks)
    # 屏幕数据结束之后的那一截
    if end > cov_hi:
        total += _unlock_seconds(max(start, cov_hi), end, unlocks)
    # 中段：按屏幕区间裁
    s, e = max(start, cov_lo), min(end, cov_hi)
    if e > s:
        for a, b in screen_iv:
            lo, hi = max(s, a), min(e, b)
            if hi > lo:
                total += (hi - lo).total_seconds()
    return total


def _end_of_screen_window(
    ts: datetime, screen_iv: list[tuple[datetime, datetime]],
) -> datetime | None:
    """``ts`` 所在的那个「屏幕亮着」区间在哪结束。没有屏幕事件就返回 None。"""
    for a, b in screen_iv:
        if a <= ts < b:
            return b
    return None


def summarize_phone(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把手机的原始事件汇总成日记和 02-Phone 用得上的形状。

    输入是 `/ingest/phone` 收下的两类事件：

      · ``metric="unlock"``      屏幕解锁
      · ``metric="app_switch"``  ``value_text`` = 应用（包名或应用名）

    ⚠️ **「使用最久的 App」是推导出来的，不是上报的。**
       Tasker 的 App Changed 事件只带一个时间戳，没有时长。但两条相邻切换
       之间的时间差，就是**前一个应用在前台待了多久**。所以
       ``(ts, app)`` 序列一阶差分就得到了每个应用的前台时长。

    ⚠️ 光有一阶差分还不够，还要**裁掉屏幕灭着的时间**：
       「切到浏览器 → 锁屏放口袋 → 两小时后解锁切到微信」，
       不裁的话那两小时全算成浏览器。

       裁剪优先用 **screen_on / screen_off**（准确：屏幕亮着才可能是在用）；
       没有屏幕事件时退回用**解锁事件**（屏幕之前是灭的），
       这样老数据、或者还没加 Display 事件的时候也照样能用。

    ⚠️ **最后一个应用**原来没有下一跳、时长无从得知，只能不记。
       有了 screen_off 就有终点了 —— 这是加 Display 事件最实在的收益，
       否则「睡前刷了半小时」这种整段都进不了账。
    """
    unlocks: list[datetime] = []
    switches: list[tuple[datetime, str]] = []
    screen_events: list[tuple[datetime, str]] = []

    for e in events:
        ts = _parse(e.get("ts_utc"))
        if ts is None:
            continue
        met = e.get("metric")
        if met == "unlock":
            unlocks.append(ts)
        elif met == "app_switch":
            # ⚠️ 存**原始值**（包名或应用名），到统计那一步再翻译。
            #    因为要先用原始包名判断「这是不是桌面」，翻译了就认不出来了。
            switches.append((ts, (e.get("value_text") or "").strip()))
        elif met in ("screen_on", "screen_off"):
            screen_events.append((ts, met))

    unlocks.sort()
    switches.sort(key=lambda x: x[0])
    screen_iv = _screen_intervals(screen_events)
    screen_seconds = sum((b - a).total_seconds() for a, b in screen_iv)
    # 屏幕事件**覆盖**的时间范围。只有落在这一段里的时长才用屏幕区间去裁；
    # 之外的一律退回按解锁事件裁 —— 否则「中途才加上 Display Profile」的那天，
    # 之前的活动会被整段裁成 0。
    covered: tuple[datetime, datetime] | None = (
        (min(t for t, _ in screen_events), max(t for t, _ in screen_events))
        if screen_events else None
    )

    fore: dict[str, float] = {}
    unknown: dict[str, float] = {}

    def attribute(raw_app: str, span: float) -> None:
        """把一段时长记到某个应用头上（含「这是不是应用」的各种过滤）。"""
        if not (0 <= span <= MAX_FOREGROUND_GAP.total_seconds()):
            return
        if raw_app in PHONE_NOT_AN_APP:
            return                        # 桌面 / 系统界面 —— 不是在用的应用
        if raw_app.startswith("%"):
            # 没被替换掉的 Tasker 变量（比如把变量名写错成 %APPNAME）。
            # ⚠️ Tasker 不会报错，会把变量名**原样**发出来。它不是应用名，
            #    别让它进排名；但也不能假装没看见 —— 记进 unknown。
            unknown[raw_app] = unknown.get(raw_app, 0.0) + span
            return
        if raw_app and _PKG_RE.match(raw_app) and raw_app not in PHONE_APPS:
            # 看着像包名却不在表里：时长照算（不能凭空丢），但另外记一笔，
            # 好在报告里告诉人去补映射表 —— 否则日记里就是一堆 com.xxx.yyy
            unknown[raw_app] = unknown.get(raw_app, 0.0) + span
        name = phone_app_name(raw_app)
        fore[name] = fore.get(name, 0.0) + span

    for (t0, raw_app), (t1, _) in zip(switches, switches[1:]):
        attribute(raw_app, _active_seconds(t0, t1, unlocks, screen_iv, covered))

    # 最后一个应用。原来它没有下一跳、时长无从得知，只能不记；
    # 有了 screen_off 就有终点了 —— 这是加 Display 事件最实在的一处收益。
    if switches:
        t_last, raw_last = switches[-1]
        end = _end_of_screen_window(t_last, screen_iv)
        if end is not None:
            attribute(raw_last, (end - t_last).total_seconds())

    top = sorted(fore.items(), key=lambda x: -x[1])[:10]

    return {
        "unlocks": len(unlocks),
        "app_switches": len(switches),
        "first_unlock": unlocks[0].astimezone(TZ).strftime("%H:%M") if unlocks else "",
        "last_unlock": unlocks[-1].astimezone(TZ).strftime("%H:%M") if unlocks else "",
        "top_apps": [[name, round(secs / 60.0, 1)] for name, secs in top],
        # **屏幕使用时间**（亮屏总时长）。这是拿 screen_on / screen_off
        # 直接测出来的，不是推的 —— 手机加了 Display 事件之后才有。
        "screen_minutes": round(screen_seconds / 60.0, 1),
        # 各应用前台的合计。⚠️ 它是**下界**：漏掉的切换、配不成对的
        # screen_on/off 都不计入，所以它 ≤ screen_minutes，别当成
        # 「手机总使用时长」往外报。
        "foreground_minutes": round(sum(fore.values()) / 60.0, 1),
        # 不在 PHONE_APPS 里的包名 → 秒数。给人补表用。
        "unknown_apps": [[pkg, round(secs / 60.0, 1)]
                         for pkg, secs in sorted(unknown.items(), key=lambda x: -x[1])],
    }


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

    phone_events: list[dict[str, Any]] = []

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
            # 同一指标取最后一次（当天汇总值）。
            #
            # ⚠️ 这里**不再设白名单**。原来只认 sleep_hours / steps / *_avg，
            #    结果把 heart_rate_min 这种漏掉了 —— 而它是有用的
            #    （静息心率的近似值）。
            #
            #    能走到这里的事件**已经**是日汇总：ingest_gadgetbridge 在写库前
            #    就把分钟级采样压成了一个值。没有过滤的必要，而且将来换设备、
            #    多出什么指标（Garmin 的 body battery、训练负荷…）会自动流进来，
            #    不用回来改这里。
            tl.health[met] = e.get("value_num")
        elif dom == "phone":
            phone_events.append(e)
        elif dom == "web" and met == "site_usage":
            tl.web.append({
                "site": e.get("value_text") or "",
                "minutes": round((e.get("value_num") or 0) / 60.0, 1),
            })
        elif dom == "manual":
            # 用户在飞书里随手记的一笔。**原样**带下去，不做任何改写 ——
            # 这是全系统里唯一一条「用户直接说的话」，价值最高。
            text = str(e.get("value_text") or "").strip()
            if text:
                tl.notes.append({
                    "ts": e.get("ts_utc"),
                    "metric": met,
                    "text": text,
                })

    tl.location.sort(key=lambda x: x["minutes"], reverse=True)
    tl.web.sort(key=lambda x: x["minutes"], reverse=True)
    tl.notes.sort(key=lambda x: str(x.get("ts") or ""))

    # 手机事件要整体看才能推前台时长（见 summarize_phone），所以收集完再算，
    # 不在循环里逐条计数 —— 逐条只能得到「切换了多少次」，得不到「用在哪」。
    if phone_events:
        tl.phone = summarize_phone(phone_events)


# ── 输出 ────────────────────────────────────────────────────────────


def render_markdown(tl: Timeline) -> str:
    lines = [f"# Timeline {tl.day}", ""]

    # 飞书里随手记的那几句，放最前面 —— 它们是用户直接说的话，
    # 比下面任何推导出来的统计都更值得先看到。
    if tl.notes:
        lines.append("## Notes（飞书里记的）")
        for n in tl.notes:
            lines.append(f"- {n.get('text', '')}")
        lines.append("")

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
        p = tl.phone
        lines.append("## Phone")
        lines.append("")
        head = (f"- {p.get('unlocks', 0)} unlocks"
                + (f" (first {p['first_unlock']}, last {p['last_unlock']})"
                   if p.get("first_unlock") else "")
                + f", {p.get('app_switches', 0)} app switches")
        lines.append(head)
        if p.get("screen_minutes"):
            # 这个是**实测**的（screen_on → screen_off），不是推的 —— 说清楚，
            # 免得模型把下面那个推导值当成屏幕时间
            lines.append(f"- Screen-on time: {p['screen_minutes']:.0f} min"
                         " (measured from screen_on/screen_off events)")
        if p.get("foreground_minutes"):
            # 说明这是**下界**，否则模型会把「按切换推导的前台时长」
            # 当成「手机总使用时长」写进日记
            lines.append(f"- App foreground time on the phone: {p['foreground_minutes']:.0f} min"
                         " (a lower bound — missed app switches and unpaired screen"
                         " events are not counted, so this is below the screen-on time)")
        top = p.get("top_apps") or []
        if top:
            lines.append("- Most used phone apps, by inferred foreground time:")
            for name, mins in top:
                lines.append(f"  - {name}: {mins:.0f} min")
        unk = p.get("unknown_apps") or []
        if unk:
            lines.append("")
            lines.append("## Phone app values that couldn't be resolved")
            lines.append("")
            for pkg, mins in unk[:20]:
                lines.append(f"- `{pkg}`: {mins:.0f} min")
            lines.append("")
            lines.append("  以 `%` 开头的 = Tasker 变量没被替换（变量名写错），"
                         "要去改 Profile 的 Body。")
            lines.append("  以字母开头的 = 包名不在 `normalize.PHONE_APPS` 里，"
                         "加一行 `\"com.example.app\": \"Readable Name\",` 即可。")
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
        # 这里**不再**按 MIN_BLOCK 过滤：短块已经由 fold_short_blocks 并进相邻块了。
        # 原来的 `continue` 会让真实发生的时间凭空消失，见该函数的说明。
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
        # 这里**不再**按 MIN_BLOCK 过滤：短块已经由 fold_short_blocks 并进相邻块了。
        # 原来的 `continue` 会让真实发生的时间凭空消失，见该函数的说明。
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
    raw_n = len(blocks)
    blocks = fold_short_blocks(blocks)
    print(f"  ✓ Merged into {raw_n} time blocks → {len(blocks)} after folding the short ones in")

    # 黑洞判定放在**折叠之后**：并进相邻块的那几分钟不该单独判一次，
    # 要判就判合并后的整块（那才是日记和面板里显示的那一行）。
    n_bh = mark_blackholes(blocks, CFG.get("blackhole") or {})
    if CFG.get("blackhole", {}).get("enabled"):
        if n_bh:
            print(f"  ✓ Marked {n_bh} block(s) as time sinks "
                  f"({sum(b.hours for b in blocks if b.blackhole):.2f}h)")
        else:
            print("  . No block matched the blackhole rules today")

    tl = Timeline(day=day.iso, blocks=blocks, unmatched_apps=unmatched)

    print("-> Attaching location / health / phone data")
    attach_other_domains(tl, day)

    # 统计（只算够长的块）
    by_cat: dict[str, float] = {}
    for b in blocks:
        # 这里**不再**按 MIN_BLOCK 过滤：短块已经由 fold_short_blocks 并进相邻块了。
        # 原来的 `continue` 会让真实发生的时间凭空消失，见该函数的说明。
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
