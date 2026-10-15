# 手机使用事件中继（Linux 服务器）

手机上报的「解锁 / 应用切换」事件先落到这台常驻服务器上，
笔记本电脑 23:00 跑流水线时再来拉。

```
位置：手机 ──随时──> Dawarich（本服务器） ──23:00──> 笔记本 collect_phone.py
使用：手机 ──随时──> 本中继  （本服务器） ──23:00──> 笔记本 collect_phone_events.py
```

---

## 为什么不让手机直接发给笔记本

笔记本一天只醒几小时（实测 2026-10-13：24 小时里 ActivityWatch 只采到
**3.4 小时**，其余时间锁屏或睡眠）。Tasker 的 HTTP 请求**没有队列**——
电脑睡着时那一发直接失败，数据静默丢失，你事后完全看不出来。

服务器是 24 小时开着的（手机的 GPS 本来就一直往它传），
所以让手机只对**一台始终在线**的设备说话，笔记本醒着的时候主动来拉。

---

## 部署

### 1. 代码

**用 git clone，别手工拷** —— 布局和理由见 [`../README.md`](../README.md)：

```bash
sudo mkdir -p /opt/lifelogging
sudo git clone https://github.com/HuangWeiLong-dot/LifeLogging.git /opt/lifelogging/repo
```

中继本身是**纯标准库**的，不需要 venv。

### 2. 数据目录

```bash
sudo mkdir -p /var/lib/lifelogging
```

**不用建专用用户。** 这台机器上一直用 root 跑，而 unit 里已经写了
`User=root`。加固放在 `ProtectSystem=strict` / `ProtectHome=yes` /
`CapabilityBoundingSet=` / `ReadWritePaths=/var/lib/lifelogging` 上 ——
这些**即使以 root 运行也生效**：绑成 root 也进不了 `/root`，也只能写那一个目录。

（unit 里 `User=` 是显式写的，不是省略。省略也等于 root，但读的人看不出来。）

### 3. 生成密钥

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

写进环境文件，**权限收紧**（只有 root 读得到）：

```bash
sudo tee /etc/lifelogging/relay.env >/dev/null <<'EOF'
RELAY_API_KEY=把上面生成的串粘进来
EOF
sudo chmod 600 /etc/lifelogging/relay.env
```

> ⚠️ 不要用 README 里的占位串，也不要把真正在用的密钥提交进 git。
> 服务启动时会检查，发现是占位串直接拒绝启动。

### 4. 绑定地址 = Tailscale 地址

```bash
tailscale ip -4        # 例如 100.90.12.34
```

把单元文件里那行改掉：

```bash
sudo sed -i 's|^Environment=RELAY_HOST=.*|Environment=RELAY_HOST=100.90.12.34|' \
    /tmp/lifelogging-relay.service
```

> ⚠️ **别写 `0.0.0.0`。** 那会把公网和局域网一起开出去。
> 绑 Tailscale 地址 = 只有你 tailnet 里的设备能访问。

### 5. 装并启动

```bash
sudo cp /tmp/lifelogging-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifelogging-relay
sudo systemctl status lifelogging-relay
sudo journalctl -u lifelogging-relay -f
```

### 6. 验证

在服务器上（或任何连了 tailnet 的设备上）：

```bash
KEY=$(sudo grep RELAY_API_KEY /etc/lifelogging/relay.env | cut -d= -f2)
IP=$(tailscale ip -4)

curl -s "http://$IP:8788/health"
# {"status":"ok","tz":"Asia/Shanghai","today":"2026-10-14","today_events":0}

curl -s -H "X-API-Key: $KEY" "http://$IP:8788/events?day=2026-10-14"
# {"day":"2026-10-14","count":0,"events":[]}

# 塞一条假的，确认写进去了
curl -s -X POST "http://$IP:8788/ingest/phone" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"ts_utc":"2026-10-14T04:00:00Z","domain":"phone","metric":"unlock","source":"selftest"}'
# {"inserted":1,"received":1}

curl -s -H "X-API-Key: $KEY" "http://$IP:8788/events?day=2026-10-14"
```

