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

> ⚠️ **这两个值你必须换成真的，别把上面的占位符原样留着。**
> 写成带中文的（`cli_你的真实appid`）会被**原样当凭据用** —— 值是有的、非空、
> 也不含 REPLACE，看着完全正常 —— 然后报出来的是 SDK 内部一个看不懂的编码错。
>
> 现在程序会自己拦住这种：`check_config()` 对「空 / 含 REPLACE / 混了非 ASCII」
> 一律判成没填，启动时直接退出并说清是哪个变量。日志里看到
> `✗ FEISHU_APP_ID 没填、还是占位符，或者里面混了非 ASCII 字符` 就是这里没改。
> （同一件事仓库里吃过一次亏，见 `scripts/common.py` 的 `require_api_key()`。）

`FEISHU_OWNER_OPEN_ID` **先留空**。第 4 步会告诉你它该填什么。

> 🔑 **App Secret 是对外凭据 —— 别把它贴进任何对话、截图或 issue。**
> 拿到 App ID + Secret 的人可以完全冒充这个机器人：读它收到的所有消息、
> 以它的身份发消息。**一旦贴出去过，就去控制台重置一次**
> （凭证与基础信息 → App Secret → 重置），然后更新这个 env 文件并重启。
> 日志里的 `wss://…access_key=…&ticket=…` 同理，那是临时凭据，也不该外传。

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

```bash
sudo journalctl -u lifelogging-feishu-bot -n 50 --no-pager | grep '\[recv\]'
# [recv] open_id=ou_xxxxxxxx chat=p2p text='...'
```

> ⚠️ **要 grep `[recv]`，不要 grep `open_id`。** 启动时那句警告
> （「没配 FEISHU_OWNER_OPEN_ID…」）里就含 `open_id` 这四个字母，按它 grep
> 出来全是那句警告，看不出到底有没有收到消息 —— 实测被这个绕了一次。

把那个 `ou_…` 填回 `/etc/lifelogging/feishu-bot.env` 的 `FEISHU_OWNER_OPEN_ID`，
再 `sudo systemctl restart lifelogging-feishu-bot`。

> ⚠️ **看不 `[recv]` 行时的两个方向，别混：**
>
> 1. **一条都没有** → 事件根本没到。去开发者后台看「事件与回调」，
>    确认订阅方式 = 长连接、事件里有 `im.message.receive_v1`，
>    并且**改完必须重新发版本**才生效。另一种表现是：机器人聊天窗连输入框
>    都没有（提示 Can't message this bot yet）——那也是同一个原因。
> 2. **有别的行、就是没有 `[recv]`** → 见上面那条 `sys.stdout.reconfigure`：
>    这个脚本已经把 stdout 改成行缓冲了，如果你看到的是旧版行为（自己 print
>    的行迟迟不出现、而 `[Lark]` 的行正常），说明服务器上跑的还是旧代码。

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
（二值题回「做了 / 没做」，选择题回数字；**答案后面可以接着写自己的话**，
后半句会进备注 —— 例如「做了，花了两小时」），再断言**落库的到底是哪个字**：

```bash
sudo grep '"domain": "question"' /var/lib/lifelogging/phone-$(date +%F).jsonl | tail -1
# 期望：…"value_text": "<题号>|yes|"
#       ⚠️ 必须是 yes，不是「做了」—— 见下面第 1 条
# 带备注时是 "<题号>|yes|花了两小时" —— 备注原样保存在最后一段
```

> ⚠️ **答案必须写在最前面。** 二值题/选择题最终写进 `value` 的只有 `yes`/`no`
> 或选项原文，`apply_answers` 靠它决定勾不勾任务 —— 而那一步**不可逆**。
> 所以宁可要求「答案先说」，也不去猜「哪一段才是答案」：猜错会把任务勾反，
> 而且不会有人发现。写在后面的进 `note`，会原样带进任务表的注释里。

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

## 记一笔（随手记录）

**给机器人发任何没被识别成「查数据」的话，它都会存下来。** 例如：

```
你：今天见了老王，聊了项目
它：记下了。（这条会进今天的日记。想让我出题就发「?」）
```

**「查数据」要求同时满足两件事：命中关键词，且那句话读起来像在问**
（问号，或者「吗 / 什么 / 多久 / 几次 / 哪 / 咋」这类疑问词）。
只命中关键词不算 —— `电脑 / 网页 / 浏览器 / 窗口 / 应用` 都是日常名词，
只看它们的话，一句「今天用电脑画了张图」会被当成查询，你收到一整份电脑
报表，而**自己写的那句话根本没存**。所以这里宁可把一句问句记成笔记
（它还会进日记），也不把笔记当问句吃掉。

代价：不带疑问语气的查询（「给我看看今天手机」）会被记成笔记。想明确提问
就带疑问词或问号。

