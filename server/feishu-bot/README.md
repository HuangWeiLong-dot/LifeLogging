# 飞书机器人（Linux 服务器）

每日提问的**收发通道**。替代原来「Tasker 每 30 分钟轮询 `/ask/pending` →
弹通知 → 浏览器填表」那条链路。

```
飞书 ⇄(长连接，服务端出站)⇄ feishu-bot（本服务）
                                ├─→ 中继 GET /questions/pending   取还没答的题
                                └─→ 中继 POST /answer             把答案交回去
```

**它不重写任何规则。** 答案仍然是 `domain=question / metric=answer /
"<题号>|<答案>|<备注>"` 落进 `phone-<day>.jsonl`，笔记本 23:00 照常拉走、
`scripts/apply_answers.py` 照常勾 `vault/04-Tasks/Tasks.md`。
飞书只是同一根管子上的另一扇门。

---

## 为什么是飞书，不是 QQ

原始需求是「接入 QQ」。查证之后否掉了 —— 以下都是一手来源：

| 方案 | 结论 | 依据 |
|---|---|---|
| QQ 官方机器人 | **做不到主动推送** | 官方文档原文：「主动推送能力于 2025 年 4 月 21 日起不再提供，接口调用时会收到错误信息。」单聊只剩被动回复：用户先发消息，60 分钟内最多回 4 条，且必须带 `msg_id` |
| 官方 · 互动召回 | 额度不够 | `is_wakeup` 只在「当天 / 1-3 天 / 3-7 天 / 7-30 天」各 1 条，**合计 4 条/30 天** —— 约等于每周 1 条，做每日提问差一个量级 |
| 官方 · 频道私信 | 可行但入口怪 | 仍有 2 条/天/人主动额度，但入口在「QQ 频道」不在消息列表，且要自建频道 |
| NapCat / OneBot（个人号） | 能推但风险高 | 社区实测普遍几小时掉一次线；Docker 重建容器＝识别为新设备，直接触发账号级风控；要用养过的小号 |

> **顺带纠正一个容易搞错的前提**：官方 QQ 的代价**不是**公网回调打不过
> Cloudflare Access —— 它支持 WebSocket 出站长连，根本不需要公网入口。
> 真正的代价是**主动推送被下架**。

飞书自建应用：主动推送不限量，长连接由服务端**出站**发起 —— 不需要公网入口、
不需要域名备案，**Cloudflare Access 一行都不用动**。个人版免费。

代价：不在 QQ 里，手机上要多装一个飞书。

---

## 部署

### 第 0 步：飞书后台（无代码，但必须先做）

