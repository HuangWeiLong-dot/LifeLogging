# 服务器侧：把手机事件直接发布到站点

**它解决什么。** 手机（Tasker / LifeBridge）24 小时往中继发事件，但
「中继 → 站点」原来只有笔记本上的 `collect_phone_events.py → events.db →
publish_life_*`。笔记本一关机，LIFE 页面的手机卡片和归档里的 EVENTS 就停了。

现在服务器每 5 分钟直接读中继落盘的 `phone-<day>.jsonl`：

```
life/raw/phone/<day>.json   →  Worker 在 GET /api/life/raw/events/<day> 时合并
POST /api/life/phone/ingest →  只合并当天的 phone 字段（及手机-only 的七维）
```

**它不做什么。** 不写 `events.db`，不碰笔记本的 `state/`，也不发布桌面 /
位置 / 健康 / 日记 —— 那些仍然需要笔记本。笔记本那条链路照常跑，两边同时
发布只会重合、不会翻倍（Worker 按 `(ts_utc, domain, metric, value_text)` 去重）。

## 配置

复用 watch-publisher 的那份 env 即可（令牌是同一个）：

```bash
sudo tee /etc/lifelogging/watch.env >/dev/null <<'EOF'
LIFE_INGEST_TOKEN=和 Worker secret 相同的那个串
EOF
sudo chmod 600 /etc/lifelogging/watch.env
```

可选变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIFE_API_BASE` | `https://cms.liveinpassion.me` | Worker 地址 |
| `RELAY_DATA_DIR` | `/var/lib/lifelogging` | 和中继的 `RELAY_DATA_DIR` 一致 |
| `PHONE_WINDOW_DAYS` | `30` | 只发布最近 N 天 |
| `PHONE_TZ` | `Asia/Shanghai` | 分天口径，必须和笔记本一致 |

> `LIFE_INGEST_TOKEN` 就是笔记本 `publish_life_raw.py` 用的同一个令牌
> （Worker 的 secret）。**不要**新建一个。

## 安装

```bash
sudo cp /opt/lifelogging/repo/server/phone-publisher/lifelogging-phone-publish.{service,timer} \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifelogging-phone-publish.timer
```

手动跑一次看看：

```bash
sudo RELAY_DATA_DIR=/var/lib/lifelogging \
     LIFE_INGEST_TOKEN=$(sudo grep LIFE_INGEST_TOKEN /etc/lifelogging/watch.env | cut -d= -f2) \
     /opt/lifelogging/venv/bin/python \
     /opt/lifelogging/repo/server/phone-publisher/phone_publisher.py --dry-run
```

期望输出 `扫了 N 天…待发 X 份原始文档 / Y 天聚合`。确认没问题就去掉 `--dry-run`。

## 验证

```bash
sudo journalctl -u lifelogging-phone-publish.service -n 30 --no-pager

# 归档（EVENTS 页会合并它）
curl -s https://cms.liveinpassion.me/api/life/raw/phone/2026-11-16 | head -c 300

# 聚合（手机卡片）
curl -s https://cms.liveinpassion.me/api/life | \
  python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["days"][-1]["day"], d["days"][-1]["phone"])'
```

> ⚠️ **部署前要先让 Worker 认识 `phone` 这个 kind、并上线
> `/api/life/phone/ingest`**，否则原始文档会 `400 invalid kind`，
> 聚合会 `404`。站点仓库的 `worker/src/routes/life_raw.js` / `life.js`。

## 幂等与状态

状态在 `/var/lib/lifelogging/phone-published.json`：按天记录 JSONL 的
`mtime_ns` / `size` 和两份内容摘要。文件和内容都没变就整轮跳过；变了才重发。
发布失败**不**更新状态，下一轮自动重试。

## 隐私边界

和 `publish_life_raw.build_events` 完全一致：

- `phone/payment` 整条丢弃；
- `phone/notification` 的 `title` / `text` / `bigText` / `subText` /
  `summary` / `lines` 丢掉，只留 App 与条数，`meta.body` 标成 `redacted`；
- `phone/music` 去掉 160×160 封面 base64；
- URL / token / key 等凭证由 `redact_text` / `redact_obj` 遮盖。

这不是「生活数据脱敏」——坐标、应用名、听歌记录都原样保留，和归档的边界一致。
