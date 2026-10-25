# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## 这是什么

个人全量生命记录系统（Lifelogging）：把电脑 / 手机 / 可穿戴的可记录数据自动采集 → 规则 + LLM
理解 → 结构化日记 → 写入 Obsidian，并把**脱敏聚合**与**原始归档**发布到 `liveinpassion.me`。

**双机分工**（2026-10 起，这是理解本仓库的第一件事）：

- **Win11 笔记本**：有 GUI 的采集（ActivityWatch 必须在本机）、规则归类、写 vault。
  靠计划任务 + 无窗口守护程序 `windows/live-daemon/LifeLoggingLive.exe`。
- **Linux 服务器（24h 开着，Tailscale 主机名 `vm-0-17-ubuntu-1`）**：手机上报的中继、
  每 15 分钟用 `close_day.py` 写日记正文、每 2 分钟发实时 NOW、每 15 分钟发布手表轨迹。
  代码在 `/opt/lifelogging/repo`（git clone），venv 在 `/opt/lifelogging/venv`，
  数据在 `/var/lib/lifelogging/`，密钥在 `/etc/lifelogging/*.env`。
- **个人站点**（另一个仓库 `C:\Users\LongGe\Desktop\liveinpassion`）：Cloudflare Worker
  + D1/R2，只读本仓库发布上去的数据。

**设计原则**：

1. **默认全部本地 / 自托管**，外发是**可控**的：唯一必需的外发是发给 LLM 的日记材料；
   发到站点的聚合 / 原始归档、飞书机器人、Cloudflare Tunnel 都是可选项。
   关掉 `life_site`、不装飞书 / Cloudflare 时，除 LLM 调用外数据不出本机。
2. **不录屏、不录音、不记键盘**。电脑端的全部依据是 ActivityWatch 采集的**窗口标题**，
   外加可选浏览器扩展的 URL 与页面标题。`collect_desktop.py` 里还留着 screenpipe
   （音频转录 / OCR / 会议）的代码，但 `sources.screenpipe.enabled: false`，整条关闭；
   重新开启前先想清楚这条原则。
3. **发布 ≠ 脱敏**。`/life` 只发聚合；`/life/archive` 的原始文档**不脱敏** ——
   窗口标题、URL、坐标、日记正文全部公开，只替换凭证。改发布逻辑前先确认这条边界。

**语言约定**：代码、注释、README、docs、本文件 —— 中文。`vault/05-Journal`、
`06-AI-Sessions`、发到站点的副本、提问正文 —— 英文。分类名（`Work`/`Study`/…）一律英文。
日志和异常消息也是中文，不要因为「vault 是英文」就以为整个仓库是英文。

---

## 常用命令