看到 `inserted:1` 就成了。**验证完记得把那条 selftest 删掉**：

```bash
sudo sed -i '/selftest/d' /var/lib/lifelogging/phone-2026-10-14.jsonl
```

---

## 升级

代码是 git clone 到 `/opt/lifelogging/repo` 的，升级就是：

```bash
sudo /opt/lifelogging/repo/server/deploy.sh
```

它快进到 `origin/master`、按改动范围决定要不要重启中继，并且会告诉你
这次更新了哪些 commit。细节见 [`../README.md`](../README.md)。

> ⚠️ **确认新版真的生效了，别只看 `/health`** —— 新旧版本的 `/health` 长得
> 一模一样。挑一个只有新版才有的路由去打：
>
> ```bash
> curl -H "X-API-Key: $KEY" http://<服务器>:8788/ask/pending
> # 新版：一个数字（0 也正常）    旧版：{"error": "not found"}
> ```

---

## 让笔记本电脑来拉

编辑 `config/config.yaml`：

```yaml
sources:
  phone_relay:
    enabled: true
    api_base: "http://100.90.12.34:8788"     # 服务器地址，就是上面那个
    api_key: "把 /etc/lifelogging/relay.env 里的值粘过来"
```

然后：

```bash
python scripts/collect_phone_events.py --date today
```

应该看到「从服务器拉到 N 条，写入 M 条」。之后 23:00 的流水线会自动跑这一步。

---

## 手机侧（Tasker）

两个 Profile，都 POST 到 `http://<服务器Tailscale地址>:8788/ingest/phone`：

| Profile | Trigger | Body |
|---|---|---|
| 屏幕解锁 | `Event → Display → Display Unlocked` | `{"ts":%TIMES,"domain":"phone","metric":"unlock","source":"tasker"}` |
| 应用切换 | `Event → App Changed`（需 Usage Access） | `{"ts":%TIMES,"domain":"phone","metric":"app_switch","value_text":"%app_package","source":"tasker"}` |
| **屏幕亮** | `Event → Display → Display On` | `{"ts":%TIMES,"domain":"phone","metric":"screen_on","source":"tasker"}` |
| **屏幕灭** | `Event → Display → Display Off` | `{"ts":%TIMES,"domain":"phone","metric":"screen_off","source":"tasker"}` |

> **后两个（Display On / Off）是「屏幕使用时间」和「应用时长准确值」的前提。**
> 只有解锁事件的话，「最后一个应用」没有下一跳、时长无从得知，
> 「睡前刷了半小时」这种整段都进不了账。有了 `screen_off` 才有终点。
>
> 屏幕时间本身也是**实测**出来的：`Σ(screen_off − screen_on)`。
> 配不成对的（只有 on、或者连续两个 on）会**被丢掉而不是猜** ——
> 猜出来的数字会污染「屏幕时间」这个指标。
>
> ⚠️ **中途才加上这两个 Profile 的那一天**：屏幕事件之前/之后的时段没有
> 屏幕数据，那段会退回按解锁事件估算（较弱），**不会归零**。
> 实测踩过这个坑 —— 19:40 才加上，17:05 起的两百多次切换全被裁成 0，
> 榜单变成一串 0 分钟，看起来像功能没生效。

HTTP Request 配置：Method `POST`，Headers `X-API-Key: <key>`、
`Content-Type: application/json`。

> ⚠️ **`%app_package` 是实测确认的，别改回 `%APPNAME`。**
> `App Changed` 这个事件暴露的局部变量是 `%app_package`（包名）。
> `%APPNAME` 在这个事件里**不存在** —— Tasker 不会报错，而是把它**原样**
> 发出去，服务器上看到的就是字面量字符串 `"%APPNAME"`。
> （实测踩过：五条记录全是 `"value_text": "%APPNAME"`。）
>
> 好在包名正是我们要的：`normalize.PHONE_APPS` 那张映射表就是按包名建的。

### 手表数据：上传 Gadgetbridge 的导出文件

同一个服务也负责收手机上传的 **Gadgetbridge 导出库**（Garmin 手表的数据）。
本机 23:00 用 `scripts/collect_watch.py` 把它取回来，
紧接着 `middleware/ingest_gadgetbridge.py` 读它。

