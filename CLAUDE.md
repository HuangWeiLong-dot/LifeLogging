# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## 这是什么

个人全量生命记录系统（Lifelogging）：把电脑 / 手机 / 可穿戴的可记录数据自动采集 → AI 理解 → 结构化日记 → 写入 Obsidian。

**两条不可动摇的设计原则**：

1. **数据全部本地 / 自托管**，不依赖第三方云。整个系统里唯一离开本机的数据是发给 LLM 的日记摘要（`ai.provider: ollama` 可消除这最后一条）。
2. **不录屏、不录音、不记键盘**。电脑端的全部依据是 ActivityWatch 采集的**窗口标题**，外加可选浏览器扩展的 URL 与页面标题。

**语言约定**：代码、注释、README、docs、本文件 —— 中文。`vault/05-Journal` 与 `vault/06-AI-Sessions` 生成的记录 —— 英文。分类名（`Work`/`Study`/…）一律英文。

---

## 常用命令

```bash
# ── 环境 ────────────────────────────────────────────────
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml    # 然后填 vault 路径 / API key / 服务器地址

# ── 验证（不碰任何采集源，改完代码先跑这个）────────────────
.venv/Scripts/python scripts/smoke_test.py           # 11 项检查，无参数、无过滤选项，全绿才算过
powershell -NoProfile -File scripts\check_ps1.ps1    # .ps1 语法 + 编码检查 —— 改过 .ps1 后必跑

# ── 常驻服务 ────────────────────────────────────────────
python -m middleware.events_api                      # 统一事件表，127.0.0.1:8787

# ── 单日流水线（按顺序）──────────────────────────────────
python scripts/collect_desktop.py --date today
python scripts/collect_phone.py   --date today
python -m middleware.ingest_gadgetbridge --date today [--db PATH] [--dry-run]
python scripts/normalize.py       --date today       # → state/timeline-<day>.{json,md} + blocks 表
python scripts/generate_journal.py --date today [--dry-run]   # --dry-run 只打印，但照样花钱

# ── 整条流水线（计划任务用的就是这个）────────────────────
powershell -ExecutionPolicy Bypass -File scripts\run_daily.ps1
powershell -ExecutionPolicy Bypass -File scripts\run_daily.ps1 -Date 2026-10-12   # 手动补某天
#   其它参数：-SkipAI  -DryRun  -NightlyHour 23  -MaxBackfillDays 14

# ── 成本 ────────────────────────────────────────────────
python scripts/cost_guard.py status                  # 退出码 0/1/2 = 绿/黄/红
python scripts/cost_guard.py gate                    # 超预算则退出码 1
python scripts/cost_guard.py report --days 7
python scripts/cost_guard.py check-deepseek          # 余额 + 按 7 日均值推算可用天数
python scripts/reconcile_cost.py --days 7 [--write-vault]

# ── 其它 ────────────────────────────────────────────────
python scripts/geo.py [lat lon]                      # 离线逆地理编码自测
powershell -File scripts\setup_scheduler.ps1         # 注册计划任务 LifeLogging-Daily
```

**没有单元测试框架**，`scripts/smoke_test.py` 就是测试套件，且不支持只跑其中一项。改了 `normalize.py` / `common.py` / `events_api.py` 的核心逻辑后，正确的验证方式就是跑它。

---

## 架构

三层，数据单向流动：

```
采集层 ──→ 统一事件表 ──→ 理解层 ──→ 存储层
```

### 1. 采集层 → 统一事件表（`state/events.db`）

所有采集源都必须归一成**同一张长表** `events`，写入**只走 HTTP**（`EventsClient`，`X-API-Key` 鉴权），没有任何脚本直接写这个 SQLite。这是新增数据源的唯一扩展点：**新增一种数据 = 新增一个 `domain` 值，不改表结构**。

| domain | 来源脚本 | 内容 |
|---|---|---|
| `screen` | `collect_desktop.py` | ActivityWatch 窗口/AFK/浏览器，**按 app 聚合的当日汇总**（秒数） |
| `location` | `collect_phone.py` | Dawarich 停留点 / 轨迹 |
| `health` | `ingest_gadgetbridge.py` | 手环步数 / 睡眠 / 心率 |
| `web` / `phone` / `manual` / `ai` | 浏览器扩展 / Tasker / 手工 / 成本对账 | — |

### 2. 理解层

- **`normalize.py` —— 确定性规则引擎**（不花钱）。读 ActivityWatch 的**原始切片**（不是上面那份聚合）+ 事件表其它 domain，合并成时间块，写 `state/timeline-<day>.json` 和 `.md`，并把结果 POST 进 `blocks` 表（可视化真正用的那一层）。规则覆盖不到的才标 `ambiguous` 交给 AI。
- **`generate_journal.py` —— 每晚一次 LLM 调用**（一天只有这一次）。读 `timeline-<day>.json` → 生成 `vault/05-Journal/<day>.md` + 追写 `vault/06-AI-Sessions/<day>.md`，最后调 `write_notes.write_all()` 把各 domain 的数据铺进 vault 对应目录。