```bash
# ── 环境 ────────────────────────────────────────────────
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml    # 然后填 vault 路径 / API key / 服务器地址

# ── 验证（不碰任何采集源，改完代码先跑这个）────────────────
python scripts/smoke_test.py                         # 64 项，全绿才算过；无参数、不能只跑一项
powershell -NoProfile -File scripts\check_ps1.ps1    # 只做语法解析，不检查 BOM —— BOM 要自己确认

# ── 常驻服务 ────────────────────────────────────────────
python -m middleware.events_api                      # 统一事件表，127.0.0.1:18787

# ── 单日流水线（按顺序）──────────────────────────────────
python scripts/collect_desktop.py --date today
python scripts/collect_phone.py   --date today
python scripts/collect_phone_events.py --date today
python scripts/collect_watch.py   --date today
python -m middleware.ingest_gadgetbridge --date today [--db PATH] [--dry-run]
python scripts/normalize.py       --date today       # → state/timeline-<day>.{json,md} + blocks 表

# ── 日记（正文在服务器上生成）────────────────────────────
python scripts/push_journal_input.py --date today    # 标 final=true 才允许写
python scripts/pull_journal_output.py --days 3       # 取回正文并组装 vault
# 调试可在本机直跑（会花钱）：python scripts/generate_journal.py --date today --dry-run

# ── 整条流水线（计划任务用的就是这个）────────────────────
powershell -ExecutionPolicy Bypass -File scripts\run_daily.ps1
powershell -ExecutionPolicy Bypass -File scripts\run_daily.ps1 -Date 2026-10-12
powershell -ExecutionPolicy Bypass -File scripts\run_daily.ps1 -Refresh   # 只拉今天，不推进水位线
#   其它参数：-SkipAI  -DryRun  -NightlyHour 23  -MaxBackfillDays 14  -NoNotify

powershell -ExecutionPolicy Bypass -File scripts\run_live.ps1             # 2 分钟实时链路
#   参数：-HeavyMinutes 15  -RawMinutes 0  -NoPublish  -DryRun

# ── 发布 ────────────────────────────────────────────────
python scripts/publish_life_site.py --days 30        # 聚合 → /api/life/ingest
python scripts/publish_life_raw.py  --days 30        # 原始归档 → /api/life/raw/ingest
python scripts/publish_live_now.py                   # 实时快照 → /api/life/now

# ── 成本 ────────────────────────────────────────────────
python scripts/cost_guard.py status                  # 退出码 0/1/2 = 绿/黄/红
python scripts/cost_guard.py gate                    # 超预算则退出码 1
python scripts/cost_guard.py report --days 7
python scripts/cost_guard.py check-deepseek          # 余额 + 按 7 日均值推算可用天数
python scripts/reconcile_cost.py --days 7 [--write-vault]

# ── 其它 ────────────────────────────────────────────────
python scripts/geo.py [lat lon]                      # 离线逆地理编码自测
powershell -File scripts\setup_scheduler.ps1         # 注册 Daily + Refresh + Desktop 三个任务
powershell -File scripts\setup_live_task.ps1         # 可选；实际推荐 windows/live-daemon 的 exe
```

服务器侧：

```bash
ssh vm-0-17-ubuntu-1                                  # Tailscale
sudo /opt/lifelogging/repo/server/deploy.sh           # git ff-only + 按改动重启常驻服务
```

`deploy.sh` **不装依赖、不更新 unit/timer**。动了 `requirements.txt` 要手动
`pip install -r ...`；动了 unit 要手动复制并 `systemctl daemon-reload`。
更新后别只看 `/health`（新旧版本长得一样），打一个新路由（如 `/ask/pending`）确认。

**没有单元测试框架**。`scripts/smoke_test.py` 就是测试套件（当前 64 项）；改了
`normalize.py` / `common.py` / `events_api.py` / 发布逻辑后，正确验证方式就是跑它。
本机没装 `recall`、`lark_oapi` 时只会 warn，不影响 64 项全绿 —— 那不是失败。

---

## 架构

```
采集层（Win11 本机 / 手机 / 手表 / 中继）
  ActivityWatch ─┐
  Dawarich ──────┼→ events.db 的 events 表（长表）
  中继手机事件 ──┤   笔记本侧只经 HTTP（EventsClient / X-API-Key）写入，
  Gadgetbridge ──┤   没有脚本直接写这个 SQLite
  GPX / FIT ─────┤
  飞书自评 / AI 账单 ┘
         ↓
理解层（规则为主，LLM 只出现在少数点）
  normalize.py → timeline-<day>.json/md + blocks 表
  features / link_graph / routine / baseline / memory / labels / structure_text
         ↓
     ┌───┴────────────────────────────┐
  存储层 vault/（01–07）          发布层 publish_life_site / raw / now
  （笔记本组装）                  ＋ 服务器 watch-publisher（不依赖笔记本）
```

> **「当天数据」有两个不同的东西，别混**：`events` 表里是采集器给的**按 app/站点聚合的
> 日汇总**；`timeline-<day>.json` / `blocks` 表里是 normalize 把**原始切片切分合并后的时间块**。
> 两者格式和用途都不同。

### domain 与 metric

新增一种数据 = 新增一个 `domain` 值，不改表结构。当前实际使用的：

