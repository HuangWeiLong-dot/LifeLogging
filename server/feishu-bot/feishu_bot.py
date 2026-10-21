#!/usr/bin/env python3
"""飞书机器人 —— 每日提问的收发通道。跑在常驻的 Linux 服务器上。

**为什么是飞书，不是 QQ**

    原本要做 QQ。查过之后否掉，理由是一手来源：

      · QQ 官方机器人：官方文档原文「主动推送能力于 2025 年 4 月 21 日起不再
        提供，接口调用时会收到错误信息。」单聊只剩被动回复 —— 用户先发消息，
        60 分钟内最多回 4 条。**推不动**。
      · 互动召回（is_wakeup）是唯一的替代，但额度是「当天 / 1-3 天 / 3-7 天 /
        7-30 天」各 1 条，**合计 4 条/30 天**，约等于每周 1 条。做每日提问差一个量级。
      · NapCat/OneBot 那类个人号协议端能推，但社区实测普遍几小时掉一次线，
        且要用养过的小号。这个项目一路在跟「会在半夜静默失效的东西」打架，
        不该主动再引入一个。

    飞书自建应用：主动推送不限量，长连接由**服务端出站**发起 —— 不需要公网
    入口、不需要域名备案，Cloudflare Access 一行都不用动。个人版免费。

**它做什么**

    它**不重写任何规则**，只是同一根管子上的另一扇门：

      收消息  ──> 拉中继 GET /questions/pending ──> 把下一题发给你
      再收消息 ──> 规范化答案 ──> POST 中继 /answer ──> 确认，再问下一题

    答案仍然是 `domain=question / metric=answer / "<题号>|<答案>|<备注>"` 落进
    `phone-<day>.jsonl`，笔记本 23:00 照常拉走、apply_answers.py 照常勾 Tasks.md。
    **中继是「什么算还没答」的唯一实现**，这里不抄第二份 —— 抄了就会漂移，
    而且漂移了两边都不报错。

    另外它还能答「我今天去哪了」这类**当天数据**的问题（同一根管子上的另一
    个用途）。那一半在 live.py 里 —— 同样是把服务器上已有的实现调过来用，
    不重写任何统计逻辑。

**⚠️ 三件必须知道的事**

    1. **答案必须规范化成 yes/no 或选项原文，不能把中文原样发过去。**
       中继的 answers_to_events() 只对字面量 yes/no 转小写，其余原样存；
       而 apply_answers.answered_done() 比的是 `== "yes"`、选择题比的是
       **选项原文逐字相等**。把「做了」直接转手发出去，答案会被愉快地存下来、
       任务**永远不被勾掉，而且两边都不报错**。

    2. **不要改用交互卡片按钮。** lark-oapi 的 WS 客户端 _handle_data_frame 只
       分发 MessageType.EVENT，MessageType.CARD 那一支是裸 return —— 卡片回调
       在长连接下**被静默丢弃**：按钮显示得出来、点了没反应、服务端一个错都不报。
       （已对着 SDK 源码确认。）所以答案交互用编号文字。

    3. **事件处理必须 3 秒内返回**，否则飞书会**超时重推**，于是同一条消息被
       处理两次。所以下面 on_message 只做「提取字段 + 入队」就返回，真正的
       HTTP 调用全在 worker 线程里；并且按 message_id 去重。

用法：
  python feishu_bot.py                  # 常驻（systemd 用这个）
  python feishu_bot.py --check          # 只自检配置和中继连通性，不连飞书
  python feishu_bot.py --push-test 文本  # 发一条主动消息就退出（验证推送链路）

部署见同目录 README.md。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ⚠️ **必须改成行缓冲。**
#
# systemd 捕获的 stdout 是**管道**，而 Python 对管道默认用**块缓冲**
# （4/8KB）。于是 print() 出来的行会卡在缓冲区里，journal 上半天看不到 ——
# 更糟的是它们的**时间戳会落在真正发生的那一刻之前**，因为那一行是后来
# 才被一起写出去的。
#
# 这会让「我发了消息、机器人没反应」查起来完全跑偏：消息其实收到了，
# 只是那行日志还没被冲出来。实测踩到过。
#
# （logging 模块走的是 stderr，不受影响 —— 所以 `[Lark] connected to wss://`
#   一直都能看到，而自己 print 的行看不到，这个差别正是线索。）
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):  # 极老的 Python 或已被替换的流
    pass

# 让 `import live` 在「直接跑脚本」和「被 importlib 加载」两种情况下都成立。
sys.path.insert(0, str(Path(__file__).resolve().parent))
# `live_questions`（收尾题的生成器）在 scripts/ 下。它是**延迟 import** 的
# （见 handle_end_day）—— 那个模块在 import 时会 load_config()，不想让它
# 影响机器人启动。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
import live  # noqa: E402

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
OWNER_OPEN_ID = os.environ.get("FEISHU_OWNER_OPEN_ID", "")

RELAY_BASE = os.environ.get("FEISHU_RELAY_BASE", "").rstrip("/")
RELAY_API_KEY = os.environ.get("RELAY_API_KEY", "")

# 中继的 unit 文件。用它问出中继到底绑在哪个地址 —— 见 relay_base_candidates()。
RELAY_UNIT_FILE = Path(
    os.environ.get("FEISHU_RELAY_UNIT",
                   "/etc/systemd/system/lifelogging-relay.service"))
RELAY_PORT_DEFAULT = 8788

STATE_DIR = Path(os.environ.get("FEISHU_STATE_DIR", "/var/lib/lifelogging"))
STATE_PATH = STATE_DIR / "feishu-state.json"

# 配置有问题时的退出码。unit 里用 `RestartPreventExitStatus=2` 认它。
#
# ⚠️ **必须有这个区分。** unit 是 `Restart=always`，而「配置错了」重启一百次
#    还是错的 —— 实测：env 里留着中文占位符时，日志被同一句话每 5 秒刷一遍，
#    一直刷到你把值改对（restart counter 涨到了 24）。
#    光靠 systemd 自己的 StartLimitBurst **拦不住**：默认是 5 次/10 秒，而
#    RestartSec=5 意味着 10 秒里只重启 2 次，永远够不到那个上限。
#    分开退出码之后：配置错 → 停下来等人；运行时崩（长连接断了等）→ 照常重连。
EXIT_CONFIG = 2

# 每天几点主动推一次（本地时间）。留空 = 不主动推，只被动应答。
PUSH_AT = os.environ.get("FEISHU_PUSH_AT", "08:30")

# 模板答不了的问题，要不要花钱交给 LLM。设成 0 就纯模板（零成本）。
FREEFORM_LLM = os.environ.get("FEISHU_FREEFORM_LLM", "1").lower() not in (
    "", "0", "false", "no", "off")

# 服务器每 30 分钟生成的**实时题**，要不要尽快单独推给你。
# 关掉的话就只剩每天 08:30 那次整批推送 —— 那样「实时提问」名不副实。
LIVE_PUSH = os.environ.get("FEISHU_LIVE_PUSH", "1").lower() not in (
    "", "0", "false", "no", "off")
# 多久去中继看一眼有没有新的实时题。生成器是 30 分钟一次，这里比它密，
# 所以题生成后最多几分钟就到手机上。
LIVE_POLL_MIN = max(1, int(os.environ.get("FEISHU_LIVE_POLL_MIN", "5") or 5))

# 「别打扰」时段。**推送这一层也需要**，不能只靠生成器那边拦。
#
# ⚠️ 生成器（scripts/live_questions.py）有一个 8–22 的窗口，但那只管「生不生成」。
#    题生成完还躺在中继上，等着这个循环来推 —— 而推送原来是**不看时间的**：
#      · 21:58 生成的题，5 分钟轮询正好落在 22:03 → 你已经躺下了还被叫醒
#      · 服务器半夜重启 → 待推的那题立刻发出去
#    两种都不会报错，只会在不该响的时候响一声。
#
# ⚠️ 这两个值要和 live_questions.py 的 ASK_FROM_HOUR / ASK_UNTIL_HOUR 一起改，
#    否则会出现「生成了但永远推不出去」。
QUIET_FROM = int(os.environ.get("FEISHU_QUIET_FROM", "22"))
QUIET_UNTIL = int(os.environ.get("FEISHU_QUIET_UNTIL", "8"))


def in_quiet_hours(now: datetime) -> bool:
    """现在是不是「别打扰」时段。支持跨午夜（默认 22 → 8）。"""
    if QUIET_FROM == QUIET_UNTIL:
        return False
    if QUIET_FROM < QUIET_UNTIL:
        return QUIET_FROM <= now.hour < QUIET_UNTIL
    return now.hour >= QUIET_FROM or now.hour < QUIET_UNTIL
TZ_NAME = os.environ.get("FEISHU_TZ", "Asia/Shanghai")

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo(TZ_NAME)
except Exception:  # noqa: BLE001
    print(f"WARN: timezone {TZ_NAME!r} unavailable, falling back to UTC", file=sys.stderr)
    TZ = timezone.utc

RECENT_MAX = 50          # 记住多少条 message_id，用来挡超时重推


# ── 配置自检 ────────────────────────────────────────────────────────
def looks_like_placeholder(v: str) -> bool:
    """这个值看着像「还没填」吗？

    ⚠️ **非 ASCII 一律算没填**，这条是重点。文档里的占位符是纯英文的
       （`cli_REPLACE_ME`），但真正会发生的是：**整个占位符被连着中文一起
       粘进去**（`cli_你的真实appid`）—— 值是「有」的，非空、也不含
       REPLACE，看着完全正常。然后它会一路跑到 SDK 里，报出来的是一个
       内部的编码错误，跟「你忘了填」这个事实看不出任何关系。

       仓库里为同一件事已经吃过一次亏，见 `common.require_api_key()` 的注释
       （那次是 DeepSeek 的 key 被写成了 `sk-你的key`）。这里把那条教训
       直接做成检查。

    ⚠️ 也别只查一个前缀：我以前写的是 `startswith("cli-REPLACE")`（连字符），
       而文档里的占位符是 `cli_REPLACE_ME`（下划线）—— 连自己写的占位符都
       拦不住，等于没写。所以这里查 `REPLACE` 这个**子串**，不分大小写。
    """
    if not v or not v.strip():
        return True
    if "REPLACE" in v.upper():
        return True
    return any(ord(ch) > 127 for ch in v)


def check_config() -> list[str]:
    """返回一串「人类可读的问题」。空列表 = 没问题。"""
    problems: list[str] = []
    if looks_like_placeholder(APP_ID):
        problems.append(
            "FEISHU_APP_ID 没填、还是占位符，或者里面混了非 ASCII 字符\n"
            "     （中文占位符会被原样当凭据用，报出来的是 SDK 内部的编码错，看不出真因）"
        )
    if looks_like_placeholder(APP_SECRET):
        problems.append("FEISHU_APP_SECRET 没填、还是占位符，或混了非 ASCII 字符")
    if looks_like_placeholder(RELAY_API_KEY):
        problems.append(
            "RELAY_API_KEY 没填（或还是占位符）。它在中继的 "
            "/etc/lifelogging/relay.env 里，unit 用 EnvironmentFile 把那份也读进来了，"
            "确认那行还在。"
        )
    return problems


def relay_base_candidates() -> list[str]:
    """中继可能在哪。按可信度排序。

    ⚠️ **为什么不能写死一个默认值。**

        中继在服务器上绑的是 **Tailscale 地址**（`phone-relay/README.md` 第 4 步
        要求的，为了让手机连得上），**不是 127.0.0.1**。那个地址是机器相关的，
        写不进要进 git 的 unit 文件。

        而猜错的代价很大，而且**完全静默**：机器人连飞书一切正常、日志一片祥和，
        只是每一次中继调用都失败 —— 表现是「收得到消息，答案从来不落库」。
        实测踩到过：unit 里那行默认值就是 127.0.0.1，而中继绑在 100.127.41.49。

        所以这里**去问中继自己的 unit**：它和本服务是并排部署的同一个中继，
        unit 里那行 `Environment=RELAY_HOST=` 就是权威答案。
        （只读文件，不用起子进程；ProtectSystem=strict 让 /etc 只读，但读得到。）
    """
    out: list[str] = []
    if RELAY_BASE:
        out.append(RELAY_BASE)          # 显式配置的最高优先

    try:
        txt = RELAY_UNIT_FILE.read_text(encoding="utf-8")
    except OSError:
        txt = ""
    host = port = ""
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("Environment=RELAY_HOST="):
            host = line.split("=", 2)[2].strip()
        elif line.startswith("Environment=RELAY_PORT="):
            port = line.split("=", 2)[2].strip()
    if host and port:
        out.append(f"http://{host}:{port}")

    out.append(f"http://127.0.0.1:{RELAY_PORT_DEFAULT}")
    # 去重但保序
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


def pick_relay_base() -> str:
    """挑第一个**真的应答**的候选。一个都不应答就返回第一个候选（照旧去撞）。"""
    cands = relay_base_candidates()
    for cand in cands:
        try:
            if httpx.get(f"{cand}/health", timeout=3.0).status_code == 200:
                return cand
        except Exception:  # noqa: BLE001
            continue
    return cands[0] if cands else f"http://127.0.0.1:{RELAY_PORT_DEFAULT}"


def check_relay() -> list[str]:
    """中继连通性 + **新版路由在不在**。

    ⚠️ 只看 /health 是不够的：新旧版本的中继 /health 长得一模一样。要挑一个
       只有新版才有的路由去打，否则会得到「中继是好的」这个假结论，而机器人
       每次拉题目都 404。
    """
    problems: list[str] = []
    cands = relay_base_candidates()
    print(f"  候选地址：{', '.join(cands)}")

    good = None
    for cand in cands:
        try:
            r = httpx.get(f"{cand}/health", timeout=5.0)
            print(f"    {cand}/health -> {r.status_code}")
            if r.status_code == 200:
                good = cand
                break
        except Exception as e:  # noqa: BLE001
            print(f"    {cand}/health -> 连不上（{type(e).__name__}）")

    if good is None:
        problems.append(
            "上面这些地址一个都连不上中继。\n"
            "    先确认它活着：  systemctl status lifelogging-relay\n"
            "    再看它绑在哪：  sudo systemctl show lifelogging-relay -p Environment"
            " | tr ' ' '\\n' | grep RELAY_HOST\n"
            "    然后显式覆盖：  在 feishu-bot 的 unit 里设\n"
            "        Environment=FEISHU_RELAY_BASE=http://<那个地址>:<端口>"
        )
        return problems

    print(f"  → 用 {good}")
    try:
        r = httpx.get(f"{good}/questions/pending",
                      headers={"X-API-Key": RELAY_API_KEY}, timeout=5.0)
        if r.status_code == 404:
            problems.append(
                "中继上没有 /questions/pending —— 跑的还是旧版代码。\n"
                "    在服务器上 `sudo /opt/lifelogging/repo/server/deploy.sh` 更新它；\n"
                "    ⚠️ 你要是已经手工 git pull 过，deploy.sh 会认为「没有变化」而"
                "**跳过重启** —— 直接 `sudo systemctl restart lifelogging-relay`。"
            )
        elif r.status_code == 401:
            problems.append("中继返回 401 —— RELAY_API_KEY 不对。")
        else:
            print(f"    /questions/pending -> {r.status_code} {r.text[:160]}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"打 /questions/pending 出错：{e}")
    return problems


# ── 本地状态 ────────────────────────────────────────────────────────
_state_lock = threading.Lock()


def load_state() -> dict:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_state(state: dict) -> None:
    """原子写：先写 .tmp 再 replace，和中继的 _write_json 同一个做法。

    ⚠️ 写不下去**不是致命的**，但会很难看：重启后 awaiting 丢了，机器人会把
       已经问过的那题重问一遍。所以这里失败要出声，不能默默吞掉。
       （unit 的 ProtectSystem=strict 必须配 ReadWritePaths，否则整个目录只读，
        这就是它写不下去的最常见原因。）

    ⚠️ `_state_lock` 只护住**这一次写**，不护「读—改—写」整段。有两个写者
       （worker 线程和推送循环），所以**任何「先 load 再改再 save」的地方，
       都要在 save 前重新 load 一次**，否则会拿旧快照盖掉别人刚写的东西。
       现在只有 push_loop 那一处需要这么小心，改这里的时候记得一起看。
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    data = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with _state_lock:
            with tmp.open("wb") as f:
                f.write(data)
            os.replace(tmp, STATE_PATH)
    except OSError as e:
        print(f"WARN: 状态写不进 {STATE_PATH}：{e}", file=sys.stderr)