**想记的句子偏偏长得像问句**（「这个电脑配置怎么样」）？前面加「记：」，
它就不进查数据那条路，无条件当笔记：

```
你：记：这个电脑配置怎么样，回头研究下
它：记下了。（这条会进今天的日记。想让我出题就发「?」）
```

前缀也认「记一笔：」「笔记：」（中文或英文冒号都行）。

存成 `domain=manual / metric=note`，走**和中继上其他手机事件同一条管道**：
中继 `/ingest/phone` → 当天 JSONL → 笔记本拉取 → `events.db` →
`normalize.Timeline.notes` → 日记提示词。

> ⚠️ 日记提示词里它被放在**时间块之后的第一节**，而且明确写着
> 「these are **first-hand facts** and outrank anything inferred」。
> 不这么说的话，模型可能用「你在 VSCode 待了 4 小时」盖掉「今天主要在陪家人」。
>
> ⚠️ **代价**（这是有意的取舍）：一句没命中关键词的问句（「我今天咋这么累」）
> 不会得到回答，而是被记下来。它没丢 —— 进了当天的日记资料 —— 但也不是
> 你要的那个答案。想明确出题就发「?」。

重置/清空：`state/feishu-state.json` 里的 `asked` 按天自动清。

## 实时提问（第二个提问来源）

除了笔记本晚上生成的「问责批」，服务器白天还会自己问几句 ——
`scripts/live_questions.py`，由它**自己的 timer** 每 30 分钟拉一次。

| | 谁生成 | 什么时候 | 问什么 | 存哪 |
|---|---|---|---|---|
| 问责批 | 笔记本（要 vault） | 晚上，问次日 | 昨天日记 / 挂着的事 | `questions-<day>.json` |
| 实时批 | 服务器 | 白天，问当场 | 今天的行踪 / 手机 | `questions-live-<day>.json` |

**每小时一题，固定节拍。** 不是「够阈值才问」—— 哪怕这一小时很平淡，
也会照实说「电脑上没记录、手机没解锁」，然后问你那段时间在做什么。

问出来大概长这样（确定性拼装，不上 LLM）：

```
13:19–14:19 这一段：
· 电脑：msedge.exe 28 分 · Code.exe 12 分 · WindowsTerminal.exe 5 分
· 手机：解锁 1 次，WeChat 切换最多

主要在忙什么？
```

数据来源各不相同，缺哪块就少哪一行：

| 一行 | 从哪来 | 缺了会怎样 |
|---|---|---|
| `位置：` | Dawarich（服务器） | 没在停留就不出现 |
| `电脑：` | 笔记本每 10 分钟推的快照里的 `last_hour` | 笔记本睡着 → 显示「没有记录到活动」 |
| `手机：` | 中继上的解锁 / 切应用事件 | 没解锁 → 显示「一次都没解锁」 |

> ⚠️ **阈值只是「选材依据」，不是发问门槛。** 最早的版本是三条阈值规则
> （停留 ≥90 分 / 静默 ≥90 分 / 单应用 ≥150 分），够不到就一题都不发 ——
> 数据平淡的那天会彻底沉默。使用者要的是「每小时至少问一次」，2026-10-17 改的。

### 怎么送到你手上

| 来源 | 什么时候发 |
|---|---|
| **实时批** | 机器人每 **5 分钟**看一眼中继，有新题就立刻推 —— 生成后几分钟内到手机上 |
| **问责批** | 每天 **08:30** 推一次（`FEISHU_PUSH_AT`），或者你发一句「?」 |

> ⚠️ **只在没在等答案时才推实时题**（`awaiting` 为空）。否则会把你正在答的那题
> 顶掉，而你以为自己还在答前一题 —— 答案就记到错的题上了。
>
> ⚠️ 一次只推一题。没答完的话，后面的题排队等着 —— 不会刷屏。
>
> 这条最初是缺的：服务器每 30 分钟生成题，但机器人只在 08:30 发一次，
> 于是「实时提问」实际变成了「第二天早上看到一堆昨天的题」。
> 2026-10-17 由使用者指出后补上（中继那边为此给每道题加了 `source` 字段）。

几条设计上的硬约束：

- **每小时最多一条。** 题的 key 里带小时（`hour-2026-10-17T14`）。timer 每
  30 分钟跑一次，同一小时的第二次会看到 key 已存在、什么都不做。
  所以一小时里最多一条，且**一定**有一条（白天窗口内）。
- **先推中继成功了才落「问过」的记录**（`/var/lib/lifelogging/live-asked-<day>.json`）。
  反过来的话，一次推送失败会让这个小时再也不发，服务器上还看不出任何异常。