| domain | 来源 | 内容 |
|---|---|---|
| `screen` | `collect_desktop.py` | ActivityWatch 窗口 / AFK / 浏览器（`app_usage` 等） |
| `location` | `collect_phone.py` | Dawarich 停留点 / 轨迹 / 通勤 |
| `health` | `ingest_gadgetbridge.py` | 手环步数 / 睡眠 / 心率 |
| `fitness` | `import_gpx.py` / `import_fit.py` | 锻炼轨迹，固定 `metric=watch_track` |
| `phone` | `collect_phone_events.py` | 中继的解锁 / 切应用事件 |
| `web` | 浏览器扩展 | 页面标题 / URL |
| `manual` | 手工 | — |
| `ai` | `collect_ai_usage.py` | 每日花费 / 余额 / token |
| `audio` | screenpipe（**默认关闭**） | 音频转录 / 会议 |
| `question` | 问答链路 | `metric=answer`，问答与打卡 |

> 例外：`push_desktop.py` / `push_health.py` 只把已算好的汇总推到中继给机器人，
> `server/watch-publisher/` 直接发布原始 `watch` 文档 —— 这两条都**不写** `events.db`。

### 脚本地图

- **采集 / 输入**：`collect_desktop` `collect_phone` `collect_phone_events`
  `collect_watch`（从中继拉 Gadgetbridge DB）`collect_media`（拉 GPX/FIT 到
  `state/gpx|fit/<sha1>`）`collect_labels`（飞书自评 → `state/labels.jsonl`）
  `collect_ai_usage`；`push_desktop` / `push_health` 把已算好的汇总推到中继给飞书机器人。
- **理解 / 分析**：`normalize.py`（确定性规则，核心）；`label_free_text`（自由文本 → 1–5 分 +
  证据）、`structure_text`（自由文本 → 结构化事件）、`learn_apps` / `app_map`（未知应用映射）、
  `features`（日状态向量，带 `FEATURE_VERSION`）、`link_graph`、`routine`（课表 vs 实际 →
  偏离）、`baseline`（中位数 + MAD）、`memory`（FTS5 长期记忆）、`dataset`（离线训练集，
  **不在定时流水线**）。
- **问答 / 复盘**：`generate_questions`（问责 4 + 常规 3）、`live_questions`（服务器，
  白天每小时一条）、`apply_answers`（答「做了」勾 `Tasks.md`）、`weekly_review`（已结束的
  ISO 周，**会调 LLM**）、`qa_client`、`merge_ledger`。
- **日记**：`push_journal_input.py` → 中继 `journal-input-<day>.json`；
  服务器 `server/close-day/close_day.py` 调 AI 写正文 → `journal-output-<day>.json`；
  `pull_journal_output.py` 取回、写 `vault/05-Journal`、追写 `06-AI-Sessions`，最后调
  `write_notes.write_all()` 铺 01/02/03/04/07。`generate_journal.py` 仍可在本机直跑，
  但**不是定时主链路**。
- **发布 / 运维**：`publish_life_site` `publish_life_raw` `publish_live_now` `translate`
  `cost_guard` `reconcile_cost` `geo` `write_notes` `common`；入口 `run_daily.ps1`
  `run_live.ps1` `setup_scheduler.ps1` `setup_live_task.ps1`、根目录 `更新数据.cmd`。

### 笔记本上的定时任务

| 任务 | 何时 | 做什么 |
|---|---|---|
| `LifeLogging-Refresh` | 每 2 小时 | 只拉今天，不调 AI、不推进水位线（= `-Date today -SkipAI`） |
| `LifeLogging-Daily` | 23:00 + 每次解锁 / 登录 | 正式处理：补跑漏天、采集、normalize、推日记输入、发布，推进水位线 |
| `LifeLogging-Desktop` | 每 10 分钟 | `push_desktop.py`，让机器人能在笔记本睡着后回答电脑的事 |
| `LifeLoggingLive.exe` | 每 2 分钟 | `run_live.ps1`：桌面 / 手机事件 / normalize / 当天聚合 / NOW；每 15 分钟重采集位置、手表、AI 账单，并单独发当天 `blocks` 原始归档 |

