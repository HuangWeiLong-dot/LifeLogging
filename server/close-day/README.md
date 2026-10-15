# 服务器侧：关账 + 写日记正文

**它解决什么。** 原来「一天什么时候结束」由**笔记本什么时候开机**决定 ——
实测 2026-10-14 那天机器 22:18 就关了，那一天实际到**次日 17:56** 才被关账。
现在调 AI 写日记这一步跑在 24 小时开着的服务器上。

**它不碰 vault。** 服务器只把写好的**正文**存回中继；frontmatter、手写的
`## Notes`、副笔记、`06-AI-Sessions` 全在笔记本上组装（见
`scripts/pull_journal_output.py`）。这样服务器不需要看见 vault，也就不需要
做双向同步 —— 没有「两边同时写同一个库」那种冲突。

## 为什么是轮询而不是定点

见 `lifelogging-close-day.timer` 里的说明。一句话：**该等的不是时钟，是笔记本
把那天标成「终版」** —— 笔记本 18:00 关机的话，4:05 时服务器手里只有它 18:00
那次推送的半截数据。所以 `close_day.py` 每 15 分钟幂等地扫一遍。

## 部署

### 0. 前置

服务器上应该已经在跑 `phone-relay`（见 `../phone-relay/README.md`）。
本服务依赖它：结果是通过 `POST /journal/output` 存回去的。

### 1. 传代码

`close_day.py` 会 import `scripts/` 下的模块（复用 `generate_journal.py` 的
提示词和校验，**不复制一份** —— 提示词一漂移日记质量就悄悄变了），所以要把
整个 `scripts/` 和一份服务器用的 `config/` 传上去。

```bash
# 在本机仓库根目录
rsync -av --exclude '.venv' --exclude 'state' --exclude 'vault' --exclude '.git' \
      ./ 你@服务器:/tmp/lifelogging/

# 在服务器
sudo mkdir -p /opt/lifelogging
sudo cp -r /tmp/lifelogging/scripts /tmp/lifelogging/config /opt/lifelogging/
```

> ⚠️ **别把本机的 `config/config.yaml` 直接搬过去** —— 里面的 `state_dir`、
> `vault_path` 是写死的 Windows 路径。见下一步。

### 2. 服务器用的 config.yaml

在服务器上改这几项（其余照抄本机那份）：

```yaml
# 服务器上没有 vault，也不该有 —— 写个占位，让它显眼地不对
vault_path: "/nonexistent/server-has-no-vault"
timezone: "Asia/Shanghai"
state_dir: "/var/lib/lifelogging"        # 和 phone-relay 共用那个目录
```

`sources.phone_relay.api_base` 要指向**本机**（两个服务在同一台机器上）：

```yaml
sources:
  phone_relay:
    enabled: true
    api_base: "http://127.0.0.1:8788"
    api_key: "<和 /etc/lifelogging/relay.env 里那把一样>"
```

其余段（`categories` / `ai` / `budget` / `pricing`）照抄本机那份 ——
尤其是 `pricing`，预算闸门靠它。

### 3. venv

中继是纯标准库所以不需要；这个要。

```bash
sudo python3 -m venv /opt/lifelogging/venv
sudo /opt/lifelogging/venv/bin/pip install -r /opt/lifelogging/requirements.txt
```

> Linux 上不需要 `tzdata` 那个包（系统自带时区库），但装上也无害。

### 4. 密钥

```bash
sudo mkdir -p /etc/lifelogging
sudo tee /etc/lifelogging/close-day.env >/dev/null <<'EOF'
DEEPSEEK_API_KEY=sk-你的key
EOF
sudo chmod 600 /etc/lifelogging/close-day.env
```

> ⚠️ 这个文件**不要进 git**。unit 文件本身是进 git 的，所以密钥走
> `EnvironmentFile=`，不写在 unit 里。

### 5. 装服务

```bash
sudo cp server/close-day/lifelogging-close-day.service \
        server/close-day/lifelogging-close-day.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifelogging-close-day.timer
```

### 6. 验证

```bash
# 手动跑一次，看它说什么
sudo systemctl start lifelogging-close-day.service
sudo journalctl -u lifelogging-close-day -n 40 --no-pager

# 定时器排上了吗
systemctl list-timers lifelogging-close-day.timer
```

**期望看到的**：有终版输入、还没生成的那天会被处理；其余天报
「输入包还没标终版，跳过」或「已经生成过了」。**一次都不调 AI 也是正常的** ——
笔记本还没推终版输入包，就没什么可做的。

## 排查

| 症状 | 原因 |
|---|---|
| 一直「nothing to do」 | 笔记本那边还没推**终版**输入包。查中继：`curl -H "X-API-Key: <key>" 'http://127.0.0.1:8788/journal/inputs?day=<日期>'`，看 `final` 是不是 `true` |
| `relay unreachable` | 中继没在跑，或 config 里的 `phone_relay.api_base` 指错了。同一台机器应该是 `127.0.0.1:8788` |
| `DEEPSEEK_API_KEY is not set` | `/etc/lifelogging/close-day.env` 没建好或权限不对。`systemctl show lifelogging-close-day -p EnvironmentFile` 能看到它读的是哪个文件 |
| 日记写出来了，但笔记本没取到 | 那是笔记本那侧的事 —— `scripts/pull_journal_output.py`。它每次跑会往前扫 3 天 |
| `❌ ... over the 60% limit` | 模型写的时间块和规则引擎对不上，**没存**。这是有意的硬闸门：宁可下次重来，也不写一份编出来的时间轴 |

## 升级

和 phone-relay 一样，**升级的坑是拷到了 systemd 不看的地方**。只改了这个
服务的话：

```bash
scp scripts/close_day.py 你@服务器:/tmp/
sudo cp /tmp/close_day.py /opt/lifelogging/scripts/close_day.py
sudo systemctl restart lifelogging-close-day.timer   # 下一轮就会用新的
```

改动涉及 `generate_journal.py` / `common.py` 等共用模块时，要把 `scripts/`
整个重传 —— 提示词和校验逻辑是**共用**的，只传一个文件会让两边漂移。
