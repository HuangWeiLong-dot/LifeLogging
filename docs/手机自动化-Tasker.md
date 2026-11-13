# 手机自动化：Tasker 版

这份是**当前在用**的 Tasker 配置。`docs/手机自动化-OpenTasker.md` 是以后想换开源替代时的迁移记录，两者请求体完全一样，只是触发方式和变量名不同。

所有事件都 POST 到中继：

```
POST https://relay.liveinpassion.me/ingest/phone
X-API-Key: <relay key>
Content-Type: application/json
```

> **时间戳统一用 `%TIMES`**（Unix 秒）。中继直接收 Unix 秒，不需要在 Tasker 里转 ISO ——
> 手机端少一步转换，就少一个出错的地方。

---

## 1. 电量

- **Profile**：`Time`（每 15 分钟，`15` / `15`，Both）+ `Power`（Any）两个上下文，OR 关系
- **任务**：HTTP Request

```json
{"ts":%TIMES,"domain":"phone","metric":"battery","value_num":%BATT,"meta":{"charging":"%LL_CHG"},"source":"tasker"}
```

- `%BATT` 是 0–100 的整数
- 落库后本地会算：当天起始/最低/最高电量、掉电量、充电时长

> ⚠️ **Tasker 没有 `%CHARGING` 这个内置变量**（官方变量表里只有 `%BATT`）。
> 用了不存在的变量，Tasker 会把 `%CHARGING` **原样**发上来，而不是发空值。
>
> 充电状态要自己维护一个全局变量 `%LL_CHG`，两个 Profile：
>
> | Profile | 任务 |
> |---|---|
> | `State → Power → Any` | `Variable Set %LL_CHG` → `true` |
> | `State → Power → Any`（勾 **Invert**） | `Variable Set %LL_CHG` → `false` |
>
> 建好之后先用 `Flash %LL_CHG` 插拔一次充电器确认。

---

## 2. 网络

- **Profile**：`Wifi Connected`、`Wifi Not Connected`、`Mobile Network` 三个状态
- 建**两个**任务，分别绑定：

| Profile | 任务 | `%NET` 设为 |
|---|---|---|
| Wifi Connected | `LL-Net-Wifi` | `wifi` |
| Wifi Not Connected | `LL-Net-Mobile` | `mobile` |
| Mobile Network | `LL-Net-Mobile` | `mobile` |

每个任务：`Variable Set %NET` → 上表的值，然后 HTTP Request：

```json
{"ts":%TIMES,"domain":"phone","metric":"network","value_text":"%NET","source":"tasker"}
```

> ⚠️ **别用 `If %WIFI ~ on` 判断**：`%WIFI` 表示「Wi-Fi **开关**是否打开」，
> 不是「是否连着」。开着 Wi-Fi 但没连上时，它会让你报成 `wifi`。
>
> ⚠️ **Tasker 没有 `%SSID` 内置变量**（只有 `%WIFII`，是人读的整段文本）。
> 所以 `value_text` 只发 `wifi` / `mobile`，不带 SSID。

> 只在**状态变化**时发，不要加定时触发。两条之间超过 45 分钟的空档，本地不会计入。

---

## 3. 在听什么

Tasker 有内置的 **Music Track** 事件，不用碰通知监听。

- **Profile**：`Event → Media → Music Track`
- **任务**：HTTP Request

```json
{"ts":%TIMES,"domain":"phone","metric":"music","value_text":"%MTRACK","source":"tasker"}
```

- `%MTRACK` 就是「Artist - Title」这一行，**不要自己拆成两个字段**——各家播放器格式不一样
- 事件只在**换歌**时触发，同一首循环播放不会重复发

> ⚠️ 如果 `%MTRACK` 是空的，说明当前播放器不广播媒体信息（部分国产 App 会）。
> 先用 `Flash %MTRACK` 确认一次再建 Profile。

---

## 4. 通知

- **Profile**：`Event → UI → Notification`
- **任务**：HTTP Request

```json
{"ts":%TIMES,"domain":"phone","metric":"notification","value_text":"%evtprm1","meta":{"title":"%evtprm2","text":"%evtprm3"},"source":"tasker"}
```

> ⚠️ **先确认 `%evtprm` 的顺序**：新建一个任务，动作只放 `Flash %evtprm()`，
> 触发一次通知，看它数组里到底哪一项是应用、哪一项是标题。
> Tasker 的版本和事件类型不同，顺序可能不一样。