`LifeLogging-Live` 计划任务已被停用（会闪 PowerShell 窗口），别为了「再注册一次」重跑
`setup_live_task.ps1` 把会闪窗的任务重新启用。窗口全部隐藏，**想知道跑没跑看
`state/last_run.md`**（摘要 + 最近 15 次），日志在 `state/logs/`。

> ⚠️ 没有 `Refresh` 时，当天的数据永远不会自己出现：`Daily` 白天必然什么都不做，
> 23:00 那次又需要机器开着。这是 refresh 存在的理由。

### 服务器上的服务

| 服务 | 形态 | 作用 |
|---|---|---|
| `lifelogging-relay`（`server/phone-relay/`） | 常驻，`100.x:8788` | 手机唯一上报入口：手机事件、Gadgetbridge DB、GPX/FIT 上传、提问收发、日记中转。**手机事件和问答是不可重建的唯一副本** |
| `lifelogging-close-day`（`server/close-day/`） | timer 15 分钟 | 扫最近 3 天，只有 `final=true` 才调 AI 写正文；**不碰 vault** |
| `lifelogging-live-now`（`server/live-now/`） | timer 2 分钟 | 组装 `/api/life/now`；笔记本关机后继续更新位置 / 手机 / 问答 / 手表 |
| `lifelogging-watch-publish`（`server/watch-publisher/`） | timer 15 分钟 | 直接解析中继的 GPX/FIT → `life/raw/watch/<day>.json`；**不写 events.db、不做聚合** |
| `lifelogging-feishu-bot`（可选） | 常驻，出站长连接 | 提问推送与文字答题；挂了是**静默的**（进程活着、长连接死了） |
| `lifelogging-live-questions`（可选） | timer 30 分钟 | 08:00–22:00 生成小时实时题；默认 23:00 收尾 |

`server/README.md` 开头还写着「两个服务」，且没列 watch-publisher —— 以本文件为准。
Cloudflare Tunnel（`server/cloudflare-tunnel/`）把中继暴露成 `relay.liveinpassion.me`，
因为 Android 同一时刻只允许一个 VPN，Tailscale 和代理软件会互相踢掉；手机改走 Tunnel 后
Tasker 的每个 HTTP action 都要带 Access Service Token 头。

### 发布契约（站点是另一个仓库）

- 聚合 `POST /api/life/ingest`：`{"v":1,"days":[...]}`。日对象含
  `v/day/categories/trackedHours/topApps/topSites/health/location/phone/ai/workouts/
  dimensions/collapsedHours`，另有 `allTime`。位置只到城市级，不含坐标 / 轨迹 / 标题 /
  URL / 包名 / 余额。Worker 侧字段白名单会 400。
- 原始 `POST /api/life/raw/ingest`：`{"v":1,"docs":[{kind,id,format,content}]}`。
  kind：`journal / events / blocks / notes / tasks / qa / weekly / vault`，
  外加站点内部的 `watch`（不进归档 tab，只在索引 `watchDays` 里列日期）。
  **不脱敏**，只清洗凭证。`blocks` 是例外：`run_live.ps1` 每 15 分钟用 `--kinds blocks`
  单独发当天的份（站点七维详情按天拉它），其余 kind 仍然只在 23:00 那轮发。
- 实时 `POST /api/life/now`：`{"v":1,"ts_utc","location","desktop","phone","watch","qa"}`，
  **故意含经纬度**。
- 鉴权统一 Bearer `LIFE_INGEST_TOKEN`（环境变量，和 Worker secret 相同；watch-publisher 也用
  同一个，不要新建）。
- 发布前会把中文副本翻成英文（`translate.py`，缓存 `state/translation_cache.json`）；
  本机 `events.db`、vault、问答原文不动。任何一段翻译失败就中止本轮、保留线上英文版。
- 今天的数据是「当天到目前为止」：站点显示 `TODAY · LIVE`、`· SO FAR`，不要当完整一天。