1. [open.feishu.cn](https://open.feishu.cn) → 开发者后台 → **创建企业自建应用**
   - ⚠️ **先确认你的账号有「组织/租户」**。飞书个人版不一定能建自建应用。
     这是唯一一件代码绕不过去的事 —— 建不出来，后面全都免谈。
2. 记下 **App ID** 和 **App Secret**（凭证与基础信息页）
3. **添加应用能力 → 机器人**
4. **权限管理**，加这两个：
   - `im:message.p2p_msg:readonly` —— 收单聊消息
   - `im:message:send_as_bot` —— 发消息（**主动推送靠的就是它**）
5. **可用范围**：把你自己加进去
6. **发布版本**并让管理员通过
   - ⚠️ 权限和能力在**发布通过之前一律不生效**。不发布就调，错会是很含糊的
     权限错误，看着像代码问题
   - ⚠️ 可用范围不含你的人，主动推送会报 **230013**

### 第 1 步：代码和依赖

```bash
sudo git clone https://github.com/HuangWeiLong-dot/LifeLogging.git /opt/lifelogging/repo
# 已有部署的话就：
sudo /opt/lifelogging/repo/server/deploy.sh

# ⚠️ 这一步**必须手动做**。deploy.sh 只做 git pull + 重启，不装依赖。
#    漏了的话服务会在 Restart=always 下无限 ImportError 重启 —— 而
#    `systemctl status` 会显示 "active (running)"，看着像一切正常。
sudo /opt/lifelogging/venv/bin/pip install -r /opt/lifelogging/repo/requirements.txt
```

### 第 2 步：密钥

```bash
sudo tee /etc/lifelogging/feishu-bot.env >/dev/null <<'EOF'
FEISHU_APP_ID=cli_REPLACE_ME
FEISHU_APP_SECRET=REPLACE_ME
FEISHU_OWNER_OPEN_ID=
EOF
sudo chmod 600 /etc/lifelogging/feishu-bot.env
```

> ⚠️ **占位符必须纯 ASCII**。写成带中文的（比如 `cli_你的appid`）会被原样
> 当成真值用，而报错会是 SDK 内部一个看不懂的编码错。这一条在中继那边
> 实测踩过一次（见 `../README.md`）。

`FEISHU_OWNER_OPEN_ID` **先留空**。第 4 步会告诉你它该填什么。

### 第 3 步：装服务

```bash
sudo cp /opt/lifelogging/repo/server/feishu-bot/lifelogging-feishu-bot.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifelogging-feishu-bot
```

**先别急着看服务状态** —— 第 4 步要先连上长连接，飞书那边才肯让你保存订阅方式。

### 第 4 步：开通长连接（顺序有坑）

```bash
sudo journalctl -u lifelogging-feishu-bot -f
# 期望看到：正在连接飞书长连接… → connected to wss://…
```

**在它连着的时候**，回飞书后台 → 事件与回调 → 事件配置 →
**订阅方式改成「使用长连接接收事件」**。

> ⚠️ **这一步有鸡生蛋**：长连接订阅方式**只有在有客户端在线时才能保存成功**。
> 所以必须先让服务跑起来，再回去存这个设置。反过来做会存不上，
> 而且提示很不明显。
>
> ⚠️ 长连接模式下 `EventDispatcherHandler.builder()` 的**两个参数必须是空串**
> （那是 Webhook 模式用的 `verification_token` / `encrypt_key`）。

然后**从你的手机飞书给机器人发一句话**，在日志里找到：

```
[recv] open_id=ou_xxxxxxxx chat=p2p text='...'
```

把那个 `ou_…` 填回 `/etc/lifelogging/feishu-bot.env` 的 `FEISHU_OWNER_OPEN_ID`，
再 `sudo systemctl restart lifelogging-feishu-bot`。

> 这是拿到自己 open_id 的**零权限**办法：不需要 `contact` 权限，只要有人给
> 机器人发过一句话。另一个办法（按手机号查 `batch_get_id`）要多一个权限，不值。

---

## 验证

> ⚠️ **手工跑这两个自检时，必须先把 env 文件读进来。**
> `sudo -E` 保留的是**你当前 shell** 的环境变量，它**不会**去读
> `/etc/lifelogging/*.env` —— 那是 systemd 启动服务时才做的事。
> 所以直接 `sudo -E python feishu_bot.py --check` 会报「FEISHU_APP_ID 没填」，
> 而那个文件其实是好的。下面用 `set -a; . 文件` 显式读进来。

```bash
# 1. 配置和中继连通性（不连飞书也能查）
sudo sh -c 'set -a; . /etc/lifelogging/feishu-bot.env; . /etc/lifelogging/relay.env; set +a; \
  exec /opt/lifelogging/venv/bin/python \
  /opt/lifelogging/repo/server/feishu-bot/feishu_bot.py --check'

# 2. 主动推送自检 —— 不经过任何事件，专测「主动」这条路
#    （先停服务，免得两边同时连着同一个应用）
sudo systemctl stop lifelogging-feishu-bot
sudo sh -c 'set -a; . /etc/lifelogging/feishu-bot.env; . /etc/lifelogging/relay.env; set +a; \
  exec /opt/lifelogging/venv/bin/python \
  /opt/lifelogging/repo/server/feishu-bot/feishu_bot.py --push-test "自检"'
# 期望：手机飞书上收到「自检」
sudo systemctl start lifelogging-feishu-bot
```

然后从飞书里发一句「?」→ 应该收到当天还没答的题 → 答一条
（二值题回「做了 / 没做」，选择题回数字），再断言**落库的到底是哪个字**：

```bash
sudo grep '"domain": "question"' /var/lib/lifelogging/phone-$(date +%F).jsonl | tail -1
# 期望：…"value_text": "<题号>|yes|"
#       ⚠️ 必须是 yes，不是「做了」—— 见下面第 1 条
```

改完中继想确认新版生效，**别只看 `/health`** —— 新旧版本的 `/health` 长得
一模一样。挑一条只有新版才有的路由：

```bash
KEY=$(sudo grep RELAY_API_KEY /etc/lifelogging/relay.env | cut -d= -f2)
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8788/questions/pending
# 旧版 {"error":"not found"}   新版 {"day":…,"questions":[…]}
```

---

## 升级

```bash
sudo /opt/lifelogging/repo/server/deploy.sh
```

`deploy.sh` 现在认识 `server/feishu-bot/` 了，这块代码一改就会重启本服务。
**但它不装依赖** —— 见第 1 步那条警告。

---

## 问我今天（读服务器上的实时数据）

除了出题，机器人也能随时答当天的数据。**一层统计逻辑都没重写** ——
它调的就是日记用的那两个函数（`normalize.summarize_phone`、
`collect_phone.derive_stays`），所以数字和日记一定对得上。
自己再写一份的话，两份会在某天给出不同的数，而两边都不报错。

| 问法 | 数据来自 | 走什么 |
|---|---|---|
| 我今天去哪了 / 去过哪 | Dawarich（服务器） | 模板 |
| 手机用了多久 / 屏幕时间 | 中继（服务器） | 模板 |
| 今天解锁几次 / 用得最多的应用 | 中继（服务器） | 模板 |
| 我今天在电脑上干了什么 / 现在在忙什么 | 笔记本推的快照（见下） | 模板 |
| 以上都不沾边的其它问题 | —— | LLM（有每日封顶） |

**答不了的会明说，不猜：**

- 「今天的日记写了吗」→ 直说 vault 没推到服务器
- 「今天走了多少步」→ 直说手表数据还没接

### 电脑那份快照（`scripts/push_desktop.py`）

以前对电脑的问题只会回一句「答不了」。那个理由**是对的但不够细** ——
笔记本醒着的时候数据就在手边，睡着之后「最后知道的样子」也还在。

笔记本上一个每 10 分钟的计划任务（`LifeLogging-Desktop`）把「今天到目前」
推给中继（`POST /desktop`，只留最新一份，不留历史）。于是机器人能答：

```
现在在：msedge.exe
  根据样片完成 city.aep - AfterEffects
  分类 Work · 这一段已持续 12 分钟

今天合计 6.4 小时
  Life 2.3h · Work 2.0h · Leisure 1.2h · Other 0.7h · Study 0.3h
  用得最多 msedge 2.1h · AfterEffects 1.2h · Code 0.5h

⚠️ 笔记本最后上报：12 分钟前
```

> ⚠️ **最后那一行是这个功能的核心，不是附注。** 笔记本一天只醒约 3.4 小时，
> 所以「几小时前」是常态。不说出来的话，一份 5 小时前的快照会被读成
> 「你现在正在写代码」—— 那是这个功能最坏的失败方式：安安静静地答错。
> 快照换天了也会明说。

### 自由问答（LLM，有封顶）

模板覆盖不到的问题会交给模型。它拿到的**只有今天的数据摘要**，
提示词里明令「数据里没有的就说没有，不要推测」。三道闸：

1. `FEISHU_FREEFORM_LLM=0` → 整个关掉，纯模板零成本
2. 每天硬上限（`FEISHU_LLM_DAILY_CAP`，默认 10 次），**在调模型之前**拦
3. 每次调用都走 `common.py` 的预算预检 + 写进同一份成本台账
   （否则这笔钱会从 Grafana 的 AI 用量面板上消失）

> ⚠️ **已经能算出来的数字永远走模板**，不交给模型。模板是确定性的：
> 同一个问题问两次答案一样，而且不会编。**模板答不了**才是花钱的理由。
>
> ⚠️ 每次请求都**新建一个 `LLM` 实例**。它的 `_calls` 是按实例计的、且是按
> 「单次流水线运行最多几次」设的；常驻机器人复用同一个实例的话，那个上限
> 迟早撑满，然后**从此再也不回话**，而日志上看不出和调用次数有关。

> ⚠️ 判别的顺序是**有意的：先说不，再说能**。「我今天在电脑上干了什么」里
> 既有「电脑」也有活动词，顺序反了就会一本正经地回一段手机数据 —— 答非所问，
> 而且看着像答对了。

## 实时提问（第二个提问来源）

除了笔记本晚上生成的「问责批」，服务器白天还会自己问几句 ——
`scripts/live_questions.py`，由它**自己的 timer** 每 30 分钟拉一次。

| | 谁生成 | 什么时候 | 问什么 | 存哪 |
|---|---|---|---|---|
| 问责批 | 笔记本（要 vault） | 晚上，问次日 | 昨天日记 / 挂着的事 | `questions-<day>.json` |
| 实时批 | 服务器 | 白天，问当场 | 今天的行踪 / 手机 | `questions-live-<day>.json` |

触发规则（确定性、不上 LLM）：

| 规则 | 条件 | 问出来是什么 |
|---|---|---|
| 长停留 | 某地停留 ≥ 90 分钟 | 「今天 14:05–16:35 你在望京待了 2 小时 30 分，那是在做什么？」 |
| 手机静默 | 相邻两次解锁间隔 ≥ 90 分钟（只算 08:00–22:00） | 「今天 14:00–16:40 手机一次都没解锁，那段时间在干嘛？」 |
| 长时间应用 | 头名应用前台 ≥ 150 分钟 | 「今天 msedge 用了 2 小时 10 分，是整天开着还是真在用？」 |

几条设计上的硬约束：

- **一天最多 3 条。** 实时题是补充不是主力；一天刷十几条会让人把整条通道
  静音，那样问责批也一起收不到了。
- **同一个片段当天只问一次**（记在 `/var/lib/lifelogging/live-asked-<day>.json`）。
  而且**先推中继成功了才落这个记录** —— 反过来的话，一次推送失败会让这条
  规则当天再也不发，服务器上还看不出任何异常。
- **一律是文字题**（`format="text"`、`ref=""`）。`apply_answers` 对文字题
  永远判「没完成」，所以实时题**不会去勾任何任务** —— 它只是问你一句。
- **会过期**：隔了天的实时题不再出现（「你 14:00 在哪」隔三天就是噪音）。
  笔记本那批正相反，它无脑取最新，因为「昨晚生成、今早来取」是常态。
- **夜里不问**（窗口外静默退出，**不算失败** —— timer 夜里那次本来就没事做）。

> ⚠️ **必须和笔记本那批分开存文件。** 中继的 `save_questions()` 是纯覆写、
> `load_latest_questions()` 取「文件名排序最后一份」。实时批写进同一份文件会
> **静默顶掉**手机早上那批题。
>
> ⚠️ 更要命的是文件名：`questions-*.json` 这个通配符**也匹配**
> `questions-live-*.json`，而 '2' 的字典序小于 'l' —— 所以实时文件排序时
> 永远在最后，会被当成「最新那份」，导致**笔记本那批整体消失**。
> 实测踩到过（并集里只剩一条），现在 `load_latest_questions()` 里显式排掉了，
> 冒烟测试 `live questions:` 那一条钉的就是它。

**部署**：两个新 unit，和 `close-day` 一样是 oneshot + timer。

```bash
sudo cp /opt/lifelogging/repo/server/feishu-bot/lifelogging-live-questions.* \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifelogging-live-questions.timer

systemctl list-timers lifelogging-live-questions.timer   # 确认排上了
sudo systemctl start lifelogging-live-questions.service  # 立刻试一次
sudo journalctl -u lifelogging-live-questions -n 40 --no-pager
```

先干跑一次看看它会问什么（不推、不落盘）：

```bash
sudo /opt/lifelogging/venv/bin/python \
     /opt/lifelogging/repo/scripts/live_questions.py --dry-run
```

（这个脚本**不需要** env 文件 —— 中继地址和 key 它从 config.yaml 读，
所以不用上面那种 `set -a; . 文件` 的写法。）

### 地名需要一份城市数据表

「今天去过哪」里的地名靠 GeoNames 离线反查（`scripts/geo.py`）。服务器上：

- 数据表默认放仓库的 `data/`，但那个目录在 `ProtectSystem=strict` 下**只读**，
  所以 unit 用 `LIFELOGGING_DATA_DIR` 把它指到了 `/var/lib/lifelogging/geo`
- 第一次用到地名时会**下载 30–40 MB**。启动时就有一个后台线程去预热，
  不会卡在你发消息的那一下 —— 但第一次启动后请给它一点时间
- 下载失败**不会报错**，只会退回坐标，报出来是 `(39.9042,116.4074)` 这种。
  机器人检测到这种情况会**明说**，不会假装那是地名

不想让它下载的话，把笔记本上的 `data/cities1000.txt` 和 `data/admin1CodesASCII.txt`
拷到服务器的 `/var/lib/lifelogging/geo/` 下（那两个文件是 gitignore 的，不在仓库里）。

---

## 三个会静默失败的地方

### 1. 中文答案直接转手发出去

中继的 `answers_to_events()` 只对**字面量** `yes`/`no` 转小写，其余原样存；
而笔记本上 `apply_answers.answered_done()` 比的是 `== "yes"`，选择题比的是
**选项原文逐字相等**。

所以把「做了」原样发过去，答案会被愉快地存下来、任务**永远不被勾掉**，
而且**两边都不报错**。

`feishu_bot.py` 的 `canonicalise()` 就是为这件事存在的：它在 POST **之前**
把中文转成契约要的那几个字；认不出来就重问，**绝不猜**。
改这段代码时请保留这个性质。

### 2. 交互卡片按钮在长连接下会被静默丢弃

`lark-oapi` 的 WS 客户端 `_handle_data_frame` 只分发 `MessageType.EVENT`，
`MessageType.CARD` 那一支是**裸 `return`**：

```python
if message_type == MessageType.EVENT:
    result = self._event_handler._do_without_validation(pl)
elif message_type == MessageType.CARD:
    return                      # ← 卡片回调就这么没了
```

于是**按钮显示得出来、点了没反应、服务端一个错都不报**。
`register_p2_card_action_trigger` 永远不会被调用。（已对着 SDK 源码确认，
v1.5.3 起，v2_main 至今未改。）

**所以答案交互用的是编号文字。** 这也和仓库既有的 Tasker 哲学一致：
不会解析 JSON，就返回一个数字直接比字符串。

真想要按钮，得改 SDK 的 `_handle_data_frame`，那是另一件事 —— 单开一个改动，
带回归测试，别混进来。

### 3. 「中继在跑」不等于「中继是新版」

见上面验证那节的 curl。这个坑中继自己踩过一次：新版路由 404 了才发现服务器上
跑的还是旧版本。

---

## 排查

```bash
sudo systemctl status lifelogging-feishu-bot
sudo journalctl -u lifelogging-feishu-bot -n 50 --no-pager

# 状态文件：会话进行到哪了
sudo cat /var/lib/lifelogging/feishu-state.json
#   awaiting  = 正在等你答的那题题号；null = 没在等
#   recent    = 最近处理过的 message_id（挡飞书的超时重推）
#   sent      = 已经推出去过的题
#   last_push_day = 今天推过没
```

| 症状 | 多半是 |
|---|---|
| 服务 active，日志里没有 `connected to wss://` | 出站被挡（**不要**给 unit 加 `IPAddressAllow=`/`RestrictAddressFamilies=`），或 App ID/Secret 不对 |
| 起不来，报 `Job for … failed because of unavailable resources or another system error` | 十有八九是 `/etc/lifelogging/feishu-bot.env` **还没建**。这个 unit 里两个 `EnvironmentFile=` 都带了 `-`（见文件里的注释），所以正常情况下它会照常启动、由程序自己报清楚 —— 你要是看到 systemd 这句，说明装的是**旧版 unit**，重装一次 |
| 起来又立刻退出，日志里 `✗ FEISHU_APP_ID 没填（或还是占位符）` | 就是字面意思：env 文件没建，或者占位符没换成真值。见上面第 2 步 |
| `sudo /opt/lifelogging/repo/server/deploy.sh` 报 `command not found` | 那个文件的**可执行位**。git 在 Linux 上会记录权限位，而仓库里它一直是 `100644` —— clone 出来就没有 +x，而文档里 5 处都把它当命令直接跑。已于 2026-10-16 修成 `100755`；老克隆 `git pull` 一次即可生效，急着用就先 `sudo bash /opt/lifelogging/repo/server/deploy.sh` |
| 收到了消息，但答案从来不落库 | `FEISHU_RELAY_BASE` 对不上中继实际绑的地址。查 `systemctl show lifelogging-relay -p Environment` |
| 每次重启都把问过的题重问一遍 | 状态文件写不下去 —— unit 少了 `ReadWritePaths=/var/lib/lifelogging` |
| 主动推送报 230013 | 你不在应用的「可用范围」里 |
| 权限怎么都不生效 | 版本没发布，或没通过管理员审批 |
| 同一条消息回了两次 | 3 秒规则触发了超时重推。`on_message` 里**不能**发网络请求 —— 它只该入队 |
| 地名显示成 `(39.9042,116.4074)` 这种坐标 | GeoNames 数据表没下来。看日志里的 `[geo]` 那行；`LIFELOGGING_DATA_DIR` 指的目录必须**可写** |
| 问数据时回「查不到：…」 | 服务器上的 `config/config.yaml` 还没建，或 `sources.dawarich` 没填。⚠️ **这不会让服务起不来** —— 那两个模块是惰性 import 的，出题那条主链路不受影响，只是查数据不可用 |
| 问了「电脑上的事」，它回了一段手机数据 | 路由顺序被改坏了。必须先判 `_LAPTOP_WORDS` —— 见冒烟测试里 `feishu: 先说不再说能` 那一条 |
