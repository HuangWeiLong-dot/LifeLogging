# 服务器侧：24h 实时快照（`/api/life/now`）

**它解决什么。** 笔记本上的 `scripts/run_live.ps1` 是本机链路：笔记本一关，
站点的 `WHAT I'M DOING NOW` 就停在最后一份。位置在 Dawarich、手机事件和实时问答
在中继、手表导出也在中继 —— 这些在 24h 服务器上都能拿到，没有理由跟着笔记本一起停。

这个服务每 2 分钟组装一份和笔记本同形状的 now 快照：

| 字段 | 服务器上的来源 | 笔记本关机后 |
|---|---|---|
| `location` | Dawarich：`collect_phone.fetch_points` + `derive_stays` | 继续更新（含坐标） |
| `phone` | 中继 `/events?day=` | 继续更新 |
| `qa` | 中继 `/qa?day=` | 继续更新 |
| `watch` | 中继上的 `gadgetbridge.db`，直接解析今日 steps / HR | 手机一传新导出就更新 |
| `desktop` | 中继 `/desktop`（笔记本每 10 分钟推的快照） | 停在最后一次，卡片显示 `LAST SEEN` |

**和笔记本的任务不打架。** 发布前它会先 GET 当前 `/api/life/now`，电脑 / 手表
只保留两边里更新的那一份：笔记本开着时本机那份通常更新；关机后服务器这份才会
接上。位置 / 手机 / 问答始终用服务器即时算出来的。

**手表最后更新时间。** 快照里的 `watch.pushedAt`：服务器这条来自 relay 上
`gadgetbridge.db` 的 mtime（手机最后一次上传）；笔记本那条来自本机 ingest 时间。
前端卡片的 `UPDATED HH:MM · Xm AGO` 读的就是它。

**翻译。** 和笔记本一样，发之前用 `translate.Translator` 把中文问答 / 窗口标题 /
地名翻成英文，缓存写到 `state_dir`；`max_calls=6` 一轮。

**边界。** 电脑窗口只能显示最后一次中继快照（ActivityWatch 在本机）；日聚合里的
桌面分类时长也仍然依赖笔记本。服务器这条只负责 NOW 条，不重写 `/api/life` 的日图。

## 部署

前置：仓库已经在 `/opt/lifelogging/repo`，venv 在 `/opt/lifelogging/venv`，
中继 `lifelogging-relay` 在跑（见 `../README.md`）。

```bash
# 1. 代码（把 repo 更新到 master；这个服务是 oneshot，不需要重启）
sudo /opt/lifelogging/repo/server/deploy.sh

# 2. 站点写入 token（和 Worker secret / 笔记本上的 LIFE_INGEST_TOKEN 相同）
sudo sh -c 'echo "LIFE_INGEST_TOKEN=<同一个值>" > /etc/lifelogging/live-now.env'
sudo chmod 600 /etc/lifelogging/live-now.env

# 3. 翻译用的 DEEPSEEK_API_KEY 已经由 close-day.env 提供；确认它存在：
sudo systemctl cat lifelogging-close-day.service | grep EnvironmentFile

# 4. 中继上的手环库路径（服务器 config 里没有的话，脚本会回退到这两个默认位置）
#    /var/lib/lifelogging/gadgetbridge.db
#    <state_dir>/gadgetbridge.db
#    如果中继的 RELAY_DATA_DIR 不是 /var/lib/lifelogging，在服务器 config 里写：
#      sources:
#        gadgetbridge:
#          enabled: true
#          db_path: /var/lib/lifelogging/gadgetbridge.db

# 5. unit / timer
sudo cp /opt/lifelogging/repo/server/live-now/lifelogging-live-now.{service,timer} \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifelogging-live-now.timer

# 6. 立刻跑一次并看日志
sudo systemctl start lifelogging-live-now.service
journalctl -u lifelogging-live-now.service -n 30 --no-pager
curl -s https://cms.liveinpassion.me/api/life/now | head -c 400
```

### 本机先干跑

在笔记本上（同一份 config、venv）可以先不部署、不发：

```bash
.venv/Scripts/python.exe server/live-now/live_now_server.py \
  --dry-run --print-json --no-translate --api-base http://127.0.0.1:9
```

`--api-base http://127.0.0.1:9` 故意让「读取当前快照」失败，这样能看到服务器
自己算出的电脑 / 手表，而不是被笔记本的当前快照合并后的结果。

## 排障

- **`✗ environment variable LIFE_INGEST_TOKEN is not set`** → 第 2 步的 env 文件
  没建，或者 unit 没读到；`sudo systemctl show lifelogging-live-now.service | grep Environment`。
- **`translation failed`** → `DEEPSEEK_API_KEY` 不在 close-day.env，或余额/额度。
  失败时仍会发原文，不会把整条快照丢掉。
- **手表还是昨天** → 手机没有把新的 Gadgetbridge 导出传到中继；`curl` 中继
  `/health` 看 `file_gadgetbridge.db.updated`。传了之后下一轮（≤2 分钟）会更新。
- **电脑窗口不动** → 正常：笔记本关机后 `/desktop` 只留最后一份。这是唯一
  无法搬去服务器的数据源。
- 停掉：`sudo systemctl disable --now lifelogging-live-now.timer`。