### 七维与 collapsedHours

`publish_life_site.py` 的 `build_dimensions` 产出 `Work / Study / Life / Transport /
Leisure / Health / Social`（小时）：Work/Study/Life/Leisure 来自桌面分类，Transport 来自
GPS 通勤，Health 来自手表跑步，Social 来自手机社交应用；手机上的对话式 AI 并入 Study；
**`Other` 不进七维**。七维是**各来源读数之和，不是时间并集**（电脑前刷手机算两次）。

`collapsedHours` 才是真实去重时长：把桌面 blocks、交通、锻炼、手机活跃区间做**区间并集**，
再裁到本地日。站点雷达底部 `UNIQUE TIME` 用它，`OVERLAP = 七维之和 − 并集`，而且只是下限
（桌面 `Other` 在并集里、不在七维里）。改任何一个口径，都要同步站点
`LifeRadar.vue` 的显示与 `tools/verify-life.mjs`。

### GPX / FIT

- 手机 Tasker → 中继 `/upload/gpx|fit`（按内容 sha1 存）→ 笔记本 `collect_media.py` 拉到
  `state/gpx|fit/` → `import_gpx.py` / `import_fit.py` → `domain=fitness /
  metric=watch_track`（`source=gadgetbridge-gpx|fit`）。
- `meta` 含 `path`、`distance_km`、`start_utc`、`end_utc`、`points`（路径最多 500 点）、
  `samples`（逐秒采样，最多 240 点）。
- `samples` 字段：`t`（相对开始秒）、`hr`（**只有 FIT 有**）、`v`（m/s）、`alt`、
  `d`（累计米）、`temp`（°C，可选）。除 `t` 外都可选，全空就省略该键。
- 同一次活动的 FIT + GPX 是**两份文件**，导入时各写一条；站点按**开始时间**去重
  （实测逐秒相同），路径取点数多的、采样取有心率的那份。不要按文件名去重 ——
  中继按 sha1 存，两边文件名不同。
- **GPX/FIT 不进 MOTION 聚合**：步数 / 心率带 / RUNNING / BEST PACE / LONGEST / VO2MAX
  来自笔记本从 Gadgetbridge 数据库算的聚合，与这两个文件无关。

---

## 必须知道的约束（改动前先读这一节）

### Windows / 编码

- **所有 `.ps1` 必须是 UTF-8 *带 BOM*。** Windows PowerShell 5.1 会把无 BOM 文件按 GBK
  解码，中文字节错位吃掉后面的 ASCII 大括号，报出指向**无关行号**的 `Missing closing '}'`
  —— 看起来像语法错误，其实是编码问题。编辑器、VS Code 默认值、Git 工具都会去掉 BOM。
  `scripts/check_ps1.ps1` **只做语法解析、不检查 BOM**，改完 `.ps1` 必须自己确认 BOM。
- `requirements.txt` 里的 `tzdata` 在 Windows 上是**必须的**：系统不带时区库，缺了
  `ZoneInfo("Asia/Shanghai")` 直接抛异常，整套系统起不来。
- 控制台输出编码由 `scripts/common.py` 在导入时统一改成 UTF-8；`run_daily.ps1` 里同时设了
  `PYTHONIOENCODING` 和 `[Console]::OutputEncoding`，**两边都要设**（一边写 UTF-8、一边按
  GBK 读，日志里的 `→` `✓` 会变乱码）。
- `.gitattributes` 强制 `.sh`/`Dockerfile` 存 LF、`.ps1` 存 CRLF 并显式写死，不依赖各人的
  `core.autocrlf`。别去动它。
- **端口**：本机 events API 是 **18787**（1024–15000 是 Windows 动态端口池，别用 8787）；
  服务器中继是 **8788**。两者不要混。`run_daily.ps1` 读 config 端口要按字段名解析，
  **不要用 `Select-String` 抓第一条**（`activitywatch` 的 5600 排在 `events_api` 前面）。

### 流水线 / 水位线 / 幂等

