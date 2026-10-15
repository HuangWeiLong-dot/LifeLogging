# Grafana 可视化

## 这是什么

把 `state/events.db` 直接接给 Grafana。**不复制、不导出**，读的就是采集和分类写进去的那份 SQLite。

回答你的问题「数据能否存在某个地方，将来可视化」：**能，而且已经在存了**。
MD 只是展示层，底下是一张标准的长表：

```sql
-- events 表：原始事实
-- ts_utc | day_local | domain | metric | value_num | value_text | source | meta_json

-- blocks 表：分类后的时间块（可视化最常用的）
-- day_local | start_utc | end_utc | hours | category | app | title | url |
-- confidence | blackhole | source
```

## 启动

```bash
cd docker/grafana
docker compose up -d
# 等 1 分钟让插件装完
# 打开 http://localhost:3001   admin / admin
```

数据源和仪表盘都已通过 provisioning 自动配好，进去直接看。

---

## 预置仪表盘

`provisioning/dashboards/` 下的 `.json` 会在启动时自动载入，Grafana 里归到 **LifeLogging** 文件夹：

| 仪表盘 | 内容 |
|---|---|
| **生命记录 · 概览** | 今日八个数字（记录时长 / 时间黑洞 / 应用 / 浏览器 / 步数 / 心率 / 手机解锁 / AI 花费）+ 90 天分类趋势 + 应用与站点 Top 10 |
| **生命记录 · 时间与分类** | 分类堆叠、占比、应用/站点排行、逐条时间块明细、黑洞清单。顶部有「回看天数」变量 |
| **生命记录 · 位置** | **地图（最近一天真实轨迹 + 近 30 天停留）**、停留、轨迹点、活动范围、交通段明细。数据来自 Dawarich |
| **生命记录 · 手机使用** | 解锁 / 切应用 / 亮屏次数、按天的手机活动量、应用 Top 15、原始事件流。数据来自 Tasker → 服务器中继 |
| **生命记录 · 健康与运动** | 步数、心率、VO2max、跑步距离趋势、单次锻炼明细。数据来自 Gadgetbridge |
| **生命记录 · AI 用量** | **账号真实花费（精确）**、按模型拆分、余额、按项目 token。数据来自 DeepSeek 平台接口 + Claude Code 本地记录 |

改完 `.json` **不用重启**：provider 设了 `updateIntervalSeconds: 30`，30 秒内自动重载。
但在 UI 里改的面板**不会写回文件**（`allowUiUpdates: true` 只影响内存），下次重载就被覆盖。
要持久化就改文件。

### 写查询时最容易踩的三个坑

**1. SQL 要写进 `rawQueryText`，不是 `queryText`。**

这是最坑的一个，因为它**不报错**——面板只会显示 "No data"。

插件的查询模型是：

```js
{ rawQueryText: <你写的 SQL>,          // 编辑器绑定的字段，输入
  queryText:    <变量替换后的 SQL>,     // 执行前由前端算出来，输出
  queryType:    "table" | "time series",
  timeColumns:  ["time"] }
```

执行前插件会跑 `queryText = templateSrv.replace(rawQueryText)`。
**只填 `queryText`、不填 `rawQueryText`，这一行就会把你的 SQL 覆盖成空串。**
插件于是执行一个空查询、返回空帧、面板显示 "No data"，日志里全是 `status=ok` ——
因为确实没出错，只是查了个空字符串。

> 直接调 `/api/ds/query` 时用 `queryText` 是**能**通的（后端认这个名字）。
> 只有走前端才会中招。所以用 curl 验证过的查询，放进仪表盘里照样可能是空的。

**2. 必须显式配 `busy_timeout`，否则并发查询会撞锁。**

插件 `pathOptions` 的默认值是 `_pragma=query_only(1)` —— 也就是 **`busy_timeout = 0`**，
含义是「遇到锁立刻报错，不等待」。而打开一个仪表盘会**同时**发十几个面板查询，
它们必然互撞，于是整片面板报：

```
{"error":"database is locked (261)","errorSource":"plugin","status":500}
```

**单个查询测不出来**（单独查 5 次全部成功），只有并发才炸。
数据源里的配置：

```yaml
pathOptions: "_pragma=query_only(1)&_pragma=busy_timeout(10000)"
```

> ⚠️ 这个默认值在 Grafana 的配置页里是**预填**在输入框里的，
> 所以它看起来像「已经配好了」，实际上并没配。