> **「当天数据」有两个不同的东西，别混**：`events` 表里是采集器给的**按 app/站点聚合的日汇总**；`timeline-<day>.json` 里是 normalize 把**原始切片切分合并后的时间块**。两者格式和用途都不同。

### 3. 存储层

`vault/`（Obsidian 库，被 .gitignore 排除）。谁写哪个目录：

| 路径 | 写入者 | 说明 |
|---|---|---|
| `01-Location/<day>.md` | `write_notes` | 有 visits **或** 只有轨迹点都会写 |
| `02-Phone/<day>.md` | `write_notes` | 按 metric 计数 |
| `03-Time-Log/<day>.md` | `write_notes` | **规则引擎原始输出**，不经 AI —— 与 05-Journal 的差异就是调规则/提示词的信号 |
| `04-Tasks/Tasks.md` | `write_notes` | 单文件滚动，不是按天 |
| `05-Journal/<day>.md` | `generate_journal` | AI 叙述 |
| `06-AI-Sessions/<day>.md` | `generate_journal` | 每次调用的成本/token 流水 |
| `07-Reviews/<ISO年>-W<周>.md` | `write_notes` | **仅周日**生成，纯 Dataview 聚合，不再调 LLM |
| `07-Reviews/Cost-Ledger.md` | `reconcile_cost --write-vault` | |

`state/` 下：`events.db`（WAL，需连 `-wal`/`-shm` 一起备份）、`cost_ledger.jsonl`（hash 链台账）、`timeline-*`（可重建）、`last_processed.json`（水位线）、`logs/`（保留 30 天）。

---

## 必须知道的约束（改动前先读这一节）

### Windows / 编码

- **所有 `.ps1` 必须是 UTF-8 *带 BOM*。** Windows PowerShell 5.1 会把无 BOM 文件按 GBK 解码，中文字节错位吃掉后面的 ASCII 大括号，报出指向**无关行号**的 `Missing closing '}'` —— 看起来像语法错误，其实是编码问题。编辑器、VS Code 默认值、Git 工具都会去掉 BOM。**改过 `.ps1` 后务必跑 `scripts\check_ps1.ps1` 确认。**
- `requirements.txt` 里的 `tzdata` 在 Windows 上是**必须的**：系统不带时区库，缺了 `ZoneInfo("Asia/Shanghai")` 直接抛异常，整套系统起不来。
- 控制台输出编码由 `scripts/common.py` 在导入时统一改成 UTF-8；`run_daily.ps1` 里同时设了 `PYTHONIOENCODING` 和 `[Console]::OutputEncoding`，**两边都要设**（一边写 UTF-8、一边按 GBK 读，日志里的 `→` `✓` 会变乱码）。
- `.gitattributes` 强制 `.sh`/`Dockerfile` 存 LF、`.ps1` 存 CRLF 并显式写死，不依赖各人的 `core.autocrlf`。别去动它。

### LLM 成本

- **`generate_journal.py` 的 `SYSTEM_PROMPT` 故意写得长且完全静态**，靠 DeepSeek/Anthropic 的前缀缓存把输入价压到约 1/10–1/50。**把日期拼进 `system`（而不是 `user`）、或随手改它，都会让当天缓存失效、输入成本翻十倍以上。**
- 未知模型不在 `config.pricing` 里 → **按 0 计费并只打一条 warn**，预算控制会静默失效。加模型必须同时加价格。
- 三层防护里只有厂商侧的硬上限是真可靠的：`cost_guard gate`（流水线开头的预检）和 `common.LLM._guard`（调用前的 `BudgetExceeded`）都是本地估算。DeepSeek **没有用量/账单 API**，只有余额。
- 补跑历史某天花的是**今天的预算**（台账时间戳取 `datetime.now()`）。补 5 天就是 5 倍，先 `cost_guard.py status`。
- `max_llm_calls_per_run` 是防 agent 跑飞的闸门，别调高到无意义。

### 幂等 / 重跑

- 采集器在写入前会**先删掉当天该 source 的数据**（`DELETE /day?day=&source=`）；`POST /blocks` 也是先删当天再插。所以脚本可以随意重跑。
- `events` 表有唯一索引 `(source, domain, metric, ts_utc, COALESCE(value_text,''))`，`blocks` 表有 `(day_local, start_utc, category)`，靠 `INSERT OR IGNORE` 兜底。
- 时间：`ts_utc` 是 ISO8601 UTC 带 `Z`；`day_local` 是**服务端按 `config.timezone` 算出的本地日期分区键**。一个本地日会跨两个 UTC 日，查数据一律用 `common.Day` 拿 UTC 区间，不要手搓。