- 采集器写入前会**先删掉当天该 source 的数据**（`DELETE /day?day=&source=`）；`POST /blocks`
  也是先删当天再插。所以脚本可以随意重跑。
- `events` 表唯一索引 `(source, domain, metric, ts_utc, COALESCE(value_text,''))`；
  `blocks` 表 `(day_local, start_utc, category)`，靠 `INSERT OR IGNORE` 兜底。
- 时间：`ts_utc` 是 ISO8601 UTC 带 `Z`；`day_local` 是**服务端按 `config.timezone` 算出的
  本地日期分区键**。一个本地日会跨两个 UTC 日，查数据一律用 `common.Day` 拿 UTC 区间。
- 水位线在 `state/last_processed.json`，只有**当天过完**（`final=true`）且 `relay_ok=true`
  才推进；手动 `-Date` 不推进，`-Refresh` 永不推进。缺一条，当天就可能被半截数据标成已完成。
- `pull_journal_output.py` **故意在日循环之外**：水位线管的是「采集做完没有」，不是
  「日记写好没有」。取不到时不能谎报成功或失败；它往前扫 3 天、自身幂等。
- 流水线里**大部分采集步骤失败不中断后续**（采不到位置不该导致日记写不出来）——
  但 **events API 不可达会硬退出**，预算闸门也会停止本次运行。别把「失败继续」当成绝对规则。
- `-SkipAI` **不是全局 AI 开关**：`collect_labels` / `label_free_text` / `structure_text`
  只判断 `-DryRun`，`-Refresh` 仍可能调用它们（通常只处理新回答）。`learn_apps`、
  `generate_questions`、`weekly_review`、`reconcile_cost`、`publish_life_raw` 才明确受
  `-SkipAI` 控制。
- GPX/FIT 导入状态按「解析后绝对路径 + mtime_ns + size」记在 `state/gpx-imported.json` /
  `fit-imported.json`；watch-publisher 按天内容 sha1 去重；翻译缓存按原文哈希。

### LLM 成本

- **`generate_journal.py` 的 `SYSTEM_PROMPT` 故意写得长且完全静态**，靠 DeepSeek 的前缀
  缓存把输入价压到约 1/10–1/50。**把日期拼进 `system`、或随手改它，都会让当天缓存失效、
  输入成本翻十倍以上。**
- 未知模型不在 `config.pricing` 里 → **按 0 计费并只打一条 warn**，预算控制静默失效。
  加模型必须同时加价格。
- 三层防护里只有厂商侧的硬上限可靠：`cost_guard gate` 和 `common.LLM._guard` 都是本地估算。
  API key 本身只能查余额；登录态 `DEEPSEEK_USER_TOKEN` 才能拿到与官网一致的精确按天账单
  （`collect_ai_usage.py`），也能统计 Claude Code 的消耗。它是登录凭证，只放环境变量。
- 补跑历史某天花的是**今天的预算**（台账时间戳取 `datetime.now()`）。补 5 天就是 5 倍，
  先 `cost_guard.py status`。
- DeepSeek 有峰谷价（工作日 09–12、14–18 为 peak，其余半价）；成本大头是输出 token，
  日记写多长几乎就决定花多少钱。
- 发布翻译也花钱：`life_site.translate_max_calls`（默认 80），实时 NOW 单轮最多 6 条。
- `max_llm_calls_per_run` 是防 agent 跑飞的闸门，别调高到无意义。

### 分类规则

- 手写规则在 `scripts/normalize.py` 的 `APP_RULES` / `TITLE_HINTS`：**有序、小写子串匹配、
  先命中先用** —— 具体的规则必须排在前面。
- 但「分类」已经扩散到别处：`app_map.py` / `state/app_map.json`（学习映射）、
  `publish_life_site.py` 的七维与手机应用集合、`learn_apps.py`、`structure_text.py`
  各有硬编码分类表。**分类名至少六处要同步**：`config.yaml` 的 `categories`、
  `normalize.py`、`generate_journal.py` 的 `SYSTEM_PROMPT`、`vault/99-Templates/Panel-*.md`
  的 `CATEGORIES`、`learn_apps.py`、`structure_text.py`。
