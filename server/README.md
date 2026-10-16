# 服务器侧

这台 Linux 机器上跑两个服务 + 一份共享配置：

| 服务 | 是什么 | 形态 |
|---|---|---|
| `lifelogging-relay` | 中继：手机上报入口 + 每日提问 + 日记中转 | 常驻（systemd service） |
| `lifelogging-close-day` | 关账 + 调 AI 写日记正文 | 定时（systemd timer，每 15 分钟） |
| `lifelogging-feishu-bot` | 飞书机器人：每日提问的收发通道（**可选**） | 常驻（systemd service） |
| `lifelogging-live-questions` | 白天根据当天数据生成「实时提问」（**可选**，随飞书机器人一起） | 定时（systemd timer，每 30 分钟） |

各自的具体说明： [`phone-relay/README.md`](phone-relay/README.md) · [`close-day/README.md`](close-day/README.md) · [`feishu-bot/README.md`](feishu-bot/README.md)

> **飞书机器人是可选的**，不装的话原来那套（Tasker 轮询 + `/ask` 表单）照常工作。
> 它是 2026-10-16 加的，为的是换一条**能主动推送**的通道 —— 原来的链路必须靠
> 手机定时来取，而手机侧那套 Tasker 配置（HTTP + Cloudflare 服务令牌 + 轮询）
> 是整个项目里最容易静默失败的一环。为什么没做成 QQ，见那份 README 的开头。

**手机怎么连进来** —— 这是另一个话题，而且有个绕不开的坑：
[`cloudflare-tunnel/README.md`](cloudflare-tunnel/README.md)。
Android 同一时刻只允许**一个** VPN 服务，所以手机上 Tailscale 和任何代理软件
（Hiddify 等）会互相踢掉 —— 而手机那侧的采集全走 Tailscale、Tasker 又没有队列，
开着代理的那段时间数据就是真丢了。那份文档讲的是把中继挂到 Cloudflare Tunnel
上、用 Access 只放你自己的设备，**手机从此不需要 Tailscale**。

---

## 代码从哪来：git clone，不是 scp

```bash
sudo git clone https://github.com/HuangWeiLong-dot/LifeLogging.git /opt/lifelogging/repo
```

**为什么改成 git 而不是手工拷文件。**

一开始中继只有一个 stdlib 文件，「拷一个文件」确实最简单。加了 close_day 之后，
服务器上要的东西变成「整个 `scripts/` + 一个 venv + 三个 unit + 一份 config」，
手工拷的代价超过了收益 —— 而且代价不是「麻烦」，是**错**：

- 拷漏一个文件，要等服务起来才报错
- 服务器上跑的和仓库里的会**悄悄漂移**，没有任何东西会提醒你

两条都实际发生过：unit 里的 `ExecStart` 曾指向 `/opt/lifelogging/close_day.py`
而代码在 `scripts/` 下；中继也曾在服务器上落后一个版本，直到新路由返回 404
才被发现。

改成 git 之后，「服务器上跑的是哪一版」有确定答案：

```bash
git -C /opt/lifelogging/repo rev-parse --short HEAD
```

> 仓库是**公开的**，所以克隆不需要凭据。被跟踪的文件里**没有**任何密钥
> （`config/config.yaml`、`state/`、`vault/` 都在 `.gitignore` 里）。

## 目录布局

```
/opt/lifelogging/
├── repo/                     ← git clone。服务器上的代码，只有代码
│   ├── scripts/              ← close_day.py 和它 import 的 generate_journal.py 等
│   ├── server/               ← 这两个服务的 unit 和文档
│   └── config/config.yaml    ← ⚠️ 服务器**自己的**那份，gitignored，git pull 不会碰它
└── venv/                     ← Python 依赖。放在 checkout **外面**，git 碰不到
```

`/var/lib/lifelogging/` 是两个服务共用的数据目录（中继的 JSONL、提问文件、
日记中转文件、成本台账）。

> ⚠️ **服务器上的 `config/config.yaml` 和笔记本那份不一样。** 至少三处要改：
> `vault_path`（服务器上没有 vault）、`state_dir`（指到 `/var/lib/lifelogging`）、
> `sources.phone_relay.api_base`（填中继**实际绑定的**地址，见下）。
> 详见 `close-day/README.md`。

---

## 首次部署