# ── 中继 ────────────────────────────────────────────────────────────
def relay_pending() -> list[dict]:
    """当天还没答的题。中继是这件事的唯一实现。"""
    r = httpx.get(f"{RELAY_BASE}/questions/pending",
                  headers={"X-API-Key": RELAY_API_KEY}, timeout=10.0)
    r.raise_for_status()
    return [q for q in (r.json().get("questions") or []) if isinstance(q, dict)]


def relay_answer(qid: str, value: str, note: str = "") -> dict:
    """把答案交给中继。编码（`<题号>|<答案>|<备注>`）由中继拼，这里不碰。

    `note` 是用户自己写的那半句 —— 以前永远传空串，等于把「想说的话」丢了。
    """
    r = httpx.post(f"{RELAY_BASE}/answer",
                   headers={"X-API-Key": RELAY_API_KEY},
                   json={"answers": [{"id": qid, "value": value,
                                      "note": note[:500]}]},
                   timeout=10.0)
    r.raise_for_status()
    return r.json()


def relay_note(text: str) -> dict:
    """把一句话存成**笔记**，走和中继上其他手机事件同一条管道。

    ⚠️ 用 `/ingest/phone` 而不是新开一个端点：那条路已经是「收下就追加到
       当天 JSONL」的，而且笔记本那边的 collect_phone_events.py 会原样透传
       任意 domain —— 于是 manual/note 会自动进事件表、进日记提示词。
       新开端点就得再改一遍笔记本侧，多一处会漂移的约定。

    `ts` 用 Unix 秒（和 Tasker 一样），中继会统一转成 ISO。
    """
    r = httpx.post(f"{RELAY_BASE}/ingest/phone",
                   headers={"X-API-Key": RELAY_API_KEY},
                   json={"ts": int(datetime.now(timezone.utc).timestamp()),
                         "domain": "manual",
                         "metric": "note",
                         "value_text": text[:2000],
                         "source": "feishu"},
                   timeout=10.0)
    r.raise_for_status()
    return r.json()