**3. 时间列必须用 `timeColumns` 显式声明。**

SQLite 没有时间类型，`datetime(day_local)` 这种表达式不带类型信息，
插件会把整列当字符串，时间序列面板就画不出来。要加：

```json
"timeColumns": ["time"]
```

值是**字符串数组**（传字符串会报 `cannot unmarshal string into Go struct field`）。
另外 `day_local` 是**本地日期**，时间表达式要带时区偏移，否则日期桶整体偏 8 小时：

```sql
strftime('%Y-%m-%dT%H:%M:%S+08:00', day_local) AS time
```

### 另外：`value_num` 的单位随 metric 变，不是统一的

`screen/app_usage` 和 `web/site_usage` 是**秒**；`location/visit` 和 `location/transport`
是**分钟**；`location/tracked_points`、`location/track_path` 是**个数**；
`health/steps` 是步、`health/heart_rate_*` 是 bpm、`fitness/workout` 是分钟；
`ai/daily_cost` 和 `ai/cost_reconcile` 是**元**。
写面板时别一律 `/3600`。

### 改完怎么验证

**别只看容器起没起来，也别只用 curl 试查询** —— 上面第 1、2 条都只有走完整前端才暴露。
仓库里带了个脚本，用无头 Chrome 真正渲染一遍，把每个面板的标题和值打出来：

```bash
node docker/grafana/verify_dashboards.mjs
# 或指定某个仪表盘
node docker/grafana/verify_dashboards.mjs http://localhost:3001/d/lifelogging-location
# 顺带截图（地图面板只能靠这个验）
node docker/grafana/verify_dashboards.mjs --shot state/logs/shots
```

它会对每个仪表盘报 ✅/❌，看到 "No data" 或 "database is locked" 就是没配好。
只依赖 Node 18+（用内置的 WebSocket/fetch），不装 npm 包。

⚠️ **它的默认仪表盘列表是写死的**，不会去读 provisioning 目录。加了新仪表盘
记得往 `verify_dashboards.mjs` 的 `ALL` 数组里加一行，否则它会安安静静地不被验证。

⚠️ **地图面板要截图看**。「不是 No data」只说明查询有数据，不代表地图是对的 ——
底图加载失败、点全堆在 (0,0)、路线连成乱麻，这三种情况下文字校验都报 ✅。

## 几个数字的口径（这几个数最容易看错）

### 地图：真实轨迹，底图必须写显式 XYZ

`生命记录 · 位置` 顶部那张地图画的是**手机上报的原始 GPS 轨迹**：
`collect_phone.py` 每天写一条 `location/track_path` 事件，
折线存在 `meta_json.path` 里（最多 500 个点，等间隔抽样、首尾必留），
面板用 `json_each()` 在 SQL 里展开成 (lat, lon) 两列。

**底图不能用面板里那个内置的「OpenStreetMap」选项。**
新版 Grafana 的内置 OSM 底图走 Stadia，没有 key 会满屏印
「API KEY REQUIRED」水印 —— 而且**不报错**，面板看起来是正常的。
必须写成显式 XYZ：

```json
"basemap": {
  "type": "xyz",
  "name": "OpenStreetMap",
  "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  "attribution": "© OpenStreetMap contributors"
}
```

**别换高德/百度底图。** 我们的数据是 WGS-84（GPSLogger / Dawarich 原样上报），
而国内底图是 GCJ-02 加偏坐标系，两者差约 500 米 —— 换过去之后每个点都会偏移，
而且是那种"看起来差不多其实全错了"的偏移。容器内实测 OSM 瓦片可直连，不用走代理。

⚠️ 折叠屏/窗口大小变化时地图的「自动缩放到数据范围」不会重算，会掉回世界视图。
所以 `view` 里除了 `id: "fit"` 还写了 `lat/lon/zoom` 作为兜底，
换城市住的话记得改这三个值（在 `03-location.json` 的地图面板里）。

### 交通方式：是 Dawarich 猜的，不是我们测的

面板标题里就写了「Dawarich 推断」—— 这不是客气话。
`collect_phone.py` 只是把 Dawarich `/tracks` 返回的 `mode_timeline[].emoji`
翻译成名字（🚗→driving、🚇→subway…，映射表在 `collect_phone.py:MODE_EMOJI`）。
模式本身是 Dawarich 按 **GPS 点的速度/加速度**推断的。