```bash
# 1. 代码
sudo mkdir -p /opt/lifelogging
sudo git clone https://github.com/HuangWeiLong-dot/LifeLogging.git /opt/lifelogging/repo

# 2. 依赖（中继是纯标准库，不需要；close_day 需要）
sudo python3 -m venv /opt/lifelogging/venv
sudo /opt/lifelogging/venv/bin/pip install -r /opt/lifelogging/repo/requirements.txt

# 3. 配置和密钥
sudo cp /opt/lifelogging/repo/config/config.example.yaml /opt/lifelogging/repo/config/config.yaml
sudoedit /opt/lifelogging/repo/config/config.yaml     # 按上面那三处改

sudo mkdir -p /etc/lifelogging
sudo python3 -c "import secrets;print('RELAY_API_KEY=' + secrets.token_urlsafe(32))" \
  | sudo tee /etc/lifelogging/relay.env >/dev/null
sudo sh -c 'echo "DEEPSEEK_API_KEY=sk-REPLACE_ME" > /etc/lifelogging/close-day.env'
#   ⚠️ 占位符务必用**纯 ASCII** 写。写成带中文的（比如 `sk-你的key`）就可以被
#      原样粘进 shell —— 于是服务器上装的 literally 是那串占位符，而报错会是
#      httpx 内部一个看不懂的 UnicodeEncodeError。实测踩过一次。
#      换掉它：sudo sed -i 's|sk-REPLACE_ME|<真 key>|' /etc/lifelogging/close-day.env
sudo chmod 600 /etc/lifelogging/relay.env /etc/lifelogging/close-day.env

# 4. 服务
sudo cp /opt/lifelogging/repo/server/phone-relay/lifelogging-relay.service \
        /opt/lifelogging/repo/server/close-day/lifelogging-close-day.service \
        /opt/lifelogging/repo/server/close-day/lifelogging-close-day.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifelogging-relay lifelogging-close-day.timer
```

> 中继的 unit 里 `RELAY_HOST` 默认是 `127.0.0.1`，**要改成 Tailscale 地址**
> 手机才连得上。见 `phone-relay/README.md` 第 4 步。**别写 `0.0.0.0`。**

### 可选：飞书机器人

不想装就跳过 —— 原来那套（Tasker 轮询 + `/ask` 表单）不受影响。
想装的话它有额外的前置条件（要在飞书开放平台建自建应用），**完整步骤见
[`feishu-bot/README.md`](feishu-bot/README.md)**，服务器侧只是多装一个 unit：

```bash
# ⚠️ 它需要 lark-oapi，而上面第 2 步装依赖时可能还没有这个包。
sudo /opt/lifelogging/venv/bin/pip install -r /opt/lifelogging/repo/requirements.txt

sudo tee /etc/lifelogging/feishu-bot.env >/dev/null <<'EOF'
FEISHU_APP_ID=cli_REPLACE_ME
FEISHU_APP_SECRET=REPLACE_ME
FEISHU_OWNER_OPEN_ID=
EOF
sudo chmod 600 /etc/lifelogging/feishu-bot.env

sudo cp /opt/lifelogging/repo/server/feishu-bot/lifelogging-feishu-bot.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifelogging-feishu-bot
```

> ⚠️ `FEISHU_OWNER_OPEN_ID` **先留空**，等机器人连上长连接、你给它发过一句话
> 之后，从日志里把你的 `ou_…` 抄出来再填。理由见那份 README 第 4 步。

## 更新

```bash
sudo /opt/lifelogging/repo/server/deploy.sh
```

它做三件事：快进到 `origin/master`、打印这次更新了哪些 commit、按改动范围决定
要不要重启常驻服务（中继、飞书机器人）。工作区有被改过的**被跟踪**文件时会拒绝
合并并让你先处理 —— 那种情况该由人看，不该脚本自作主张。

⚠️ **它不装依赖。** 如果这次更新动了 `requirements.txt`（比如加了 `lark-oapi`），
`git pull` 不会把它装进 venv —— 而飞书机器人开着 `Restart=always`，于是会
**无限 ImportError 重启**，`systemctl status` 还显示 "active (running)"，
看着像一切正常。这种更新要补一句：

```bash
sudo /opt/lifelogging/venv/bin/pip install -r /opt/lifelogging/repo/requirements.txt
sudo systemctl restart lifelogging-feishu-bot
```

**close_day 不用重启**：它是 oneshot，每次运行都重新读源码，下一个 15 分钟的
tick 自然用上新版本。

> 更新后想确认中继真的换了，**别只看 `/health`** —— 新旧版本的 `/health` 长得
> 一模一样。挑一个只有新版才有的路由去打（比如 `/ask/pending`）。
> 这一条也是实测踩出来的。