# 「这句话我是要记下来，不是要问你」——带上前缀就完全不进查数据那条路。
#
# ⚠️ 存在的理由：一句话是在「问数据」还是在「记一笔」，只有写的人知道。
#    live.answer_query 靠关键词 + 疑问语气猜，猜得再准也有猜错的时候
#    （「这个电脑配置怎么样」是问句、又命中「电脑」，但它不是在问今天用了
#    多久电脑）。前缀是这个猜测的逃生口，同时也是「问了问题却只被记下来」
#    时的补救。
NOTE_PREFIXES = ("记：", "记:", "记一笔：", "记一笔:", "笔记：", "笔记:")


def split_note_prefix(text: str) -> str | None:
    """带「记：」前缀就返回去掉前缀的正文；没带返回 None。

    前缀后面是空的时候返回 ""（调用方提示「记什么」）—— 不能用 None，
    那样和「没带前缀」就分不开了。
    """
    for p in NOTE_PREFIXES:
        if text.startswith(p):
            return text[len(p):].strip()
    return None


def save_note(open_id: str, text: str) -> None:
    """把一句话存成笔记并回执。

    ⚠️ 失败必须说出来。静默失败等于把用户亲手写的东西丢了，而且他以为
       已经在日记里了 —— 那比一句报错糟得多。
    """
    try:
        relay_note(text)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR 记笔记失败：{type(e).__name__}: {e}", file=sys.stderr)
        send_text(open_id, f"没存进去：{e}")
        return
    # 记下原文（截断）：排查「我说的话它怎么没存/怎么当成问句了」时，
    # 日志里得有那句话本身，光有「记下了」什么都查不出来。
    print(f"[note] {text[:80]!r}")
    send_text(open_id, "记下了。\n"
                       "（这条会进今天的日记。想让我出题就发「?」）")


def pending_filtered(state: dict) -> list[dict]:
    """中继说还没答的题，减去**今天手动跳过**的那些。

    ⚠️ 这是一份**本地**状态，但和「什么算还没答」无关 —— 那个永远只有中继
       一份实现。这里记的是**我的偏好**（今天不想答这题），是机器人自己的事，
       中继无从知道，也不该知道。

    ⚠️ 不滤掉的话 `/skip` 是个假动作：那题在中继眼里仍然待答，下一轮
       `advance()` 会原样把它问回来。用户看到的会是「跳过了」然后马上又被问 ——
       比不支持跳过还糟。
    """
    day = datetime.now(TZ).date().isoformat()
    if state.get("skipped_day") != day:
        state["skipped_day"] = day
        state["skipped"] = []
    skipped = set(state.get("skipped") or [])
    return [q for q in relay_pending() if str(q.get("id")) not in skipped]


# ── 答案规范化 ──────────────────────────────────────────────────────
# ⚠️ 整个文件里最要命的一段。见模块开头的第 1 条。
_YES = {"做了", "是", "好", "对", "有", "嗯", "yes", "y", "1", "✓", "√", "true"}
_NO = {"没做", "没", "否", "没有", "不是", "无", "no", "n", "0", "×", "x", "false"}


def _norm_word(s: str) -> str:
    return s.strip().lower().replace(" ", "")


# 用来把「答案」和「用户自己说的话」分开的字符。
# 答案必须在最前面 —— 见 _split_answer() 的注释。
_SEPS = " \t\r\n，,。;；、:：!！?？()（）[]【】\"'“”‘’「」『』"


def _split_answer(text: str) -> tuple[str, str]:
    """把「答案 + 自己的话」拆成两段。答案必须在**最前面**。

    ⚠️ 为什么不让答案出现在任意位置。

       二值题/选择题最终写进 `value` 的只有 yes/no 或选项原文，`apply_answers`
       靠它决定「勾不勾任务」。而一旦勾了，任务表就被改了 —— 那个动作**不可逆**。
       所以这里宁可要求「答案先说」，也不去猜「哪一段才是答案」：
       猜错会把任务勾反，而且不会有人发现。

       用户想说的话进 `note`，原样带着走。

        做了，花了两小时        → ("做了", "花了两小时")
        1 整条做完了（其实差一点） → ("1", "整条做完了（其实差一点）")
        没做                    → ("没做", "")
    """
    t = text.strip().strip(_SEPS)
    for i, ch in enumerate(t):
        if ch in _SEPS:
            return t[:i].strip(), t[i:].strip(_SEPS)
    return t, ""


def _match_label(text: str, opts: list[str]) -> str | None:
    """把一句话对到某个选项原文上。逐字（或忽略大小写/空白）相等才认。"""
    for label in opts:
        if text == label or _norm_word(text) == _norm_word(label):
            return label
    return None


