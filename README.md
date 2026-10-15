# LifeLogging — 个人全量生命记录系统

把电脑、手机、日常生活的可记录数据自动采集 → AI 理解 → 结构化日记 → 写入 Obsidian。

**设计原则**：数据全部本地 / 自托管，不依赖第三方云；每个模块都能单独落地，不搞大爆炸式上线。

---

## 架构

```
┌─ 采集层 ────────────────────────────────────────────────┐
│ Win11 本机    ActivityWatch（窗口标题 / AFK / 浏览器 URL）  │
│ Android 手机  OwnTracks/GPSLogger → Dawarich（位置/停留/交通）│
│ 手环手表      Gadgetbridge ──┐                            │
│ 安卓健康      Health Connect ─┼→ middleware（统一事件表）   │
│ 一键记录      Tasker / 快捷方式 ┘                          │
└──────────────────────────┬──────────────────────────────┘
                           ↓
┌─ 理解层 ────────────────────────────────────────────────┐
│ 每晚 23:00  AI Agent（Anthropic API）                     │
│   拉取当天原始数据 → 柳比歇夫分类 → 生成 Markdown 日记       │
│   cost_guard 预检预算 → 记账 → 与厂商账单对账               │
└──────────────────────────┬──────────────────────────────┘
                           ↓
┌─ 存储层 ────────────────────────────────────────────────┐
│ vault/  Obsidian 库（7 个分区 + Dataview 面板）            │
└─────────────────────────────────────────────────────────┘
```

> **本方案不含屏幕/麦克风录制。** 电脑端的全部依据是 ActivityWatch 采集的
> **窗口标题**（如 `main.py - LifeLogging - Code`、`Pull request #12 · GitHub`），
> 它已经能支撑分类判断和「这段时间在干嘛」的描述。
> 换取的是零录制、零隐私负担。详见 `docs/分阶段落地.md` 阶段 1。

**双机分工**：Win11 负责有 GUI 的采集（ActivityWatch 必须在本机跑）；Linux 服务器负责常驻服务（Dawarich、后续的 events API、LiteLLM）。两边通过 Tailscale 之类组内网。

### 数据不止是 Markdown

Obsidian 里的是**展示层**。底下是一份标准的结构化存储，任何 BI 工具都能直接读：

| 文件 | 内容 |
|---|---|
| `state/events.db` → `events` 表 | 原始事实（长表：ts / domain / metric / value / source） |
| `state/events.db` → `blocks` 表 | **分类后的时间块** —— 可视化真正要用的那一层 |
| `state/cost_ledger.jsonl` | 每次 LLM 调用（hash 链防篡改） |

`docker/grafana/` 里有一套现成的 Grafana 配置（**6 个仪表盘**：概览 / 时间与分类 /
位置（含地图） / 手机使用 / 健康与运动 / **AI 用量**），直接挂载这个 SQLite 文件，
不复制不导出。见该目录的 README —— 里面记了几个「不报错但显示不出来」的坑。

⚠️ 其中「AI 用量」那页解决了一个真实误会：`cost_ledger.jsonl` 只记**本流水线自己**
的调用（一天约 ¥0.02），而你白天用 Claude Code 花掉的钱走的是同一把 key、同一个余额，
却完全不进台账 —— 实测两者差 **100 倍**。那页的面板以 DeepSeek 平台的用量接口为准。

---

## 目录说明