| 端点 | 用途 |
|---|---|
| `POST /upload/gadgetbridge.db` | 上传（body 是文件内容） |
| `GET /files/gadgetbridge.db` | 下载，供笔记本拉取 |

**手机侧（Tasker）**：建一个定时任务，动作是 **Net → HTTP Request**：

| 字段 | 值 |
|---|---|
| Method | `POST` |
| URL | `http://<服务器Tailscale地址>:8788/upload/gadgetbridge.db` |
| Headers | `X-API-Key:<key>`（一行） |
| **File To Send** | Gadgetbridge 导出的那个 `.db` 的完整路径 |

> ✅ **不要往 Body 里填内容** —— 要用 **File To Send** 那个字段。
> 用 Body 的话 Tasker 会把文件当文本字段处理，传上去的东西
> **看起来有几十 KB、时间戳也对，但 SQLite 打不开**。
> `collect_watch.py` 会检查开头的 `SQLite format 3` 魔数并明确报错，
> 不会让它悄悄流到下游。
>
> ⚠️ **Content-Type 可以留空也可以设 `application/octet-stream`，两种都认** ——
> 中继会自己判断是不是 multipart 并解开一层。

**这个任务什么时候跑？** 建议设成 **每天 22:00**（比笔记本的 23:00 流水线早一小时）。
放在充电时或连上 WiFi 时也合理。

> 💡 **漏跑一次不要紧。** Gadgetbridge 的导出是**全量快照**（整个库），
> 不是增量流 —— 下一次传上去的还是完整的。这也是为什么「Tasker 没有队列」
> 在这里不构成问题（跟位置数据不一样）。

> 🔒 **文件名走白名单**（只认 `gadgetbridge.db`）。这是个对外的写端点，
> 不做白名单就是任意路径写入。另外写入是**原子**的（先写 `.tmp` 再 rename），
> 上传断线时旧的那份完整的还在；上一版会留成 `.prev` 以便回退。

**排查**：`curl <服务器>:8788/health` 会带上文件的大小和最后更新时间 ——
看到时间没变就说明手机那边没在传。

**`%TIMES` 直接写就行，不用转。** 中继收 Unix 秒（`ts` 字段），
也会替手机转成 ISO —— 这是在**服务器上**做一次的事，
比在 Tasker 里挂一段 JavaScriptlet 可靠得多
（后者是整个配置里最容易出错、最难排查的一步）。

> `%TIMES` 是 Unix **秒**。想发 ISO 也行（用 `ts_utc` 字段），两种都认。
> 毫秒值会被中继丢掉而不是猜 —— 静默写进去一个 1970 年的时间戳更糟。

> 「Display Unlocked」那个 Profile **不是可选项**：前台时长是靠「两条相邻
> 应用切换之间的时间差」推出来的，而解锁事件用来截断它 ——
> 没有解锁事件，「切到浏览器 → 锁屏放口袋 → 两小时后再解锁」会把两小时
> 算成浏览器。详见 `docs/扩展路线图.md` 手机那一节。

### 每日提问：问题下发 + 答案回收

笔记本生成的「每日提问」需要有地方放，好让手机在笔记本睡着时也能取 ——
服务器 24 小时开着，本来就有这个职责。生成见 `scripts/generate_questions.py`，
回写见 `scripts/apply_answers.py`。

| 端点 | 用途 |
|---|---|
| `POST /questions` | 笔记本把当天的问题推上来（JSON） |
| `GET /ask/pending` | 手机轮询「有没有题」—— **纯文本，就一个数字** |
| `GET /ask` | 手机浏览器打开的表单页（做了 / 没做 + 备注） |
| `POST /answer` | 答案落成事件，随手机使用事件一起被笔记本拉走 |

**为什么 `/ask/pending` 返回纯文本而不是 JSON。**
Tasker 收得到 HTTP 响应体，但**不会解析 JSON** —— 那要挂 JavaScriptlet，
而那段 JS 是整个手机配置里最容易出错、最难排查的一步（理由和上面
`%TIMES` 那段完全一样）。返回一个数字，Tasker 直接比字符串就行。

