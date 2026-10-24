# 服务器侧：发布手表路线（GPX / FIT）

**它解决什么。** 中继 24 小时收手机传上来的锻炼文件，但「文件 → 站点」那一步
原来只在笔记本的 `run_daily.ps1` 里。笔记本一关机，那天的 LIFE 地图上就没有
手表虚线 —— 哪怕文件早就躺在服务器的 `/var/lib/lifelogging/gpx|fit` 里。

现在服务器每 15 分钟把这两个目录解析一遍，写成一份 `watch` 类型的原始文档：

```
life/raw/watch/<day>.json   →   GET /api/life/raw/watch/<day>
```

站点读地图时把它和笔记本发布的 `events` 合并，按**开始时间**去重
（同一次锻炼的 FIT / GPX 开始时间逐秒相同），所以只会画一条。

**它不做什么。** 不写 `events.db`，不碰笔记本的 `state/`，也不做聚合
（steps / 心率 / workouts 仍然在笔记本的 `publish_life_site.py` 里）。
笔记本那条链路照常跑，两边同时发布也只会重合、不会翻倍。

## 配置

```bash
sudo tee /etc/lifelogging/watch.env >/dev/null <<'EOF'
LIFE_INGEST_TOKEN=和 Worker secret 相同的那个串
EOF
sudo chmod 600 /etc/lifelogging/watch.env
```

可选变量（写进同一个文件）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIFE_API_BASE` | `https://cms.liveinpassion.me` | Worker 地址 |
| `RELAY_DATA_DIR` | `/var/lib/lifelogging` | 和中继的 `RELAY_DATA_DIR` 一致 |
| `WATCH_WINDOW_DAYS` | `30` | 只发布最近 N 天 |
| `WATCH_TZ` | `Asia/Shanghai` | 分天口径，必须和笔记本的 `publish_life_raw` 一致 |

> `LIFE_INGEST_TOKEN` 就是笔记本那边 `publish_life_raw.py` 用的同一个令牌
> （Worker 的 secret）。**不要**新建一个。

## 安装

```bash
sudo cp /opt/lifelogging/repo/server/watch-publisher/lifelogging-watch-publish.{service,timer} \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifelogging-watch-publish.timer
```

手动跑一次看看：

```bash
sudo RELAY_DATA_DIR=/var/lib/lifelogging LIFE_INGEST_TOKEN=$(sudo grep LIFE_INGEST_TOKEN /etc/lifelogging/watch.env | cut -d= -f2) \
  /opt/lifelogging/venv/bin/python /opt/lifelogging/repo/server/watch-publisher/publish_watch.py --dry-run
```

期望输出 `扫了 N 个文件…合并后 M 条`。确认没问题就去掉 `--dry-run`。

## 验证

```bash
# 服务端日志
sudo journalctl -u lifelogging-watch-publish.service -n 30 --no-pager

# 站点侧（换成一个确实有锻炼的日期）
curl -s https://cms.liveinpassion.me/api/life/raw/watch/2026-10-23 | head -c 400
```

返回 `{"v":1,"kind":"watch","id":"2026-10-23","format":"json","data":{"day":…,"events":[…]}}`
就说明通了。

> ⚠️ **部署前要先让 Worker 认识 `watch` 这个 kind**，否则 ingest 会返回
> `400 invalid kind`。站点仓库的 `worker/src/routes/life_raw.js` 里那个
> `KINDS` 集合是白名单。

## 幂等与状态

`/var/lib/lifelogging/watch-published.json` 记着「每天上一次发布的内容 sha1」，
内容没变就跳过，不会每 15 分钟往 R2 写一遍。

> 删掉这个文件只会导致下次全量重发一次，不会丢数据。

## 它和中继的关系

只读中继的目录，**不依赖中继进程是否在跑**。所以中继重启、或者手机还没传文件，
它都只是「这次没东西可发」，不会报错。