### 分类与规则

- **分类名有四处必须同步**，改一处要改四处：`config.yaml` 的 `categories`、`normalize.py` 的 `APP_RULES`/`TITLE_HINTS`、`generate_journal.py` 的 `SYSTEM_PROMPT`、`vault/99-Templates/Panel-*.md` 里的 `CATEGORIES` 数组。
- `APP_RULES` 是**有序的、小写子串匹配、先命中先用** —— 具体的规则必须排在前面。
- **浏览器按 `title` 分类，不按 app 名**。浏览器没有 title 命中 → `("Other", "ambiguous")` 交给 AI，**不会**落到 default（否则 `unmatched_apps` 会被浏览器刷屏）。
- 时间块 `< MIN_BLOCK`(6 分钟) 不会被写进任何地方，`MERGE_GAP` 是 5 分钟。Grafana 上早期看到 blocks 很少是正常的。
- `normalize.py` 里 URL 附着是**按时间最大重叠**匹配的（窗口桶和浏览器桶独立采样，差几秒很正常）。**不要改成「切片起点必须被覆盖」** —— 那样会导致所有 URL 静默附着失败。

### 其它

- `generate_journal.py` 会**保留并原样回填用户手写的 `## Notes` 小节**，其它内容每天覆盖。不要动这个逻辑。
- `run_daily.ps1` 用**水位线**（`state/last_processed.json`）决定处理哪几天，不是 `today`。手动 `-Date` 运行**不会**推进水位线（避免补跑时误标）。补跑历史天要手动指定。
- 流水线里**除预算闸门外，任何一步失败都不中断后续步骤**（采不到位置不该导致日记写不出来）。这是有意的。
- `run_daily.ps1` 会自己拉起事件表服务（读 config 拿端口 —— **用 Python 按字段名读，别用 `Select-String` 抓第一条**，`activitywatch` 的 5600 排在 `events_api` 前面）。
- `vault/` **被 .gitignore 排除且必须保持排除** —— 里面有定位坐标和日记。这是一个纯代码仓库，vault 同步走 Syncthing 或自建 remote。`.gitignore` 里 `vault/` 那一行必须顶格，曾经因为写在注释里导致 22 个含定位的文件差点推到公开仓库。
- API key **一律放用户级环境变量**（`setx DEEPSEEK_API_KEY "sk-..."`），不进配置文件、不进 vault。`config/config.yaml` 本身也被 ignore。
- 第三方工具的性质已核实过在 `docs/工具核实报告.md`：**screenpipe 是 Commercial License（自用免费，不能上服务器、不能商用）**；LiteLLM 的 `max_budget` 在没有 Postgres 时是 **fail-open（静默放行超额）**。

---

## 外部依赖的坑（实测踩过的）

- **Dawarich**：`latitude`/`longitude` 在 API 里是**字符串**（`"39.9042"`），必须 `float()`；`visits` 是从原始点**推导**出来的，点位少时会为空 —— 所以 `collect_phone.py` 会同时读 `/points` 兜底。`/api/v1/visits` 返回的是**裸 JSON 数组**，不是 `{"visits": [...]}`。部署坑（`TIME_ZONE` 默认欧洲/伦敦、`SECRET_KEY_BASE` 默认 `CHANGE_ME`、nginx 抢占 Tailscale 的 443）见 `docker/dawarich/README.md`。
- **Gadgetbridge**：没有 REST API，只能读导出 SQLite，且**表名随设备型号变化**。`ingest_gadgetbridge.py` 按**列名**而非表名匹配，第一次接入**必须先用 `--dry-run` 看它选中了哪张表**。
- **Grafana** 直接挂 `state/events.db`，挂载点必须是**读写**（WAL 要写 `-wal`/`-shm`），只读会 `database is locked`。
- **Windows 上的 MCP**：npm 系的必须写 `npx.cmd` 全路径（裸 `npx` 大面积连不上），路径要双反斜杠，改完配置要以管理员重启客户端。

---

## 更深入的文档

- `docs/工具核实报告.md` —— 先读这个，里面若干条会推翻直觉判断
- `docs/分阶段落地.md` —— 分阶段实施步骤与当前进度
- `docs/运维手册.md` —— 排障、备份清单、隐私开关、维护节奏
- `docs/扩展路线图.md` —— 计划中的扩展（GitHub 活动、手机使用频率、支出记录、AI 用量统计）
- `README.md` —— 目录说明与「已验证到什么程度」清单