def canonicalise(q: dict, raw: str) -> tuple[str | None, str, str]:
    """把用户那句话变成 (值, 备注, 给用户的说明)。

    值必须是：
      · yes_no  → 恰好 "yes" 或 "no"（apply_answers 比的是 `== "yes"`）
      · choice  → **问题文件里那条选项的原文，逐字**（比的是字符串相等）
      · text    → 原样（这时「备注」留空，「值」本身就是内容）
    对不上就返回 (None, "", 提示)，由调用方重问 —— **绝不猜**。

    **可以带自己的话**：答案写前面，后面随便写，进 `note`。中继和
    apply_answers 一直都支持这个字段，只是以前这里从来没填过。
    """
    fmt = str(q.get("format") or "yes_no")
    text = raw.strip()

    if fmt == "text":
        return (text, "", "") if text else (None, "", "这题要回内容，不能是空的。")

    if fmt == "choice":
        opts = [str(o.get("label") or "") for o in (q.get("options") or [])
                if isinstance(o, dict)]
        if not opts:
            return None, "", "这题标了选择题，但问题文件里没有选项 —— 跳过它。"

        # 先试整句：选项原文里**可能**含分隔符（比如「做完了，但很勉强」），
        # 先拆的话会把一条合法选项拆坏。
        hit = _match_label(text, opts)
        if hit:
            return hit, "", ""

        head, rest = _split_answer(text)
        if head.isdigit():
            i = int(head) - 1
            if 0 <= i < len(opts):
                return opts[i], rest, ""
            return None, "", f"只有 {len(opts)} 个选项，回 1 到 {len(opts)}。"

        hit = _match_label(head, opts)
        if hit:
            return hit, rest, ""

        lines = "\n".join(f"  {i}) {o}" for i, o in enumerate(opts, 1))
        return None, "", (
            f"没认出这个选项。回数字就行：\n{lines}\n"
            "想补充就写在答案后面，比如「1 其实只做了一半」。")

    # yes_no
    n = _norm_word(text)
    if n in _YES:
        return "yes", "", ""
    if n in _NO:
        return "no", "", ""

    head, rest = _split_answer(text)
    n = _norm_word(head)
    if n in _YES:
        return "yes", rest, ""
    if n in _NO:
        return "no", rest, ""

    return None, "", (
        "回「做了」或「没做」。\n"
        "想补充就写在后面，比如「做了，花了两小时」—— 后半句会记成备注。")


# ── 渲染 ────────────────────────────────────────────────────────────
def render_question(q: dict, remaining: int) -> str:
    kind = {"commitment": "问责", "routine": "常规"}.get(str(q.get("kind") or ""), "")
    # 实时题 / 收尾题单独标出来：它们和晚上批量生成的问责批性质不同，
    # 混在一起会让人以为系统在乱问。
    # ⚠️ 收尾题也带 source="live"，所以必须先判 kind。
    if str(q.get("kind") or "") == "commitment":
        # 实时问责题也带 source=live；必须在 source 判断之前标成「问责」，
        # 否则它会显示成「实时」，和每小时的数据题混在一起。
        kind = "问责"
    elif str(q.get("kind") or "") == "summary":
        # 收尾流程里的「常规题」（体力/心情/人际…）单独标出来 —— 它们和
        # 数据拼出来的题性质不同，混在一起会让人以为系统在瞎问。
        kind = "常规" if q.get("routine") else "收尾"
    elif str(q.get("source") or "") == "live":
        kind = "实时"
    head = f"还剩 {remaining} 题" + (f"（{kind}）" if kind else "")
    lines = [head]

    # ⚠️ 题干本身**不写「今天」**（见 live_questions.py 的注释）。日期在这里按
    #    题目真实的 day 补上。
    #
    #    为什么必须有这一步：实时题的有效期是 1 天，所以**昨天晚上生成的题可能
    #    今天早上才被读到**。题干要是写着「今天 16:23–18:08 …」，而那是昨天的
    #    数据 —— 看着完全正常，但它是错的。实测在第一次联通时就踩到了。
    day = str(q.get("day") or "")
    today = datetime.now(TZ).date().isoformat()
    if day and day != today:
        lines += ["", f"⚠️ 这是 **{day}** 的题，不是今天的。"]

    lines += ["", str(q.get("text") or "").strip()]

    evidence = str(q.get("evidence") or "").strip()
    if evidence:
        # 证据是原文照抄，是这类提问的意义所在，要留着。
        lines += ["", f"（依据：{evidence}）"]

    fmt = str(q.get("format") or "yes_no")
    if fmt == "choice":
        for i, o in enumerate((q.get("options") or []), 1):
            if isinstance(o, dict):
                lines.append(f"  {i}) {o.get('label') or ''}")
        lines += ["", f"回一个数字（1-{len(q.get('options') or [])}），"
                      "后面可以接着写自己的话"]
    elif fmt == "text":
        lines += ["", "直接回内容就行"]
    else:
        lines += ["", "回「做了」或「没做」；后面可以接着写自己的话"]
    return "\n".join(lines)


# ── 飞书发送 ────────────────────────────────────────────────────────
_client = None
_client_lock = threading.Lock()


def feishu_client():
    """懒建、且全局一个。

    ⚠️ 每次调用都新建会有两个后果：多占长连接名额（每应用上限 50），
       以及每次都要重新换一遍 tenant_access_token。
    """
    global _client
    with _client_lock:
        if _client is None:
            import lark_oapi as lark

            _client = (lark.Client.builder()
                       .app_id(APP_ID).app_secret(APP_SECRET).build())
        return _client


# 飞书那几个「查起来会跑偏」的错误码，各自的真实原因和该去哪儿。
# ⚠️ 21001 那条特别值得留着：它**看起来**像 IP 白名单问题，实际不是 ——
#    IP 白名单的码是 99991401，而且**根本不作用于 access_token 接口**。
#    21001 是 type=policy / subtype=access_denied，即「你要的权限被数据安全
#    管控策略拦了」。实测在这个坑上差点指错方向。
_API_HINTS = (
    ("21001",
     "被飞书的**数据安全策略**拦了（不是 IP 白名单 —— 那个的码是 99991401，"
     "而且不作用于 token 接口）。\n"
     "      去看看两处：\n"
     "        · 开发者后台 → 权限管理：每个 scope 的状态是「已开通」还是"
     "「需申请 / 已拒绝 / 默认关闭」\n"
     "        · 管理后台 → 安全 → 数据安全 → 应用权限管控：这个应用有没有被拦"),
    ("230013", "目标用户不在机器人的**可用范围**里（或者被列进了禁用名单）。"),
    ("99991401", "调用方的公网 IP 不在应用的 **IP 白名单**里（安全设置 → IP 白名单）。"),
)


def explain_api_error(err: BaseException) -> str:
    """把一个飞书 API 异常翻成「该去改什么」。认不出来就原样返回。"""
    text = f"{type(err).__name__}: {err}"
    for code, hint in _API_HINTS:
        if code in text:
            return f"  ⚠️ code {code} —— {hint}"
    return ""


def send_text(open_id: str, text: str) -> None:
    """主动发一条文本。

    这里**不用 reply**（回复原消息）而是直接 create 到 open_id：少一层对
    message_id 的依赖，出错面更小。主动能力正是选飞书的全部理由，
    所以走的也是「主动消息」那条路径。

    ⚠️ 拿 tenant_access_token 也在这一步发生（SDK 内部做），所以**权限/策略
       类的问题会在这里第一次现形**，而不是在某个业务调用上。
    """
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    req = (CreateMessageRequest.builder()
           .receive_id_type("open_id")
           .request_body(CreateMessageRequestBody.builder()
                         .receive_id(open_id)
                         .msg_type("text")
                         .content(json.dumps({"text": text}, ensure_ascii=False))
                         .build())
           .build())
    try:
        resp = feishu_client().im.v1.message.create(req)
    except Exception as e:  # noqa: BLE001
        # SDK 拿不到 token 时是**抛异常**而不是返回 success=False 的响应，
        # 所以两条路都要接住。
        hint = explain_api_error(e)
        raise RuntimeError(f"飞书发送失败：{type(e).__name__}: {e}"
                           + (f"\n{hint}" if hint else "")) from None
    if not resp.success():
        raise RuntimeError(
            f"飞书发送失败 code={resp.code} msg={resp.msg}"
            + (f"\n{explain_api_error(RuntimeError(f'{resp.code}: {resp.msg}'))}"
               if resp.code else ""))