```
LifeLogging/
├── README.md                    ← 你在这
├── 更新数据.cmd                  ← 双击：立刻拉取今天的数据（不调 AI）
├── config/
│   └── config.example.yaml      ← 全部可调参数集中在这，改这里不用改代码
├── docs/
│   ├── 实现文档.md                ← ★ 系统现在有什么、记录了哪些数据
│   ├── 工具核实报告.md            ← ⚠️ 先读这个：哪些工具名与现实有出入
│   ├── 分阶段落地.md              ← 最小可行 / 完整 两套方案对照
│   ├── 扩展路线图.md              ← 以后想加的数据源
│   └── 运维手册.md                ← 排障、备份、隐私开关
├── vault/                       ← Obsidian 库（用 Obsidian 打开这个目录）
│   ├── 01-Location/
│   ├── 02-Phone/
│   ├── 03-Time-Log/
│   ├── 04-Tasks/
│   ├── 05-Journal/
│   ├── 06-AI-Sessions/
│   ├── 07-Reviews/
│   └── 99-Templates/                  ← 日记模板 + 4 个 Dataview 面板
├── middleware/
│   ├── events_api.py            ← 统一事件表（FastAPI + SQLite）
│   └── ingest_gadgetbridge.py   ← 手环 SQLite → 事件表（补 API 缺口）
├── scripts/
│   ├── smoke_test.py            ← 冒烟测试：不碰采集源，验证代码与配置
│   ├── collect_desktop.py       ← ActivityWatch → 事件表（无录屏录音）
│   ├── collect_phone.py         ← Dawarich → 事件表（手机位置）
│   ├── collect_phone_events.py  ← 服务器中继 → 事件表（手机使用：解锁/切应用）
│   ├── normalize.py             ← 原始数据 → 柳比歇夫分类
│   ├── generate_journal.py      ← 夜间 AI 生成日记
│   ├── cost_guard.py            ← 预算硬封顶 + 每日花费写进事件表
│   ├── collect_ai_usage.py      ← AI 真实花费：平台账单 + 余额 + Claude Code 分布
│   ├── reconcile_cost.py        ← 与厂商账单对账
│   ├── run_daily.ps1            ← 每日流水线（供计划任务调用）
│   ├── setup_scheduler.ps1      ← 注册 Windows 计划任务
│   └── check_ps1.ps1            ← .ps1 语法预检（改完脚本先跑它）
├── server/
│   └── phone-relay/             ← Linux 服务器：手机使用事件中继（单文件，无依赖）
├── docker/
│   ├── dawarich/                ← Linux 服务器：位置后端
│   ├── grafana/                 ← 可视化：6 个仪表盘，直接读 state/events.db
│   └── litellm/                 ← 可选：AI 网关（硬预算用）
└── mcp/
    └── claude_desktop_config.example.json   ← MCP 服务器配置
```

---

## 快速开始（最小可行，约 1 小时）

```bash
cd C:/path/to/LifeLogging
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt

cp config/config.example.yaml config/config.yaml
# 编辑 config.yaml：填 vault 路径、API key

python scripts/smoke_test.py                       # ← 先跑这个，不依赖任何采集源
```

冒烟测试全绿之后，再逐模块接真实数据：

```bash
python -m middleware.events_api                    # 另开一个窗口常驻
python scripts/collect_desktop.py --date today     # 采集
python scripts/normalize.py --date today           # 归类
python scripts/generate_journal.py --date today    # 生成日记
```

完整分阶段步骤见 `docs/分阶段落地.md`。

### 想随时刷新数据？双击 `更新数据.cmd`

流水线固定在 23:00 跑，所以白天打开 Grafana，**当天的数不全** ——
尤其是「今日应用时长 / 今日浏览器」（要 `collect_desktop.py` 跑过才有）
和「今日时间黑洞」（要等 AI 写完日记才回写）。

仓库根目录的 `更新数据.cmd` 双击即可：它对**今天**跑一遍
「采集 → 分类 → 写副笔记」，**不调 AI**（日记留给 23:00 那次，省那 ¥0.02）。
一两分钟出结果，跑完刷新 <http://localhost:3001> 就能看到。

它本质就是 `scripts\run_daily.ps1 -Date today -SkipAI` —— 手动模式不推进水位线，
所以晚上 23:00 的正式运行照样会完整处理这一天。

> 想连日记一起生成，把 `.cmd` 里的 `-SkipAI` 删掉即可。

---

## 已验证到什么程度

代码是实际跑过、不是"看起来对"：

- `scripts/smoke_test.py` 8/8 通过（配置、时区换算、事件表读写与幂等去重、
  成本计算、台账、AI 输出契约解析、规则表、库结构）
- 事件表服务实际起过，HTTP 层面验证：鉴权 401/200、批量写入、
  幂等去重（重复上报返回 `inserted: 0`）、聚合查询、多源查询
- 采集脚本在数据源缺席时优雅降级（打印可操作的告警，不抛异常）

**后来在真实环境里验证过的**：

- **Dawarich 连通性 + 鉴权** —— `collect_phone.py` 实测跑通，`Authorization: Bearer <api_key>`
  可用。`/api/v1/visits` 返回的是**裸 JSON 数组**（不是 `{"visits": [...]}`），
  代码里的容错解析正好覆盖了两种情况
- **Dawarich 轨迹点摄入 + 读取** —— 实测一次 POST 成功入库，`/api/v1/points` 也能读回。
  ⚠️ 实测发现的两个坑：`latitude` / `longitude` 是**字符串**（`"39.9042"`）不是数字，
  必须 `float()` 转换；`visits` 是从原始点**推导**出来的，点位太少时会是空的 ——
  所以 `collect_phone.py` 现在会同时读 `/points` 做兜底
