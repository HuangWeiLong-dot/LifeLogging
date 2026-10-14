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
| **生命记录 · 概览** | 今日四个数字（记录时长 / 时间黑洞 / 应用 / 浏览器）+ 90 天分类趋势 + 应用与站点 Top 10 |
| **生命记录 · 时间与分类** | 分类堆叠、占比、应用/站点排行、逐条时间块明细、黑洞清单。顶部有「回看天数」变量 |
| **生命记录 · 位置** | 停留、轨迹点、活动范围、交通方式。数据来自 Dawarich |

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
是**分钟**；`location/tracked_points` 是**个数**；`ai/cost_reconcile` 是**元**。
写面板时别一律 `/3600`。

### 改完怎么验证

**别只看容器起没起来，也别只用 curl 试查询** —— 上面第 1、2 条都只有走完整前端才暴露。
仓库里带了个脚本，用无头 Chrome 真正渲染一遍，把每个面板的标题和值打出来：

```bash
node docker/grafana/verify_dashboards.mjs
# 或指定某个仪表盘
node docker/grafana/verify_dashboards.mjs http://localhost:3001/d/lifelogging-location
```

它会对每个仪表盘报 ✅/❌，看到 "No data" 或 "database is locked" 就是没配好。
只依赖 Node 18+（用内置的 WebSocket/fetch），不装 npm 包。

### ⚠️ 没有「每日 AI 花费」面板

下面示例查询里那条 `ai/daily_cost` 是**空的** —— 没有任何东西往事件表写这个 metric。
`reconcile_cost.py` 只写 `ai/cost_reconcile`（对账差额）。

真正的每次调用花费在 `state/cost_ledger.jsonl`，那是 JSONL 不是 SQLite，
这个数据源读不到。想在 Grafana 里看花费，得先让流水线把每日花费也写成事件 ——
现在还没有。

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

## 排障

### `database is locked`

SQLite 是「一写多读」，但读的一方需要能访问 `-wal` 和 `-shm` 文件。
这个 compose 挂的是整个 `state/` 目录且可写，正常情况下不会出现。

如果真遇到了：
1. 确认挂载是 `rw` 不是 `ro`（WAL 模式下只读挂载会出问题）
2. 检查是不是有采集脚本卡住没退出：`netstat -ano | grep 8787`
3. 临时办法：把 `events.db` 连同 `-wal`/`-shm` 一起复制一份给 Grafana 读，
   代价是数据有延迟

### 看不到 blocks 表 / 表是空的

`blocks` 表是 `normalize.py` 写进去的。**先确认那天的 `normalize.py` 跑没跑**，
再确认 `state/events.db` 里真的有行：

```bash
curl -H "X-API-Key: <你的key>" "http://127.0.0.1:8787/blocks/daily?from=2026-01-01"
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