**手机侧（Tasker）**：建一个时间 Profile，**每 30 分钟、限 07:00–12:00**，
动作是 **Net → HTTP Request**：

| 字段 | 值 |
|---|---|
| Method | `GET` |
| URL | `http://<服务器Tailscale地址>:8788/ask/pending` |
| Headers | `X-API-Key:<key>`（一行） |

然后接一个 **If  %http_data  ≠  0**，里面放一条通知：

    今天有 %http_data 条要问

这条通知的点击动作设成打开 URL（Tasker 的 `Browse URL` 动作）：

    http://<服务器Tailscale地址>:8788/ask?key=<key>

> ✅ **读一次就标记。** `/ask/pending` 第一次返回条数，之后一律返回 `0` ——
> 所以每 30 分钟轮询不会把同一个通知弹十遍，而手机侧**不需要保存任何状态**。
> 想让它重新提醒：`GET /ask/pending?again=1` —— 它只**清**标记、自己不算一次
> 读取，所以你能看到还剩几条，而手机下次轮询照样会提醒。

> ⚠️ **`/ask` 是在浏览器里打开的，key 只能走查询参数。**
> 普通导航请求没法带自定义请求头。代价是这把 key 会进手机的浏览器历史 ——
> 走的是 Tailscale 内网，可以接受，但你要知道这件事。
> **其它端点一律仍然只认请求头**，只有这一条开了口子。

> ⚠️ **中继不会主动推给手机。** 没有推送、没有长轮询、没有队列 ——
> 手机永远是发起方。所以「早上问你」是手机定时来取，而不是服务器推过去。
> 笔记本睡着不影响取题，这是把问题放在服务器上的**全部理由**。

---

## 运维

```bash
sudo systemctl restart lifelogging-relay
sudo journalctl -u lifelogging-relay -n 50

# 数据长这样，一天一个文件
ls -la /var/lib/lifelogging/
# phone-2026-10-14.jsonl      手机事件（**提问的答案也混在这里**，domain=question）
# questions-2026-10-14.json   当天要问的问题（笔记本推上来的）
# questions-2026-10-14.seen   「已经提醒过手机了」的标记

# 磁盘：一天几百条、每条约 150 字节，一年不到 20 MB，不用管
du -sh /var/lib/lifelogging/
```

**备份**：`/var/lib/lifelogging/` 是原始数据的**唯一**副本（笔记本拉走的是
事件表里的一份副本，但中继这份没被删过）。跟 Dawarich 的备份放一起就行。

> 其中只有 `phone-*.jsonl` 是**不可再生**的 —— 提问的答案就混在里面，
> 那是你自己的话，弄丢了就没了。`questions-*.json` 和 `.seen` 都是派生物
> （重新生成一次就有），不用特意保。

---

## 设计上有意为之的几件事

- **没有数据库。** 一天一个 JSONL，`cat` 就能看，坏了不会整库打不开。
- **不做去重。** 笔记本侧的事件表有唯一索引，重复拉取是幂等的
  （`(source, domain, metric, ts_utc, value_text)`）。中继只需要保证「写进去就不丢」。
  内存里有个当天已见集合挡 Tasker 的连续重试，但进程重启后失效 —— 那是刻意的，
  别为它引入持久化状态。
- **`/health` 不鉴权。** 它要能被探活直接打，且只暴露「今天几条」，不含事件内容。
- **`/ask` 额外认 `?key=`。** 全文件唯一的例外，因为那一页要在浏览器里打开，
  而浏览器导航请求带不了自定义头。代价写在上面那一节里了。
- **不主动推送给手机。** 没有推送、没有队列 —— 手机永远是发起方。这不是偷懒：
  引入推送就要引入一套会在半夜静默失效的东西，而这个项目一路都在跟那类
  东西打架。
- **按本地日期分文件。** 分天算法必须和笔记本侧 `events_api.py` 的 `_day_local()`
  一致，否则两边切出来的「天」不一样，拉取会漏。改一边记得改另一边。
- **单条和批量都收。** Tasker 一次发一条；别的工具可能一次发一天。