- 浏览器按 `title` 分类，不按 app 名。2026-10-24 起，没命中强信号 → `("Study","rule")`
  （看资料）；GitHub/Notion → Work，视频/社交 → Leisure，购物/账单 → Life。
  代价是「随便逛新闻/论坛」也算 Study，这是有意接受的。
- 时间块 `< MIN_BLOCK`（**2 分钟**）不会单独成块，`fold_short_blocks` 把它**并入相邻块**
  （不是丢弃）；`MERGE_GAP` 是 5 分钟。
- 跨应用短切片只贡献时长，**不把别的应用的标题 / 分类写进当前块**；同一应用的短切片才带标题。
  否则 PUBG 会被隔壁的 Code/msedge 切片带成 Work。
- `normalize.py` 里 URL 附着是**按时间最大重叠**匹配的（窗口桶和浏览器桶独立采样，差几秒
  很正常）。**不要改成「切片起点必须被覆盖」** —— 那样会导致所有 URL 静默附着失败。
- 手机上的对话式 AI（DeepSeek / ChatGPT / Claude / Gemini / Copilot / Perplexity）并入
  Study；PUBG / TslGame → Leisure；Antigravity / Cursor 等编辑器助手 → Work。

### 发布 / 隐私边界

- `/life` 聚合：城市级位置、分类时长、步数/心率、手机计数、AI 花费，**不含坐标、轨迹、
  窗口标题、URL、包名、余额**。
- `/life/archive` 原始文档：**完整保留**窗口标题、URL、坐标、日记、问答、`meta`。
  唯一会改的是凭证：URL 里 `token`/`state`/`code`/`part` 等 query 值、URL userinfo、
  `sk-`/`gh[pousr]_`/`github_pat_`/`xox`/`AKIA`、`Bearer` 头、`password=`、敏感 dict 键值
  替换成 `REDACTED`。**这是防凭证泄露，不是生活数据脱敏。**
- `/api/life/now` **故意含当前坐标**，和 `/api/life` 的边界不同。
- 翻译只改**发布副本**，本机原文不动。发布失败时保留线上已有的英文版，不发布半截中文。
- 时区：`publish_life_raw` 按天分片、`watch-publisher` 的 `WATCH_TZ`、站点展示都必须是
  `Asia/Shanghai`，不一致会让同一次活动落到两天。

### 数据与凭证

- `vault/` **被 .gitignore 排除且必须保持排除** —— 里面有定位坐标和日记。`.gitignore` 里
  `vault/` 那一行必须顶格，曾经因为写在注释里导致 22 个含定位的文件差点推到公开仓库。
- API key **一律放用户级环境变量**（`setx DEEPSEEK_API_KEY "sk-..."`）或不入库的 env 文件：
  `LIFE_INGEST_TOKEN`、`DEEPSEEK_USER_TOKEN`、`FEISHU_APP_SECRET`、中继 key 都不进仓库。
  `config/config.yaml`、`config/timetable.yaml` 本身也被 ignore。
- 备份清单：`state/events.db`（WAL，要连 `-wal`/`-shm`）、`state/cost_ledger.jsonl`、
  `config/config.yaml`、vault（单独同步）；服务器 `/var/lib/lifelogging/`
  （手机事件 / 问答是唯一副本）和 `/etc/lifelogging/*.env`。
- `state/` 下可重建的：`timeline-*`、`features.jsonl`、`dataset.jsonl`、`graph.json`、
  `weeks/`、`translation_cache.json`。必须备份的：`events.db`、`cost_ledger.jsonl`、
  `last_processed.json`，以及 `state/gpx|fit/` 里还没导入 / 已导入的原始文件
  （中继那边通常还有一份，但不要假设它永远在）。

### 服务器部署

- `deploy.sh` 用 `git merge --ff-only`；工作区有被改过的**被跟踪**文件时拒绝合并（该由人看）。
  它**不装依赖、不复制 unit**；close-day / live-questions / watch-publish 是 oneshot，
  不用重启；中继和飞书常驻必须重启。