# ── 核心：推进一轮问答 ──────────────────────────────────────────────
def _asked_map(state: dict, day_iso: str) -> dict:
    """今天问过哪些题（qid → 题干/类型）。跨天自动清空。

    ⚠️ 为什么需要它：中继只存**答案**（`<题号>|<值>|<备注>`），题目原文在答完
       之后就不在「待答」列表里了。收尾写总结时要把「我问了什么、你怎么答的」
       配对喂给模型，所以题干得在发出去的那一刻记下来。
    """
    if state.get("asked_day") != day_iso:
        state["asked_day"] = day_iso
        state["asked"] = {}
    m = state.get("asked")
    return m if isinstance(m, dict) else {}


def _answers_today(day_iso: str) -> dict[str, tuple[str, str]]:
    """今天答过的题：qid → (答案, 备注)。答案就在中继的当天事件流里。

    ⚠️ 备注要一起带出来 —— 「为什么没做到」往往比 yes/no 本身有价值。
       这是 /qa 不可用时的降级路径。
    """
    try:
        r = httpx.get(f"{RELAY_BASE}/events", params={"day": day_iso},
                      headers={"X-API-Key": RELAY_API_KEY}, timeout=15.0)
        r.raise_for_status()
        evs = r.json().get("events") or []
    except Exception as e:  # noqa: BLE001
        print(f"WARN 读答案失败：{type(e).__name__}: {e}", file=sys.stderr)
        return {}
    out: dict[str, tuple[str, str]] = {}
    for e in evs:
        if e.get("domain") != "question" or e.get("metric") != "answer":
            continue
        parts = str(e.get("value_text") or "").split("|", 2)
        if parts and parts[0]:
            out[parts[0]] = (
                parts[1] if len(parts) > 1 else "",
                parts[2] if len(parts) > 2 else "",
            )
    return out


def maybe_write_summary(open_id: str, day_iso: str,
                        force: bool = False) -> bool:
    """收尾三题**全答完**了吗？答完就写一段当天总结。返回是否处理了。

    ⚠️ 「答完」靠**中继**判断（再拉一次待答，看还有没有 kind=summary 的题），
       不在本地留队列 —— 和「什么算还没答」永远只有一份实现是同一个原则。
    """
    try:
        pending = relay_pending()
    except Exception as e:  # noqa: BLE001
        print(f"WARN 收尾：拉不到待答：{type(e).__name__}: {e}", file=sys.stderr)
        return False
    if not force and any(str(x.get("kind") or "") == "summary" for x in pending):
        return False                    # 还有没收尾的题

    # 优先从 /qa 取**当天全部**回答（早晨批 + 实时批 + 收尾批），
    # 不再只挑 kind=summary。旧版中继没有 /qa 时退回原来的 summary-only。
    qa = live.relay_qa(day_iso, RELAY_BASE, RELAY_API_KEY)
    if not qa:
        state = load_state()
        asked = state.get("asked") if isinstance(state.get("asked"), dict) else {}
        answers = _answers_today(day_iso)
        for qid, v in asked.items():
            if not isinstance(v, dict) or str(v.get("kind") or "") != "summary":
                continue
            if qid not in answers:
                continue
            val, note = answers[qid]
            qa.append({
                "text": str(v.get("text") or ""),
                "answer": val,
                "note": note,
                "kind": "summary",
                "source": "live",
                "id": qid,
            })

    try:
        text, err = live.write_day_summary(day_iso, RELAY_BASE,
                                           RELAY_API_KEY, qa)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR 写总结失败：{type(e).__name__}: {e}", file=sys.stderr)
        return False
    # ⚠️ 即使总结没写成，也要把「数据 + 你的回答」发出去 —— 那才是复盘的主体，
    #    模型那一段只是收尾。整条链路里最不该因为 LLM 挂了就什么都不给。
    recap = live.day_recap(day_iso, RELAY_BASE, RELAY_API_KEY, qa, text)
    if text is None:
        recap += f"\n\n（总结没写成：{err}）"
    send_text(open_id, recap)

    # ⚠️ **整份复盘存成一条笔记** —— 明天写日记时它会作为「你说过的话」进提示词。
    #    不然它就只是聊天记录里的一段，不属于这套系统的记忆。
    try:
        relay_note("【今日复盘】" + recap)
    except Exception as e:  # noqa: BLE001
        print(f"WARN 复盘存笔记失败：{type(e).__name__}: {e}", file=sys.stderr)
    return True


def advance(open_id: str) -> None:
    """拉一次「还没答的题」，把下一题发出去。没有题就明说。

    每次都由**中继**重新判定「什么算还没答」—— 本地不留队列，就没有第二份
    真相，也就没有「两边不一致」这种故障。代价是每题多一次 HTTP，值。
    """
    state = load_state()
    try:
        questions = pending_filtered(state)
    except Exception as e:  # noqa: BLE001
        send_text(open_id, f"拉不到问题：{e}\n（中继多半没起来，看一下 "
                           f"systemctl status lifelogging-relay）")
        return

    if not questions:
        state["awaiting"] = None
        save_state(state)
        send_text(open_id, "今天没有待答的问题了。")
        return

    q = questions[0]
    qid = str(q.get("id") or "")

    # ⚠️ **先发消息，再落状态。**
    #    反过来写的话，发送失败会留下一个「正在等一题、但用户从没见过它」的状态，
    #    于是他下一条消息会被当成那道看不见的题的答案 —— 而且他还不知道为什么。
    #    这个顺序下最坏只是「用户看到了题、状态没存上」，他下次说话时重新走一遍
    #    advance()，能自愈。
    send_text(open_id, render_question(q, len(questions)))

    state["awaiting"] = qid
    sent = state.get("sent")
    if not isinstance(sent, dict):
        sent = {}
    sent[qid] = datetime.now(TZ).isoformat(timespec="seconds")
    state["sent"] = sent
    # 记下题干 —— 收尾时要用它和答案配成问答对。中继那边只存答案（`qid|值|备注`），
    # 题目原文在答完之后就不在待答列表里了。
    asked = _asked_map(state, datetime.now(TZ).date().isoformat())
    asked[qid] = {"text": str(q.get("text") or ""),
                  "kind": str(q.get("kind") or "")}
    save_state(state)


END_DAY_COMMANDS = ("/收尾", "/结束", "/收工", "/end")
# 「重来一次复盘」。存在的理由是失败可恢复：总结那一步可能因为 LLM 挂了而失败，
# 而答完的题不会再问一遍 —— 没有这个命令就永远补不回来。
RECAP_COMMANDS = ("/复盘", "/复盘了", "/recap")