实测它在地铁上必然出错：地下没有 GPS，点稀疏又漂移，一段纯地铁通勤
被判成了 cycling + walking + driving 三段。所以**别把这个面板当成事实**。

判断某一段到底是什么，看旁边的「交通段明细」那张表：
它把每个移动段的起止时间，和那个时段手机前台开着的应用关联起来了。
写着 driving 但同一时段前台是 Shanghai Metro，那显然是地铁不是开车。
（⚠️ 手机事件是 2026-10-14 傍晚才接进来的，更早的时间段这一列是空的。）

### 「近 30 天出门」用的是停留点间隔，不是 Dawarich 的移动段

这两个口径差得很远：14 号那趟 110 分钟的通勤，按 Dawarich 判为「移动」的段
只有 50.8 分钟（其余时间它认为你在原地）。所以面板改成用**停留点的间隔**反推：
离开上一处 → 到达下一处，那段时间人一定在路上。

```sql
WITH v AS (
  SELECT day_local, json_extract(meta_json,'$.end_utc') AS end_utc,
         LEAD(ts_utc) OVER (PARTITION BY day_local ORDER BY ts_utc) AS next_start
  FROM events WHERE domain='location' AND metric='visit')
SELECT ROUND(SUM((julianday(next_start) - julianday(end_utc)) * 24.0), 1) AS h
FROM v WHERE next_start IS NOT NULL
```

⚠️ 一天只有一个停留点时算不出来，那天记 0。

### 时间黑洞：两个来源，链路是分两段接上的

`blocks.blackhole` 这个字段在很长一段时间里**没有任何生产代码把它设成 True** ——
面板上那两个「时间黑洞」永远是 0，看着像坏了，其实是没数据。现在有两处会写它：

1. `normalize.py` 按 `config.yaml` 的 `blackhole:` 节判（应用名 / URL / 窗口标题关键词，
   小写子串匹配），在时间块折叠**之后**打标；
2. AI 在日记正文里标的 `[blackhole:: true]` 由 `generate_journal.py` 回写 ——
   按**时间重叠**匹配（AI 会把小块合并成 6–18 个大块，和数据库里的块不是一一对应）。

⚠️ 回写走 `POST /blocks/mark-blackhole`，必须在 normalize **之后**执行 ——
`POST /blocks` 是「按天删旧再插」，会把已有的黑洞标记清掉。
流水线顺序（collect → normalize → journal）本来就满足。

⚠️ 两个来源都可能判不出来（那天确实很自律，或者关键词没覆盖到），
那面板就是 0。**那是实话，不是坏了。**

### 「今日应用时长 / 今日浏览器」为什么和「今日记录时长」对不上

这两个数来自**两条完全不同的路径**：

| 面板 | 数据来源 | 什么时候有数 |
|---|---|---|
| 今日记录时长、分类趋势 | `blocks` 表 ← `normalize.py` **直接读 ActivityWatch** | 跑一次 normalize 就有 |
| 今日应用时长、今日浏览器 | `events` 表 ← `collect_desktop.py` 写入 | 得先跑 collect-desktop |

所以「记录时长 5.94h 有数、应用时长 0.00」**不是 bug**：那天跑了 normalize
没跑 collect-desktop。而且流水线到 23:00 才处理"今天"（`run_daily.ps1` 的水位线逻辑），
白天打开面板看到的就是这个组合。

### 已知数据缺陷：手机事件里的 `%APPNAME`

`phone/app_switch` 里混着一批 `value_text = '%APPNAME'` 的行 ——
Tasker 的变量没被替换掉就直接上报了。**手机应用 Top 15 那张图已经把它过滤掉**
（`WHERE value_text NOT LIKE '%APPNAME%'`），明细表里则显示成
`(Tasker 变量没展开)`，免得你以为数据脏了。
根治要在手机的 Tasker 配置里改，不是这边的事。

### ⚠️ 两个「AI 花费」差两个数量级，都对

这是本项目里最容易误判的一个数，所以单独说：

| 面板 | 数值（实测 2026-10-14） | 它统计的是什么 |
|---|---|---|
| 概览 · **今日本流水线花费** | ¥0.21 | **只有本流水线**写日记时自己的 LLM 调用 |
| AI 用量 · **今日实际花费** | **¥22.97** | **账号真被扣掉的钱**（所有工具） |