- **一律是文字题**（`format="text"`、`ref=""`）。`apply_answers` 对文字题
  永远判「没完成」，所以实时题**不会去勾任何任务** —— 它只是问你一句。
- **会过期**：隔了天的实时题不再出现（「你 14:00 在哪」隔三天就是噪音）。
  笔记本那批正相反，它无脑取最新，因为「昨晚生成、今早来取」是常态。
- **夜里不发，而且是两道闸。**
  生成器有 8:00–22:00 的窗口（窗口外静默退出，**不算失败** —— timer 夜里那次
  本来就没事做）；**推送那一层也有同样的「别打扰」时段**（`FEISHU_QUIET_FROM` /
  `FEISHU_QUIET_UNTIL`，默认 22–8）。
  只有生成器那道闸是不够的：它只管「生不生成」，题生成完还躺着等推送 ——
  少了第二道，21:58 生成的题会在 22:03 被收到，或者服务器半夜重启时把待推的
  题发出去。两处的小时要一起改。
- **只推当天的实时题。** 隔了夜的「过去一小时」没有意义，早上八点收到一条
  「昨天 21:00–22:00 这一段…」只会让人困惑。

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

## 反射弧（没问它也说话）

`server/feishu-bot/reflex.py`。机器人每 **10 分钟**（`FEISHU_REFLEX_EVERY_MIN`）
看一眼当天数据，命中下面任一条就发一句：

| 规则 | 条件（默认） | 什么时候 |
|---|---|---|
| `late_night` | 00:30–04:00 之间，手机 20 分钟内还有活动 | 就是那一刻 |
| `screen_heavy` | 手机亮屏 ≥ 6 小时 | 18:00 之后 |
| `leisure_heavy` | 单个娱乐应用 ≥ 3 小时 | 18:00 之后 |
| `long_desktop` | 电脑快照新鲜（≤30 分钟）且同一窗口连续 ≥ 3 小时 | 8:00–23:00 |

```
⚡ 反射弧 · 娱乐时长
WeChat 今天 3 小时 20 分（阈值 3 小时）。
```

四条边界，都是有原因的：

- **每条一天最多一次，两条之间至少 60 分钟。** 连着响的提醒会被静音，
  然后连真正重要的那条也一起错过。
- **正有一道题在等答案时不发。** 否则你在飞书里回的那句话会被当成答案存下去。
- **只提醒，不执行。** 不做「到点关网卡」这种事 —— 误报一次的代价是你把整个
  系统关掉。
- **不猜。** 规则全是确定性阈值，没有模型；笔记本快照超过 30 分钟就当「不知道
  你现在在干嘛」，宁可不说。

阈值都在 `reflex.py` 顶部（也可用 `REFLEX_*` 环境变量改）。整个功能用
`FEISHU_REFLEX=0` 关掉。

> ⚠️ 这是个**主动打扰**的通道，误报比漏报贵得多。改阈值时先问「这句话
> 错发了会怎样」，而不是「漏发会怎样」。

## 晚上收尾（每日总结）

每天 **21:30**（`LIVE_SUMMARY_AT`）之后，实时题换成一批**收尾题**：

- 4 条数据题：最值得记住的一件事 / 电脑分布 / 手机节奏 / 位置
- 7 个常规维度：体力、心情、人际、睡眠、吃饭、休息、学习（每天全问，措辞轮换）
  - **心情和体力是 1–5 打分题**（回数字，后面可以补一句为什么），其余五维仍是自由文本
- 1 条「今天有什么没做成、想留到明天的？」

> **为什么只有这两维打分：** 自由文本进不了统计。`scripts/collect_labels.py`
> 每天把「4 不错」抽成 `state/labels.jsonl` 里的一行 `{day, dim: 心情,
> score: 4, note: "下午有点堵"}`，这是以后做相关性/预测时**唯一的标签来源**。
> 没有 `dim` 的旧数据、或者答的是「还行」这种自由文本，一律**跳过不估分** ——
> 猜出来的标签比没有标签更坏。七维全改成选择会把晚上变成填表，所以没这么做。

答完所有收尾题，机器人会**用 LLM 写一段当天总结**发给你，并**把总结也存成一条笔记** ——
所以它明天会出现在日记资料里，是真·记忆的一部分，而不只是聊天记录里的一句话。

> ⚠️ **总结不再只看收尾题。** 它通过 `GET /qa?day=` 取当天**全部**回答：
> 早晨问责批 + 15 条小时实时题 + 收尾批，按
> 「白天的实时回答 / 早晨的问责与常规 / 收尾回答」分组，备注也一起带上。
> 这样你在白天随口说的「在上课」「在拿快递」也会进入总结，而不是只有晚上
> 那 12 条。