def handle_end_day(open_id: str) -> None:
    """「我现在要结束这一天」—— 立刻出收尾题，不等自动时间。

    ⚠️ 一天什么时候结束只有你自己知道。`SUMMARY_AT` 是服务器按钟点猜的，
       而这个命令把它变成一件由你发起的事。

    ⚠️ 会覆盖当前 `awaiting`：你已经明确说「结束了」，那一道没答的就先放下。
       它仍然留在中继上，之后还会再被问到（不会丢）。
    """
    day_iso = datetime.now(TZ).date().isoformat()
    try:
        # 延迟 import：live_questions 在 import 时会 load_config()，
        # 不想让「配置文件有问题」把整个机器人拖得启动不了。
        import live_questions as lq
    except Exception as e:  # noqa: BLE001
        send_text(open_id, f"收尾功能不可用：{type(e).__name__}: {e}")
        return

    try:
        qs = lq.force_summary(day_iso, RELAY_BASE, RELAY_API_KEY)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR 收尾题生成失败：{type(e).__name__}: {e}", file=sys.stderr)
        send_text(open_id, f"收尾题没生成成：{type(e).__name__}: {e}")
        return

    if not qs:
        send_text(open_id, "今天的收尾题都已经答完了。")
        return

    q = qs[0]
    qid = str(q.get("id") or "")
    send_text(open_id, render_question(q, len(qs)))

    state = load_state()
    state["awaiting"] = qid
    sent = state.get("sent")
    if not isinstance(sent, dict):
        sent = {}
    sent[qid] = datetime.now(TZ).isoformat(timespec="seconds")
    state["sent"] = sent
    asked = _asked_map(state, day_iso)
    asked[qid] = {"text": str(q.get("text") or ""),
                  "kind": str(q.get("kind") or "")}
    save_state(state)
    print(f"[end-day] 收尾开始，还剩 {len(qs)} 题")


def handle_text(open_id: str, text: str) -> None:
    """一条用户消息进来之后做什么。跑在 worker 线程里，不受 3 秒限制。"""
    state = load_state()
    awaiting = state.get("awaiting")

    stripped = text.strip()

    if stripped in ("/help", "帮助", "/?"):
        send_text(open_id,
                  "【记一笔】\n"
                  "  直接发一句话 → 存进今天的记录，会进日记。\n"
                  "  例：「今天见了老王，聊了项目」\n"
                  "  带「记：」前缀的一定当笔记存（句子长得像问句时用它）；\n"
                  "  除此之外，带问号/疑问词的话会先被当成「问我今天」。\n"
                  "\n"
                  "【要我出题】\n"
                  "  回「?」→ 今天还没答的题\n"
                  "  二值题回「做了 / 没做」，选择题回数字；\n"
                  "  答案后面可以接着写自己的话，后半句记成备注：\n"
                  "    「做了，花了两小时」 / 「1 其实只做了一半」\n"
                  "  /skip 跳过这题 · /pending 重新拉一遍\n"
                  "\n"
                  "【问我今天】\n"
                  "  · 我今天去哪了 · 手机用了多久 · 今天解锁几次\n"
                  "  · 我今天在电脑上干了什么 / 现在在忙什么\n"
                  "  模板覆盖不到的交给模型，只喂今天的数据，没有就说没有。\n"
                  "\n"
                  "【白天与晚上】\n"
                  "  白天每个小时问你一次「刚才这一小时在干嘛」；\n"
                  "  发「/收尾」→ 立刻开始问收尾题，答完我给你一份今日复盘\n"
                  "  （23:00 也会自动问一次，如果你还没发过）\n"
                  "  发「/复盘」→ 重新生成一次今日复盘（总结失败时用它补）\n"
                  "\n"
                  "⚠️ 电脑数据来自笔记本每 10 分钟推的快照，答案里会写明\n"
                  "   「最后上报是几点」—— 是几小时前的就直说，别当成此刻。\n"
                  "⚠️ 日记和 vault 答不了：它们在笔记本的 Obsidian 库里，没推到服务器。\n"
                  "⚠️ 手表（步数/心率）还没接。")
        return

    if stripped in END_DAY_COMMANDS:
        handle_end_day(open_id)
        return

    if stripped in RECAP_COMMANDS:
        # ⚠️ force=True：不检查「还有没有没收尾的题」。
        #    存在的理由：总结那一步可能因为 LLM 挂了/额度用完而失败（实测过，
        #    见 2026-10-17 那次 DEEPSEEK_API_KEY 缺失）。而**答完的题不会再
        #    被问一遍**，没有这个命令就永远补不回来 —— 数据和回答都在，只差
        #    那一段总结。
        if not maybe_write_summary(open_id, datetime.now(TZ).date().isoformat(),
                                   force=True):
            send_text(open_id, "今天还没有可复盘的记录。")
        return

    if stripped == "/pending":
        advance(open_id)
        return

    if stripped == "/skip":
        if not awaiting:
            send_text(open_id, "现在没有在问的题。")
            return
        # 跳过 = **今天**不再问这题。⚠️ 不写任何答案事件 —— 写一条假的
        # 「没做」会让 Tasks.md 被勾错，那比不答更糟。
        day = datetime.now(TZ).date().isoformat()
        if state.get("skipped_day") != day:
            state["skipped_day"] = day
            state["skipped"] = []
        skipped = list(state.get("skipped") or [])
        if awaiting not in skipped:
            skipped.append(awaiting)
        state["skipped"] = skipped
        state["awaiting"] = None
        save_state(state)
        advance(open_id)
        return

    # ── 明确要记一笔 ──
    # 放在所有命令和查数据之前：带了前缀就是笔记，哪怕它长得像问句、
    # 哪怕正有一道题在等答案（笔记不该被题截胡，题也不会丢 —— 它还在
    # 中继的待答列表里）。
    #
    # ⚠️ 这里**不**清 awaiting：如果是昨天的实时题，下一条消息会走下面
    #    那段 expire 逻辑把它清掉。在这里清就得先拉一次 relay_pending，
    #    而那会让「没挂题时发一条笔记」也去碰网络 —— 不值。
    forced_note = split_note_prefix(stripped)
    if forced_note is not None:
        if not forced_note:
            send_text(open_id, "记什么？在「记：」后面写内容。")
            return
        save_note(open_id, forced_note)
        return

    if not awaiting:
        # 没有在等答案。先看这是不是一个「问我今天」的问题；不是的话，
        # 就当「给我题」——「随便一句都能出题」这个手感要保住。
        day_iso = datetime.now(TZ).date().isoformat()
        try:
            reply = live.answer_query(stripped, day_iso=day_iso,
                                      base=RELAY_BASE, key=RELAY_API_KEY)
            if reply == live.UNKNOWN_ANSWER and FREEFORM_LLM:
                # 模板答不了、但它看着确实是在问一句什么 —— 才交给模型。
                #
                # ⚠️ 顺序不能反。**已经能算出来的数字永远走模板**（确定、零成本、
                #    不会漂移）。只有模板覆盖不到的问题才该花钱，而且喂给模型的
                #    只有今天的原始数据，提示词里明令「数据里没有就说没有」。
                reply = (live.answer_freeform(stripped, day_iso=day_iso,
                                              base=RELAY_BASE, key=RELAY_API_KEY)
                         or reply)
        except Exception as e:  # noqa: BLE001
            # 中继挂了、Dawarich 挂了、配置没填 —— 都会走到这里。
            # ⚠️ 不能默默吞掉然后去出题：那会让「我问了它一个数据问题、
            #    它却开始给我出题」看起来像答非所问。
            print(f"ERROR 查数据失败：{type(e).__name__}: {e}", file=sys.stderr)
            send_text(open_id, f"查不到：{e}")
            return
        if reply:
            send_text(open_id, reply)
            return

        # ── 出题的暗号 ──
        # 这几个词仍然是「给我题」，不当事笔记。除此之外**一律记下来**。
        if stripped in ("?", "？", "题", "给我题"):
            advance(open_id)
            return

        if not stripped:
            return

        # ── 记一笔 ──
        #
        # ⚠️ 这是**有意的取舍**（使用者定的）：任何没被识别成「查数据」的消息
        #    都存成笔记，而不是被当成「给我出题」或被打回去重说。
        #    代价：一句没命中关键词的问句（「我今天咋这么累」）不会得到回答，
        #    而是被记下来。但它同时也进了当天的日记资料 —— 所以不是「丢了」，
        #    而是「记下来了」。想明确要题就发「?」。
        #
        # ⚠️ 反过来的保证在 live.answer_query 里：**不像在问的话永远到不了
        #    这里之上那条分支**。关键词里有「电脑」「网页」「应用」这种日常
        #    词，只靠它们判「查数据」会把随手记吃掉。
        save_note(open_id, stripped)
        return

    try:
        questions = relay_pending()
    except Exception as e:  # noqa: BLE001
        send_text(open_id, f"拉不到题目内容：{e}")
        return

    q = next((x for x in questions if str(x.get("id")) == str(awaiting)), None)
    if q is None:
        # 中继已经不认为这题待答了（别处答过、或问题文件换了一份）。
        state["awaiting"] = None
        save_state(state)
        advance(open_id)
        return

    # ⚠️ **实时题只属于它那一天。**
    #
    #    如果昨晚的实时题一直没答，`awaiting` 会跨夜挂着。第二天你发的第一句话
    #    ——哪怕是一个「?」、或者一条想记下来的笔记——会被当成**那道题的答案**。
    #    而实时题是 text 格式，`canonicalise()` 什么都收，所以**不会报错**，
    #    只会静默地把你的话记到昨天的题上，笔记也就没了。
    #
    #    问责批（笔记本生成的）不适用这条：它本来就是「问昨天」，第二天早上答
    #    才是正常用法。所以这里只按 source=live 判。
    today_iso = datetime.now(TZ).date().isoformat()
    if (str(q.get("source") or "") == "live"
            and str(q.get("day") or "") != today_iso):
        state["awaiting"] = None
        save_state(state)
        print(f"[expire] 昨天的实时题没答，清掉 awaiting：{awaiting}")
        # 这条消息不是答案 —— 按普通消息重新走一遍（现在 awaiting 已经空了）。
        handle_text(open_id, stripped)
        return

    value, note, hint = canonicalise(q, stripped)
    if value is None:
        send_text(open_id, hint)
        return

    try:
        res = relay_answer(str(awaiting), value, note)
    except Exception as e:  # noqa: BLE001
        send_text(open_id, f"答案没存进去：{e}")
        return

    # 回显「记下了什么」：有备注的时候尤其要回显，否则用户不知道
    # 自己写的那半句是被收下了还是被丢了。
    if str(q.get("format") or "yes_no") == "text":
        stored = "已记下。"
    else:
        stored = f"已记下：{value}"
        if note:
            stored += f"\n备注：{note}"
    print(f"[answer] {awaiting} = {value!r} note={note!r} -> relay {res}")
    send_text(open_id, stored)

    # 晚上收尾：这是最后一道总结题的话，立刻写总结，不再接着出题。
    if str(q.get("kind") or "") == "summary":
        try:
            if maybe_write_summary(open_id, datetime.now(TZ).date().isoformat()):
                return
        except Exception as e:  # noqa: BLE001
            print(f"WARN 收尾失败：{type(e).__name__}: {e}", file=sys.stderr)

    advance(open_id)


