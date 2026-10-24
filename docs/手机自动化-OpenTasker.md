# 手机自动化：从 Tasker 换成 OpenTasker

Tasker 是付费的。OpenTasker（[SysAdminDoc/OpenTasker](https://github.com/SysAdminDoc/OpenTasker)，
`com.opentasker.app`）是 MIT 许可的开源替代，**能力上够用**：

| 现在用 Tasker 做的 | OpenTasker | 怎么实现 |
|---|---|---|
| 屏幕解锁 | ✅ | `ACTION_USER_PRESENT` → `unlocked` 状态 |
| 屏幕亮 / 灭 | ✅ | `ACTION_SCREEN_ON/OFF` → `screen` 状态 |
| **应用切换（前台应用）** | ✅ | **UsageStats 前台检测**（需 Usage Access） |
| HTTP POST + 自定义头 | ✅ | HTTP Request 动作 |
| **上传 Gadgetbridge 文件** | ✅ | HTTP Request 的 `body_file` |
| 每 30 分钟轮询 `/ask/pending` | — | **删掉**：问题现在走飞书 |

> 选它而不是 Mahout 的原因：Mahout 没有前台应用触发器（清单里
> `PACKAGE_USAGE_STATS` 标着 unused），HTTP 节点也不支持文件 body。

---

## 0. 装之前

- **Android 8.0+**
- 装法：GitHub Releases 下 APK，或用 Obtainium 订阅这个仓库
- 装完在它的 **Setup** 页把这两项授权掉：
  - **Usage Access**（前台应用检测必需，和 Tasker 一样）
  - **通知 / 电池优化**（要长期后台跑）

---

## 1. 先确认变量名（重要）

前台应用包名在模板里的写法，不同版本可能不同。**别照抄，先让它自己显示一次**：

1. 建一个「前台应用」上下文的 Profile，挂一个只做「发通知」的任务
2. 用它的 **Trigger simulation / Inspector** 触发一次
3. 看 Inspector 里实际展开的变量名

本文按 `{{event.foreground}}` 写。**如果 Inspector 显示的是别的**（比如
`{{task.foreground}}`），以它为准。

---

## 2. 三个常量

后面每条任务都用这一组。

| 项 | 值 |
|---|---|
| URL | `https://relay.liveinpassion.me/<路径>` |
| Header 1 | `X-API-Key: <中继的 key>`（`/etc/lifelogging/relay.env`） |
| Header 2 | `CF-Access-Client-Id: <Tasker 那把服务令牌的 Client Id>` |
| Header 3 | `CF-Access-Client-Secret: <同一把令牌的 Secret>` |
| Content-Type | `application/json` |

> ⚠️ **必须是 `https://`**。OpenTasker 全 app 禁明文，`http://` 会被直接拒。
> 走 Cloudflare Tunnel 的域名正好是 https。
>
> ⚠️ 那把服务令牌在 Cloudflare Zero Trust → Access → Service Auth 里。**别新建**，
> 用 Tasker 现在用的同一把 —— 两份令牌在 Access 里是「或」的关系，多一把就多一个
> 要轮换的东西。

---

## 3. 四个上报任务

### 3.1 解锁

- 触发：屏幕状态上下文 → `unlocked = true`
- 动作：HTTP Request → `POST https://relay.liveinpassion.me/ingest/phone`

Body：

```json
{"ts":"now","domain":"phone","metric":"unlock","source":"opentasker"}
```

### 3.2 屏幕亮

- 触发：`screen = on`
- Body：

```json
{"ts":"now","domain":"phone","metric":"screen_on","source":"opentasker"}
```

### 3.3 屏幕灭

- 触发：`screen = off`
- Body：

```json
{"ts":"now","domain":"phone","metric":"screen_off","source":"opentasker"}
```

> **为什么 `"ts":"now"`**：OpenTasker 的模板里没有内置的 Unix 秒变量，而中继
> 需要时间戳。中继专门认这一个字面量，用收到它的那一刻当事件时间。
> 实测延迟在毫秒级，对「屏幕亮灭」这种精度完全够。
>
> ⚠️ **别用「不写 ts」代替** —— 中继会直接丢掉没有时间戳的事件（这是刻意的：
> 批量补传历史数据时少一个字段就把所有事件压到收件那一刻，比丢掉更难发现）。

### 3.4 应用切换（最关键的一条）

- 触发：**前台应用**上下文
- 动作：HTTP Request

Body：

```json
{"ts":"now","domain":"phone","metric":"app_switch","value_text":"{{event.foreground}}","source":"opentasker"}
```

> ⚠️ **`value_text` 必须是包名**（`com.tencent.mm`），不是「微信」。
> `normalize.PHONE_APPS` 那张映射表是按包名建的；填应用名不是不能跑，
> 但不在表里的会被当成「没认出来的应用」。
>
> ⚠️ **触发频率**：Tasker 的 `App Changed` 是**事件**（切一次发一次）；
> OpenTasker 的前台应用是**状态**（进入/离开各一次）。A→B→A 来回切，两边
> 都会触发，行为等价。但**第一次上线后看一眼中继的当天条数**，如果比平时多
> 一倍，说明它把「离开」也算了一次，需要加个条件。

---

## 4. 手环数据上传

- 触发：每天 **22:00**（比笔记本 23:00 的流水线早一小时）
- 动作：HTTP Request
- URL：`https://relay.liveinpassion.me/upload/gadgetbridge.db`
- Header：同上面三条
- **Content-Type：`application/octet-stream`**
- **Body 选「来自文件」**（`body_file`），指向 Gadgetbridge 导出的 `.db`

> ⚠️ **不是 multipart，也不是把内容粘进文本字段。** 中继的 `/upload` 收裸 body，
> 而且会检查开头的 `SQLite format 3` 魔数 —— 用文本字段传上去的东西「看起来
> 有几十 KB、时间戳也对，但 SQLite 打不开」。
>
> ⚠️ **文件必须在 OpenTasker 自己的文件目录里。** 先把 Gadgetbridge 的自动导出
> 目标指过去，或用它的「接收分享」触发器中转一次。这一步是迁移里唯一需要试的。

### 4.2 锻炼轨迹（GPX / FIT）

换到 OpenTasker **之前**就漏了这一条：中继的 `/upload/gpx`、`/upload/fit`
早就写好了，笔记本 `run_live.ps1` 每轮也都在拉，但**手机从来没传过文件**，
所以地图上一条手表路线都没有（中继 `/files/gpx` 返回空列表）。

Gadgetbridge 每结束一次锻炼会导出**一对同名文件**：

```
2026-10-23T08_54_12+08_00-running.fit
2026-10-23T08_54_12+08_00-running.gpx
```

两条任务（或一条任务里两个 HTTP Request 动作），除了 URL 外配置相同：

| 字段 | `.fit` 那条 | `.gpx` 那条 |
|---|---|---|
| Method | `POST` | `POST` |
| URL | `https://relay.liveinpassion.me/upload/fit` | `https://relay.liveinpassion.me/upload/gpx` |
| Header | 同 §2 三条 | 同 §2 三条 |
| Content-Type | `application/octet-stream` | `application/octet-stream` |
| Body | 「来自文件」→ 那个 `.fit` | 「来自文件」→ 那个 `.gpx` |

**「传哪个」：全传，一个不落。** 导出目录里的每一对文件是**一次独立活动**
（文件名带的是各自开始时间，体积也各不相同），少传一对就少一次锻炼。

> ⚠️ `File To Send` **只能填一个具体路径**，指不到一个目录。所以要先列目录再循环：

**Tasker 的动作顺序**（每 30 分钟触发一次就够）：

1. **List Files** —— Dir `Download/Gadgetbridge`，Match `*.gpx`，Sort `Date`
   → `%files`（`%files1…%filesN`）
2. **For** `%f` in `%files`
3. **HTTP Request** —— URL `/upload/gpx`，`File To Send` =
   `Download/Gadgetbridge/%f`
4. **File Move** —— 同一个路径 → `Download/Gadgetbridge/sent/%f`
5. **End For**
6. 1–5 整段复制一份，`*.gpx` → `*.fit`，URL → `/upload/fit`

> ⚠️ **第 4 步别省。** 第一次跑会把目录里已有的文件全传一遍 —— 这正是想要的，
> 历史锻炼一次补齐；但传完不挪走的话，之后每轮都会把几百个文件重传一遍。
> 挪进 `sent/` 子目录后 `List Files`（只列当前目录）就看不见它们了。
>
> ⚠️ **别删**，用「移动」。手机上的原始文件留着，以后要补哪次还有源。
>
> ⚠️ **「移动」放在 HTTP Request 之后**，并且 HTTP 返回非 2xx 时不要移动 ——
> 否则一次断网就把这批锻炼永久跳过（中继没收到、手机也移走了）。

> ✅ **重复传没关系。** 中继按内容 sha1 存成 `<sha1>.fit`，同一份传十次也只有
> 一份；手机上**不需要记**「上次传到哪了」。这比维护一个 `%lastfile` 变量可靠
> 得多（那个变量一旦丢了就会静默漏掉一批锻炼）。
>
> ✅ **Android 11+ 要授予「所有文件访问」权限**，Tasker 才读得到 `Download/`
> 下的文件；否则 `List Files` 返回空，任务静默什么都不传。

**验证**（笔记本上，不依赖手机）：

```bash
python scripts/collect_media.py     # 期望看到 gpx/fit 各有 N 个、N new
python -c "import sqlite3;c=sqlite3.connect('state/events.db');print(c.execute(\"select source,count(*) from events where metric='watch_track' group by source\").fetchall())"
```

第一行还是 `0 on relay` 就说明手机没传上去 —— 先查中继日志里有没有
`POST /upload/gpx`（Cloudflare Tunnel 后面），再看 OpenTasker 的响应码。

---

## 5. 迁移顺序（别搞反）

1. **先停掉 Tasker 的四个上报 Profile**（不是卸载 Tasker）
2. 再启用 OpenTasker 的四个任务
3. 第二天看一遍数据

> ⚠️ **两边同时跑 = 所有手机数据翻倍。** 事件的去重键是
> `(source, domain, metric, ts_utc, value_text)`，而两边 `source` 不同
> （`tasker` / `opentasker`），所以**不会去重**，而是各算一次。
> 解锁次数、切换次数、屏幕时长全会翻倍，而且脚本不会报错。
>
> 这也是为什么 `source` 用了 `opentasker` 而不是复用 `tasker`：迁移期间能一眼
> 看出哪些是新上报的。

---

## 6. 验证

在笔记本上（或服务器上）看当天的条数：

```bash
curl -s -H "X-API-Key: $KEY" \
  "https://relay.liveinpassion.me/events?day=$(date +%F)" \
  | python -c "import json,sys,collections;d=json.load(sys.stdin);print(d['count']);print(collections.Counter((e['source'],e['metric']) for e in d['events']))"
```

期望看到 `('opentasker', 'unlock')`、`('opentasker', 'app_switch')` 这些。

再跑一次归一化，看应用榜是不是还有数：

```bash
python scripts/normalize.py --date today
```

---

## 7. 回滚

停 OpenTasker 的四个任务、重新启用 Tasker 的 Profile 就行。数据格式完全一样，
中继和笔记本侧不认识 `source` 是谁。

---

## 8. Tasker 里可以删掉的东西

| Tasker 配置 | 处置 |
|---|---|
| 每 30 分钟轮询 `/ask/pending` | **删掉** —— 每日提问现在走飞书 |
| 4 个上报 Profile | 先停用，确认新链路正常后再删 |
| Gadgetbridge 上传任务 | 同上 |