> ⚠️ 收尾题一天只发一次（key 前缀 `summary-<day>`）。到点前一小时就不再发
> 小时题了，否则晚上连着两条。
>
> ⚠️ 总结走的是同一套每日额度（默认 10 次），费用进同一份成本台账。
> 额度用完时只会说一句「没写成」，不会静默。

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
| 机器人聊天窗**连输入框都没有**，提示 `Can't message this bot yet` | 事件订阅缺 `im.message.receive_v1`。官方 FAQ 的原话：机器人必须订阅「接收消息」事件，聊天窗口才会出现对话框。**和版本发布、可用范围无关** —— 那两样都对也一样会这样。改完必须**重新发版本** |
| 自己 `print` 的行迟迟不出现，而 `[Lark] …` 的行正常 | stdout 被块缓冲了（systemd 捕获的是管道）。旧版代码有这个问题，已用 `sys.stdout.reconfigure(line_buffering=True)` 修掉 —— 还这样的话说明服务器上跑的是旧代码 |
| 收得到消息、**发不出去**；日志里 `code: 21001 Access denied by access control policy` | `EXIT 21001` 是**数据安全策略**拦的，⚠️**不是 IP 白名单**（那个是 `99991401`，而且官方文档明说它不作用于 token 接口）。去看两处：开发者后台 → 权限管理（每个 scope 是「已开通」还是「需申请/已拒绝/默认关闭」）；管理后台 → 安全 → 数据安全 → 应用权限管控。⚠️ 注意长连接**能连上**不代表 OpenAPI 可用——两者走不同的鉴权路径 |
| 服务 active，日志里没有 `connected to wss://` | 出站被挡（**不要**给 unit 加 `IPAddressAllow=`/`RestrictAddressFamilies=`），或 App ID/Secret 不对 |
| 起不来，报 `Job for … failed because of unavailable resources or another system error` | 十有八九是 `/etc/lifelogging/feishu-bot.env` **还没建**。这个 unit 里两个 `EnvironmentFile=` 都带了 `-`（见文件里的注释），所以正常情况下它会照常启动、由程序自己报清楚 —— 你要是看到 systemd 这句，说明装的是**旧版 unit**，重装一次 |
| 起来又立刻退出，日志里 `✗ FEISHU_APP_ID 没填（或还是占位符）` | 就是字面意思：env 文件没建，或者占位符没换成真值。见上面第 2 步 |
| `sudo /opt/lifelogging/repo/server/deploy.sh` 报 `command not found` | 那个文件的**可执行位**。git 在 Linux 上会记录权限位，而仓库里它一直是 `100644` —— clone 出来就没有 +x，而文档里 5 处都把它当命令直接跑。已于 2026-10-16 修成 `100755`；老克隆 `git pull` 一次即可生效，急着用就先 `sudo bash /opt/lifelogging/repo/server/deploy.sh` |
| 收到了消息，但答案从来不落库 | 中继地址对不上。**先看启动日志里那行 `→ 中继地址 …`** —— 程序会自己从 `lifelogging-relay.service` 里读出 `RELAY_HOST` 并探一遍 `/health`。它挑错了或挑不出来，才用 `Environment=FEISHU_RELAY_BASE=` 显式覆盖 |
| 每次重启都把问过的题重问一遍 | 状态文件写不下去 —— unit 少了 `ReadWritePaths=/var/lib/lifelogging` |
| 主动推送报 230013 | 你不在应用的「可用范围」里 |
| 权限怎么都不生效 | 版本没发布，或没通过管理员审批 |
| 同一条消息回了两次 | 3 秒规则触发了超时重推。`on_message` 里**不能**发网络请求 —— 它只该入队 |
| 日志被同一句 `✗ …` 每 5 秒刷一遍 | 配置有问题，而 unit 是 `Restart=always`。代码对配置错用退出码 `EXIT_CONFIG=2`，unit 里 `RestartPreventExitStatus=2` 让它停下来等人。**两边必须一致**（冒烟测试里钉了这点）。⚠️ systemd 自带的 `StartLimitBurst` **拦不住**这个：默认 5 次/10 秒，而 `RestartSec=5` 意味着 10 秒里只重启 2 次，永远够不到上限 |
| 地名显示成 `(39.9042,116.4074)` 这种坐标 | GeoNames 数据表没下来。看日志里的 `[geo]` 那行；`LIFELOGGING_DATA_DIR` 指的目录必须**可写** |
| 问数据时回「查不到：…」 | 服务器上的 `config/config.yaml` 还没建，或 `sources.dawarich` 没填。⚠️ **这不会让服务起不来** —— 那两个模块是惰性 import 的，出题那条主链路不受影响，只是查数据不可用 |
| 问了「电脑上的事」，它回了一段手机数据 | 路由顺序被改坏了。必须先判 `_LAPTOP_WORDS` —— 见冒烟测试里 `feishu: 先说不再说能` 那一条 |