- 加了 `lark-oapi` 之类的依赖而没 `pip install`，飞书会 `Restart=always` **无限 ImportError
  重启**，`systemctl status` 还显示 active (running)，看着像一切正常。
- 服务器自己的 `config/config.yaml` 和笔记本那份**不一样**：`vault_path`（服务器没有 vault）、
  `state_dir`（指到 `/var/lib/lifelogging`）、`sources.phone_relay.api_base`。git pull 不会碰它。
- 中继 unit 的 `RELAY_HOST` 要改成 Tailscale 地址手机才连得上，**别写 `0.0.0.0`**。

### 飞书机器人

- 用**文字题**，不用卡片按钮（卡片发不出去时会退回纯文本）。答案协议 `<qid>|yes|note`。
- 一次只推一题，答完才推下一题；实时题不勾任务；收尾后封盘。
- 凌晨收尾按 `FEISHU_DAY_ROLL_HOUR=5` 归到前一天；打卡题 id 必须和收尾题不同。
- 它挂了是**静默的**：进程活着、状态 active，只是长连接死了。查
  `journalctl -u lifelogging-feishu-bot`。

---

## 外部依赖的坑（实测踩过的）

- **Dawarich**：`latitude`/`longitude` 在 API 里是**字符串**（`"39.9042"`），必须 `float()`；
  `visits` 是从原始点**推导**出来的，点位少时会为空 —— `collect_phone.py` 会同时读
  `/points` 兜底。`/api/v1/visits` 返回**裸 JSON 数组**，不是 `{"visits": [...]}`。部署坑
  （`TIME_ZONE` 默认欧洲/伦敦、`SECRET_KEY_BASE` 默认 `CHANGE_ME`、nginx 抢占 Tailscale 的
  443）见 `docker/dawarich/README.md`。
- **Gadgetbridge**：没有 REST API，只能读导出 SQLite，且**表名随设备型号变化**。
  `ingest_gadgetbridge.py` 按**列名**而非表名匹配，第一次接入**必须先用 `--dry-run`**
  看它选中了哪张表。
- **Grafana** 直接挂 `state/events.db`，挂载点必须是**读写**（WAL 要写 `-wal`/`-shm`），
  只读会 `database is locked`。
- **Cloudflare Tunnel**：Android 同时只能开一个 VPN；手机改走 Tunnel 后 Tasker 的每个
  HTTP action 都要带 Access Service Token 头。
- **screenpipe** 是 source-available（自用免费，不能上服务器、不能商用），且表结构跨版本
  变动很大；代码全部走「探测列名」，表不存在就跳过。
- **LiteLLM** 的 `max_budget` 在没有 Postgres 时是 **fail-open（静默放行超额）**。
- **Windows 上的 MCP**：npm 系的必须写 `npx.cmd` 全路径（裸 `npx` 大面积连不上），
  路径要双反斜杠，改完配置要以管理员重启客户端。

---

## 更深入的文档

- `docs/工具核实报告.md` —— 先读这个，里面若干条会推翻直觉判断
- `docs/实现文档.md` —— 数据字典、blocks schema、发布契约（最后更新 10-14，部分流程已过时）
- `docs/分阶段落地.md` —— 分阶段实施步骤与当前进度
- `docs/运维手册.md` —— 排障、备份清单、隐私开关、每日任务
- `docs/扩展路线图.md` —— 计划中的扩展（其中「AI 用量统计」已由 `collect_ai_usage.py` 实现）
- `docs/手机自动化-OpenTasker.md` —— 手机侧 Tasker 配置
- `docs/lls-dataflow.mmd` + `render_dataflow.py` —— 数据流图源文件
- `server/*/README.md`、`docker/grafana/README.md`、`windows/live-daemon/README.md`
- `README.md` —— 总览与「已验证到什么程度」；注意里面 smoke 数字（8/8）已过时，
  以本文件 64 项为准。`docs/实现文档.md`（20/20）同理。
