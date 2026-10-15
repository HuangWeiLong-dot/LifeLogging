# 服务器侧

这台 Linux 机器上跑两个服务 + 一份共享配置：

| 服务 | 是什么 | 形态 |
|---|---|---|
| `lifelogging-relay` | 中继：手机上报入口 + 每日提问 + 日记中转 | 常驻（systemd service） |
| `lifelogging-close-day` | 关账 + 调 AI 写日记正文 | 定时（systemd timer，每 15 分钟） |

各自的具体说明： [`phone-relay/README.md`](phone-relay/README.md) · [`close-day/README.md`](close-day/README.md)

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
> `sources.phone_relay.api_base`（两个服务在同一台机器上，用 `127.0.0.1:8788`）。
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
sudo sh -c 'echo "DEEPSEEK_API_KEY=sk-你的key" >> /etc/lifelogging/close-day.env'
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

## 更新

```bash
sudo /opt/lifelogging/repo/server/deploy.sh
```

它做三件事：快进到 `origin/master`、打印这次更新了哪些 commit、按改动范围决定
要不要重启中继。工作区有被改过的**被跟踪**文件时会拒绝合并并让你先处理 ——
那种情况该由人看，不该脚本自作主张。

**close_day 不用重启**：它是 oneshot，每次运行都重新读源码，下一个 15 分钟的
tick 自然用上新版本。

> 更新后想确认中继真的换了，**别只看 `/health`** —— 新旧版本的 `/health` 长得
> 一模一样。挑一个只有新版才有的路由去打（比如 `/ask/pending`）。
> 这一条也是实测踩出来的。