# ── worker：所有会阻塞的事都在这里 ──────────────────────────────────
_work: "queue.Queue[tuple[str, str, str]]" = queue.Queue()


def worker_loop() -> None:
    while True:
        message_id, open_id, text = _work.get()
        try:
            seen = load_state().get("recent") or []
            if message_id in seen:
                # 飞书在我们超时的时候会把同一条事件重推一次。这是**预期行为**，
                # 不是异常 —— 直接丢掉。
                print(f"[dup] 忽略重推的 {message_id}")
                continue
            handle_text(open_id, text)
            state = load_state()
            recent = [m for m in (state.get("recent") or []) if m != message_id]
            recent.append(message_id)
            state["recent"] = recent[-RECENT_MAX:]
            save_state(state)
        except Exception as e:  # noqa: BLE001
            # ⚠️ worker 里绝不能把异常抛出去 —— 抛一次线程就死了，而
            #    systemd 看进程还在，不会重启。那会变成「连着但永远不回话」。
            print(f"ERROR 处理消息时炸了：{type(e).__name__}: {e}", file=sys.stderr)
        finally:
            _work.task_done()


# ── 定时主动推送 ────────────────────────────────────────────────────
def push_next_live(open_id: str) -> bool:
    """有新的**实时题**就立刻推一题。返回是否推了。

    ⚠️ 为什么需要它。

        `live_questions.py` 每 30 分钟把当天的行踪/手机数据扫一遍，生成一题推到
        中继 —— 但它**只生成、不发送**。而机器人原来只在每天 08:30 推一次。
        于是「实时提问」的实际效果是：第二天早上一次性看到一堆昨天的题。
        名字和效果差得很远。实测由用户指出。

    五条约束：

      · **别打扰时段不推**（默认 22:00–08:00）。生成器有窗口，但那只管
        「生不生成」；题生成完还躺着等这里推，所以这里也必须看时间。
      · **只推当天的题**。隔了夜的「过去一小时」没有意义，早上收到只会困惑。
      · **只在没在等答案时推**（`awaiting` 为空）。否则会把正在答的那题顶掉，
        用户刚打了一半的答案会被当成另一题的答案。
      · **只推 source="live" 的题**。笔记本那批问责题仍然每天整批推一次 ——
        它们是「晚上的事」，不该在白天插进来。
      · **先发再落状态**，和 advance() 同一个理由：发送失败不该留下一个
        「在等一题用户没见过的题」的状态。
    """
    now = datetime.now(TZ)
    # ⚠️ 夜间静音**不能一刀切**：自动收尾（默认 23:00）落在静音时段里，
    #    挡住就等于「到点不收尾」，而实时题过零点就作废了。
    #    所以静音只拦小时题，收尾题照发 —— 那是你自己把一天的结束定在这个点上的。
    quiet = in_quiet_hours(now)

    state = load_state()
    if state.get("awaiting"):
        return False

    try:
        pending = relay_pending()
    except Exception as e:  # noqa: BLE001
        print(f"WARN: 实时题轮询失败：{type(e).__name__}: {e}", file=sys.stderr)
        return False

    today = now.date().isoformat()
    # ⚠️ 只要**当天**的实时题。
    #    「过去一小时」的题隔了夜就完全没意义了 —— 早上八点收到一条
    #    「昨天 21:00–22:00 这一段：…」只会让人困惑。中继那边留 1 天是为了
    #    「晚上生成的、第二天早上还能看到」，但那是给**问责批**的余地；
    #    实时题不该享受这个待遇。
    live_qs = [q for q in pending
               if str(q.get("source") or "") == "live"
               and str(q.get("day") or "") == today]
    if quiet:
        # 静音时段只放行收尾题（见上面的说明）。
        live_qs = [q for q in live_qs
                   if str(q.get("kind") or "") == "summary"]
    if not live_qs:
        return False

    sent = state.get("sent")
    if not isinstance(sent, dict):
        sent = {}
    for q in live_qs:
        qid = str(q.get("id") or "")
        if not qid or qid in sent:
            continue
        send_text(open_id, render_question(q, len(live_qs)))
        state["awaiting"] = qid
        sent[qid] = datetime.now(TZ).isoformat(timespec="seconds")
        state["sent"] = sent
        asked = _asked_map(state, today)
        asked[qid] = {"text": str(q.get("text") or ""),
                      "kind": str(q.get("kind") or "")}
        save_state(state)
        print(f"[live-push] {qid}")
        return True
    return False