差 109 倍。原因：白天用得最多的是 **Claude Code**，它跑的是同一个 DeepSeek key、
扣的是同一个余额，但它**不会**往我们的 `cost_ledger.jsonl` 里写任何东西。
台账记的是「这套系统自己花了多少」，不是「你花了多少」。

所以：**想知道花了多少钱，看「生命记录 · AI 用量」**；想知道这套系统自己贵不贵，
看概览页那个。两个数都不是 bug。

> AI 用量那张仪表盘上，精确的（平台账单、余额）和估算的（按项目的 token 分布）
> 在面板描述里分得很清楚。估算值来自 Claude Code 本地记录 ——
> 本机推不出官网那个精确数，所以它只用来出「花在哪个项目上」这个形状。

## 几条现成的查询

## 几条现成的查询

**过去 90 天，每天的深色时间（分类堆叠）**
```sql
SELECT
  day_local AS time,
  SUM(CASE WHEN category='Work'      THEN hours ELSE 0 END) AS "Work",
  SUM(CASE WHEN category='Study'     THEN hours ELSE 0 END) AS "Study",
  SUM(CASE WHEN category='Leisure'   THEN hours ELSE 0 END) AS "Leisure",
  SUM(CASE WHEN category='Life'      THEN hours ELSE 0 END) AS "Life",
  SUM(CASE WHEN category='Transport' THEN hours ELSE 0 END) AS "Transport",
  SUM(CASE WHEN category='Health'    THEN hours ELSE 0 END) AS "Health",
  SUM(CASE WHEN category='Social'    THEN hours ELSE 0 END) AS "Social"
FROM blocks
WHERE day_local >= date('now', '-90 days')
GROUP BY day_local
ORDER BY day_local
```

**每天的时间黑洞时长**
```sql
SELECT day_local AS time, ROUND(SUM(hours), 2) AS "Time sinks (h)"
FROM blocks
WHERE blackhole = 1 AND day_local >= date('now', '-60 days')
GROUP BY day_local
ORDER BY day_local
```

**各站点用时（装了浏览器扩展之后才有数据）**
```sql
SELECT day_local AS time,
       ROUND(SUM(value_num)/3600.0, 2) AS hours
FROM events
WHERE domain='web' AND metric='site_usage' AND value_text = 'github.com'
GROUP BY day_local ORDER BY day_local
```

**每日 AI 花费**
```sql
SELECT day_local AS time,
       ROUND(SUM(value_num), 4) AS "Cost (CNY)"
FROM events
WHERE domain='ai' AND metric='daily_cost'
GROUP BY day_local ORDER BY day_local
```

> 数据来自 `cost_guard.py emit-events`：它把 `state/cost_ledger.jsonl` **按天合计**
> 写成 `ai/daily_cost` 事件。流水线每次跑完都会调一次（`run_daily.ps1`）。
> 台账是 JSONL、Grafana 读的是 SQLite —— 中间没有这一步的话这条查询永远是空的。
>
> ⚠️ 每次调用的明细仍然只在 `cost_ledger.jsonl` 里（还有 hash 链），
> 这里只有**按天的合计**。
> ⚠️ 金额含调试/重跑的那些调用（台账就是这么记的），所以它反映的是真实支出，
> 不等于"生成的日记值多少钱"。实测差得不少：单次日记平均 ¥0.02，
> 而忙碌的一天（含反复调试）总额会到 ¥0.2 上下。
> ⚠️ 当天要有数得等夜里流水线跑完 —— 和 `blocks` 一样是"跑完才有"。
> ⚠️ 想让这个数**对上厂商账单**：核对的是**量级**，不是分毫。
>    DeepSeek 只提供余额接口、没有按日账单，所以精确对账做不到。
>    价格本身核对过：`config.yaml` 的 `pricing` 和官方价目表逐项一致
>    （缓存命中 0.02/0.04、未命中 1/2、输出 4/8 元每百万 token，空闲/高峰），
>    峰谷判定在 `scripts/common.py:is_peak`，高峰 = 北京时间工作日 9–12 点与 14–18 点。

**每日 Token 用量**（概览页同名面板）
```sql
SELECT strftime('%Y-%m-%dT%H:%M:%S+08:00', day_local) AS time,
       MAX(CAST(json_extract(meta_json,'$.cache_hit')  AS INTEGER)) AS "缓存命中",
       MAX(CAST(json_extract(meta_json,'$.cache_miss') AS INTEGER)) AS "缓存未命中",
       MAX(CAST(json_extract(meta_json,'$.output')     AS INTEGER)) AS "输出"
FROM events WHERE domain='ai' AND metric='daily_cost'
GROUP BY day_local ORDER BY day_local
```

