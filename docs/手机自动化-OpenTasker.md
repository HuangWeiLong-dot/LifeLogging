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