def live_push_loop() -> None:
    """每隔 LIVE_POLL_MIN 分钟看一眼有没有新的实时题。"""
    if not LIVE_PUSH:
        print("FEISHU_LIVE_PUSH=0，不主动推实时题（只剩每天定时那一次）。")
        return
    if not OWNER_OPEN_ID:
        print("WARN: 没配 FEISHU_OWNER_OPEN_ID，实时题推不了。", file=sys.stderr)
        return
    print(f"→ 实时题轮询：每 {LIVE_POLL_MIN} 分钟")
    while True:
        try:
            push_next_live(OWNER_OPEN_ID)
        except Exception as e:  # noqa: BLE001
            print(f"WARN: 实时题循环出错：{type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(LIVE_POLL_MIN * 60)


def push_loop() -> None:
    """每天 PUSH_AT 主动推一次。这就是选飞书而不是 QQ 的全部理由。"""
    if not PUSH_AT:
        print("FEISHU_PUSH_AT 是空的，不做定时推送。")
        return
    if not OWNER_OPEN_ID:
        print("WARN: 没配 FEISHU_OWNER_OPEN_ID，定时推送没法发。"
              "先给机器人发一条消息，把你自己的 open_id 从日志里抄出来。",
              file=sys.stderr)
        return

    while True:
        try:
            now = datetime.now(TZ)
            hh, _, mm = PUSH_AT.partition(":")
            due = now.replace(hour=int(hh), minute=int(mm or 0),
                              second=0, microsecond=0)
            day = now.date().isoformat()
            state = load_state()
            if now >= due and state.get("last_push_day") != day:
                n = 0
                try:
                    # 用 filtered 而不是 relay_pending()：今天已经跳过的那几题
                    # 不该把一次「其实没得问」的推送撑起来 —— 推出去之后
                    # advance() 只会回一句「今天没有待答的问题了」。
                    n = len(pending_filtered(state))
                except Exception as e:  # noqa: BLE001
                    print(f"WARN: 推送前拉题目失败：{e}", file=sys.stderr)
                if n:
                    advance(OWNER_OPEN_ID)
                    print(f"[push] 已推送，待答 {n} 题")
                else:
                    print("[push] 今天没有待答的题，不打扰")
                # ⚠️ 无论有没有题都记上 —— 否则没题的那天会每 60 秒重试一次。
                #
                # ⚠️ 这里要**重新读一次**再写，不能用上面那份快照：
                #    advance() 刚刚落过 awaiting（它在 worker 线程那边也可能同时
                #    在写），拿旧快照 save 会把它盖回去 —— 于是那道刚推出去的题
                #    答案会被当成「给我出新题」。窗口极小，但后果是静默的。
                fresh = load_state()
                fresh["last_push_day"] = day
                save_state(fresh)
        except Exception as e:  # noqa: BLE001
            print(f"WARN: 推送循环出错：{type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(60)


# ── 飞书事件入口 ────────────────────────────────────────────────────
def on_message(data) -> None:
    """⚠️ 必须**立刻返回**。

    飞书要求 3 秒内处理完且不抛异常，否则触发超时重推。所以这里只做提取和
    入队，一个网络请求都不发。真正的活在 worker 线程里。
    """
    try:
        msg = data.event.message
        sender = data.event.sender
        open_id = sender.sender_id.open_id
        content = json.loads(msg.content)          # ⚠️ content 本身是 JSON 字符串
        text = content.get("text") or ""
        message_id = msg.message_id

        # 把你自己的 open_id 打出来 —— 这是拿到它的**零权限**办法：
        # 不需要 contact 权限，只要有人给机器人发过一句话。
        print(f"[recv] open_id={open_id} chat={msg.chat_type} text={text[:80]!r}")
        if not OWNER_OPEN_ID:
            print(f"    ⚠️ FEISHU_OWNER_OPEN_ID 还没配。把它填进 "
                  f"/etc/lifelogging/feishu-bot.env：\n"
                  f"        FEISHU_OWNER_OPEN_ID={open_id}", file=sys.stderr)

        _work.put((message_id, open_id, text))
    except Exception as e:  # noqa: BLE001
        # 这里**必须**吞掉：抛出去就会变成「超时」→ 飞书重推 → 又炸 → 死循环。
        print(f"ERROR 解析事件失败：{type(e).__name__}: {e}", file=sys.stderr)


def run() -> None:
    import lark_oapi as lark

    # ⚠️ 启动时**当场**定下中继地址，并且打出来。
    #    定错了的代价是静默的（见 relay_base_candidates 的注释），所以这一行
    #    日志是这个服务里最该被人看见的一行。
    global RELAY_BASE
    picked = pick_relay_base()
    if picked != RELAY_BASE:
        tail = f"（配置里是 {RELAY_BASE}）" if RELAY_BASE else ""
        print(f"→ 中继地址自动选定 {picked}{tail}")
    else:
        print(f"→ 中继地址 {picked}")
    RELAY_BASE = picked

    if not OWNER_OPEN_ID:
        print("⚠️  没配 FEISHU_OWNER_OPEN_ID —— 被动应答能用，主动推送不能。",
              file=sys.stderr)
    if not PUSH_AT:
        print("⚠️  FEISHU_PUSH_AT 为空，不会主动推。", file=sys.stderr)

    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=push_loop, daemon=True).start()
    # 实时题单独一个循环：它要按分钟级去看中继，而不是等每天 08:30 那一次。
    threading.Thread(target=live_push_loop, daemon=True).start()
    # 提前把 GeoNames 城市表准备好。第一次要下载 30–40 MB，那一下绝不能在
    # 处理用户消息的线程里发生 —— 否则机器人会显得「卡死」很久。
    threading.Thread(target=live.warm_geocoding, daemon=True).start()

    # ⚠️ builder 的两个参数在长连接模式下**必须都是空串**。
    #    填了 verification_token / encrypt_key 是给 Webhook 模式的，长连接用不上。
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .build())

    client = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler,
                            log_level=lark.LogLevel.INFO)
    print("正在连接飞书长连接…（连上会打印 connected to wss://…）")
    client.start()          # 阻塞。断了 SDK 会自己重连。


def main() -> int:
    ap = argparse.ArgumentParser(description="飞书机器人：每日提问的收发通道")
    ap.add_argument("--check", action="store_true",
                    help="只自检配置和中继连通性，不连飞书")
    ap.add_argument("--push-test", metavar="文本",
                    help="主动发一条消息就退出（验证推送链路）")
    args = ap.parse_args()

    if args.check:
        problems = check_config()
        problems += check_relay()
        if problems:
            print("\n自检没过：")
            for p in problems:
                print(f"  ✗ {p}")
            return 1
        print("\n自检通过。")
        return 0

    if args.push_test:
        problems = check_config()
        if not OWNER_OPEN_ID:
            problems.append("FEISHU_OWNER_OPEN_ID 没配，不知道发给谁")
        if problems:
            for p in problems:
                print(f"  ✗ {p}", file=sys.stderr)
            return 1
        try:
            send_text(OWNER_OPEN_ID, args.push_test)
        except Exception as e:  # noqa: BLE001
            # ⚠️ **不要甩 traceback。** 这条命令是给人用来定位问题的，而一堆
            #    栈帧只会把真正有用的那句「code 21001 … 该去哪儿改」埋掉。
            #    实测就是这样的：屏幕上最显眼的是 lark_oapi 的七八层调用栈，
            #    而「Access denied by access control policy」缩在最后一行。
            print("  ✗ 发不出去。", file=sys.stderr)
            for line in str(e).splitlines():
                print(f"    {line}", file=sys.stderr)
            return 1
        print(f"已发给 {OWNER_OPEN_ID}")
        return 0

    problems = check_config()
    if problems:
        # ⚠️ 启动就拦住，别让它带着错配置进长连接 —— 那样失败会以
        #    「连上了但什么都不回」的形式出现，比直接退难查得多。
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        # EXIT_CONFIG 而不是 1：让 systemd **不要**重启（见上面那个常量的注释）。
        return EXIT_CONFIG
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