> ⚠️ **正文只留在本地。** `publish_life_raw` 发布前会删掉
> `title`/`text`/`bigText`/`subText`/`summary`/`lines`，只发「哪个 App、几条」。
> 不想本地也留正文，就把 `meta` 整个删掉，只发 `value_text`。

---

## 5. 应用使用时长（最容易被忽略的一条）

Tasker **不需要**写 Java，它内置的 **App Info** 动作就是 UsageStatsManager 的封装。

- **Profile**：`Time`，每 15 分钟

### 5.1 先确认 App Info 输出什么

1. `TASKS` → `+` → 命名 `LL-TestUsage`
2. `+` → **App** → **App Info**
   - Source：**Most Used**
   - Type：**Time**
   - 参数：`most(time,15:0,20)`
3. `+` → **Alert** → **Flash**，Text 填 `%app_used()`
4. `+` → **Alert** → **Flash**，Text 填 `%app_used_time()`
5. 点 ▶，看两次 Flash：
   - 第一个应该是应用列表
   - 第二个是时长。**记下格式**：如果是 `312` 这种纯数字，直接用；
     如果是 `5m12s` 这种文本，先告诉我，本地解析要另做

> 两个 Flash 都是空 → 这个 Tasker 版本的 App Info 不返回时长，
> 这条做不了。跳过它，推断值仍然能用。

### 5.2 建任务 `LL-AppUsage`

按顺序加 **9 个动作**（For 和 End For 之间的动作就是循环体）：

| # | 动作 | 字段 |
|---|---|---|
| 1 | **App Info** | Source=`Most Used`，Type=`Time`，参数=`most(time,15:0,20)` |
| 2 | **Variables → Variable Clear** | Name=`%item*` |
| 3 | **Task → For** | Variable=`%idx`，Items=`1:%app_used(#)` |
| 4 | **Variables → Variable Set** | 见下方 |
| 5 | **Task → End For** | — |
| 6 | **Variables → Variable Join** | Variable=`%item`，Joiner=`,` |
| 7 | **Variables → Variable Set** | Name=`%body`，To=`{"events":[%item]}` |
| 8 | **长按 + → Paste**（HTTP Request） | Body 只填 `%body` |
| 9 | **Variables → Variable Clear** | Name=`%body` |

第 4 个动作的字段：

- **Name**：`%item(%idx)`
- **To**：

```
{"ts":%TIMES,"domain":"phone","metric":"app_usage","value_text":"%app_used(%idx)","value_num":%app_used_time(%idx)}
```

> 第 4 个动作**必须在 For 和 End For 之间**。Tasker 里在 For 后面新增的动作
> 会自动进循环体；如果你先建好再拖动，注意别拖到 End For 后面。

### 5.3 验证

1. 在 HTTP Request **前面**临时插一个 `Flash %body`
2. 点 ▶，应该看到：

```json
{"events":[{"ts":1763...,"domain":"phone","metric":"app_usage","value_text":"com.tencent.mm","value_num":312}, ...]}
```

3. 格式对了就把 Flash 删掉

### 5.4 加触发

`PROFILES → + → Time`，每 `15` 分钟，Both → 绑 `LL-AppUsage`

> `value_num` 必须是**这 15 分钟内的前台秒数**。`most(time,...)` 本身就是按
> 时间窗口取的，所以直接就是窗口内的值，不用自己做差分。
> 一次发一批（`{"events":[...]}`），别一个应用发一次。

> **为什么必须有这条**：现在的「使用最久的 App」是拿两条应用切换事件的**时间差推**的，
> 漏一次切换，后面整段都会记到上一个应用头上，而且不报错。
> 有了实测值，本地只在总量 **≥ 5 分钟**时用它替换推断值，并保留推断值做对比。

---

## 6. 上线后确认

1. 手机随便操作几分钟
2. 服务器上看当天条数：

```bash
curl -s -H "X-API-Key: $KEY" \
  "https://relay.liveinpassion.me/events?day=$(date +%F)" | \
  python -c "import json,sys,collections;d=json.load(sys.stdin);print(d['count']);print(collections.Counter(e['metric'] for e in d['events']))"
```

期望看到 `battery`、`network`、`music`、`notification`、`app_usage` 这些新 metric。

3. 本地跑一次 `python scripts/collect_phone_events.py --date today`，
   再看 `state/timeline-<day>.json` 里 `phone.battery` / `phone.music` / `phone.notifications`。