- **AI 日记生成** —— DeepSeek 实测通过，自动前缀缓存生效（命中 1152/1324 输入 token）。
  单次日记 **¥0.0023–0.0642，平均 ¥0.0206**，其中 **97% 花在输出 token 上**：
  成本几乎只跟「正文写多长」有关，输入那点钱被自动缓存压到了 3%。
  ⚠️ 早期的文档写「一次约 ¥0.003」—— 那是第一次测试（输出 392 token）的数字，
  后来正文变长到平均 3700 token，涨了约 10 倍。这个数**随输出长度变**，不是常数。
- **Tailscale Serve** —— 服务器部署实测（含 snap 沙箱、端口冲突等 5 个坑，见 `docker/dawarich/README.md`）

**仍然没有验证到的部分**：

- **Dawarich 的 visit 元素内部字段名** —— 连通了但还没有数据，看不到
  `started_at` / `place` / `latitude` 的实际形状。代码用容错解析（多候选字段名 + 缺字段跳过），
  但没在真数据上跑过。等手机上传第一个点后再确认
- Gadgetbridge 导出的 SQLite —— 表名随手环型号变化，**第一次必须用 `--dry-run` 看它选中了哪张表**

过程中实测修掉的 7 个真实缺陷（1、2、5、6 是 Windows 特有的，3、4、7 是跨平台的逻辑缺陷）：
1. `tzdata` 缺失 → Windows 无系统时区库，`Asia/Shanghai` 解析失败，整套系统起不来
2. 控制台 GBK 编码 → 脚本里的 emoji 抛 `UnicodeEncodeError`
3. `CostLedger.spend_on` 用 `ts_utc` 反推日期而非用写入方记录的 `day` 字段
4. `events_api.py` 的 `Query(None, alias=...)` 老写法 → 直接调用函数时默认值是 `Query` 对象而非 `None`
5. **`.ps1` 无 BOM → Windows PowerShell 5.1 按 GBK 解码 UTF-8，中文字节错位吃掉大括号，
   报出指向无关行号的 `Missing closing '}'`**。三个 `.ps1` 已全部改为 UTF-8 **with BOM**。
   ⚠️ 用编辑器改这些脚本后，务必确认 BOM 还在 —— 否则又会坏掉。
   改完先跑 `powershell -NoProfile -File scripts\check_ps1.ps1` 验证。
6. **事件表端口被系统偷走** → 8787 落在 Windows 的 TCP 保留段里（Hyper-V / WSL 每次开机
   重新划，`netsh int ipv4 show excludedportrange protocol=tcp` 可查）。服务 bind 报
   `WinError 10013`，但它是被隐藏窗口拉起的，报错直接被吞，日志里只剩一句
   「did not come up within 12s」，看不出真因。现在 uvicorn 的 stderr 会落到
   `state/logs/events_api.err.log`，端口改到动态池外的 **18787**。
7. **流水线报成功，而库里一行都没有** → 采集脚本把「连不上事件表」当降级处理、照样
   `exit 0`（对「手表没戴」是对的，对「落盘的地方没了」是错的），而 `Invoke-Step` 只看退出码。
   更隐蔽的是 `normalize` 和 `journal` 都从本地 JSON 读，所以整条流水线照常走完、
   印出 `Done -- 1 day(s) succeeded` → **水位线前进，这一天再也不会被重跑**。
   2026-10-14 就这样被标成「已完成」过一次（它的 timeline 和日记也随之被基于降级数据重写）。
   现在事件表起不来 → 直接 `exit 1`，不碰水位线。


---

## 当前待定项

以下三项不定死，代码就没法完全定稿。**已全部做成配置项**，答案填进 `config/config.yaml` 即可，不用改代码：

1. **Apple Health 与 Android 都勾选** —— 是一台 Android 手机，还是两台手机？纯 Android 的话 `sources.apple_health` 直接设 `enabled: false`，iOS 那条链路整块删掉。
2. **Linux 服务器**：发行版 / 有无公网或域名 / 磁盘容量。影响 `docker/dawarich/` 里的域名与反代配置，以及手机在外网能否直连上报（不能的话走 Tailscale）。
3. **AI 接入方式**：已设为 `deepseek`（deepseek-flash）。单次日记平均
   **¥0.02**（¥0.002–0.064，看正文多长），**97% 花在输出 token 上**；
   自动前缀缓存已验证生效，把输入那部分压到 3%。按每天一篇算月化 **约 ¥0.6**。
   ⚠️ 面板上的「今日 AI 花费」比这个大 —— 它统计**当天全部调用**，包含调试重跑。
   备用 provider 支持 `anthropic` / `claude_code` / `ollama`，
   改 `config.yaml` 的 `ai.provider` 即可切换。

---

## 下一步

- 先读 `docs/工具核实报告.md` —— 里面有若干条会推翻你原始设想的事实
- 再读 `docs/分阶段落地.md` 决定这一轮做到哪