> 一眼就能看出**钱花在哪**：输入被自动前缀缓存压得很低（命中价是未命中的 1/50），
> 输出是整价 —— 实测 97% 的成本来自输出 token。
> 想看更多字段（`calls` / `reasoning` / `peak_calls`）直接 `json_extract` 那个 meta，
> 它的内容是 `cost_guard.py emit-events` 写进去的。

**手机每天的解锁次数**
```sql
SELECT strftime('%Y-%m-%dT%H:%M:%S+08:00', day_local) AS time,
       COUNT(*) AS "Unlocks"
FROM events
WHERE domain='phone' AND metric='unlock'
GROUP BY day_local ORDER BY day_local
```

**某天的轨迹（地图面板用的就是这个）**
```sql
SELECT CAST(json_extract(p.value,'$[0]') AS REAL) AS lat,
       CAST(json_extract(p.value,'$[1]') AS REAL) AS lon
FROM events e, json_each(json_extract(e.meta_json,'$.path')) p
WHERE e.domain='location' AND e.metric='track_path'
  AND e.day_local = '2026-10-14'
ORDER BY p.key
```

## 排障

### `database is locked`

SQLite 是「一写多读」，但读的一方需要能访问 `-wal` 和 `-shm` 文件。
这个 compose 挂的是整个 `state/` 目录且可写，正常情况下不会出现。

如果真遇到了：
1. 确认挂载是 `rw` 不是 `ro`（WAL 模式下只读挂载会出问题）
2. 检查是不是有采集脚本卡住没退出：`netstat -ano | grep 18787`
3. 临时办法：把 `events.db` 连同 `-wal`/`-shm` 一起复制一份给 Grafana 读，
   代价是数据有延迟

### 看不到 blocks 表 / 表是空的

`blocks` 表是 `normalize.py` 写进去的。**先确认那天的 `normalize.py` 跑没跑**，
再确认 `state/events.db` 里真的有行：

```bash
curl -H "X-API-Key: <你的key>" "http://127.0.0.1:18787/blocks/daily?from=2026-01-01"
```

如果行数明显偏少（比如一整天才 5 行），那是 `normalize.py` 的合并逻辑问题，
不是 Grafana 的 —— 见 `docs/运维手册.md` 里「一天的活动只剩几个小时」那一节。

> 历史上这里踩过一个坑：`MIN_BLOCK` 原来是个**过滤器**，短于 6 分钟的时间块
> 被直接丢掉，实测一天 2.34 小时的活动只有 0.41 小时入账。现在改成了
> **折叠**（并进相邻块，总量守恒），`MIN_BLOCK` 只控制颗粒度。

### 改了数据源的 uid 之后 Grafana 起不来（重启循环）

症状：容器一直是 `Restarting`，日志里是

```
level=error msg="Failed to provision data sources"
  error="Datasource provisioning error: data source not found"
Error: ✗ invalid service state: Failed, ... 1 failed: [starting module provisioning]
```

**原因**：provisioning 是按**名字**去更新数据源的。库里那个数据源叫 `LifeLogging`、
uid 是老的那个，而配置里写了一个新 uid —— 两边对不上，Grafana 直接崩在启动阶段。
改 uid 不是「改一下就行」的操作。

**修**：正确顺序是**先删数据源，再改 uid，最后重启**。

```bash
# 1. 先让 Grafana 能跑起来（把 uid 那行改回去，或者保持现状先重启）
docker restart lifelogging-grafana

# 2. 删掉旧数据源
curl -u admin:admin -X DELETE \
  "http://localhost:3001/api/datasources/uid/<旧uid>"

# 3. 现在才改 sqlite.yaml 里的 uid，然后重启
docker restart lifelogging-grafana
```

> 当前用的 uid 是 `lifelogging-sqlite`，**仪表盘 JSON 全靠它引用数据源**。
> 这个 uid 一旦变，所有面板会静默失效（不报错，只是查不出数据）。

### 想连别的工具

同一张表，Superset / Metabase / 甚至 Excel 的 Power Query 都能直接读。
数据格式是标准的长表，没有私有格式。
