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

数据源已通过 provisioning 自动配好，进去直接写查询。

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

`blocks` 表是 `normalize.py` 写进去的，而它**只写时长 ≥ 6 分钟的时间块**
（`MIN_BLOCK`）。如果你刚开始用，切窗口很频繁，可能一块都没有。

正常用一整天之后就有了。手动验证：
```bash
curl -H "X-API-Key: <你的key>" "http://127.0.0.1:8787/blocks/daily?from=2026-01-01"
```

### 想连别的工具

同一张表，Superset / Metabase / 甚至 Excel 的 Power Query 都能直接读。
数据格式是标准的长表，没有私有格式。
