# Dawarich 部署（Linux 服务器）

位置轨迹后端。选它而不是 Reitti 的理由见 `docs/工具核实报告.md`，
主要就一条：**官方内置只读 MCP**（`/api/v1/mcp`），位置数据不用写中间件就能喂给 Agent。

---

## 快速部署

```bash
# 1. 把脚本传到服务器
scp docker/dawarich/install.sh 你的用户@服务器IP:/tmp/

# 2. 登录执行
ssh 你的用户@服务器IP
bash /tmp/install.sh /opt/dawarich
```

脚本会：拉官方 compose → 生成强随机密钥的 `.env` → **把端口绑到 `127.0.0.1`** →
起容器 → 等健康检查 → 打印后续步骤。

---

## 官方 compose 的三个坑（都已在脚本里处理）

这些是**实际读官方 compose 文件**发现的，不是凭印象：

### 1. `SECRET_KEY_BASE` 的默认值是字面量 `"CHANGE_ME"`

```yaml
SECRET_KEY_BASE: ${SECRET_KEY_BASE:-"CHANGE_ME"}
```

不换掉的话，服务能起来，但会话密钥是可预测的。脚本生成 64 字节随机值。

### 2. `TIME_ZONE` 默认是 `Europe/London`

```yaml
TIME_ZONE: ${TIME_ZONE:-Europe/London}
```

不改的话，你所有轨迹的时间戳会差 7–8 小时。脚本设成 `Asia/Shanghai`。

### 3. `DATABASE_PASSWORD` 和 `POSTGRES_PASSWORD` 是两套变量，但必须同值

```yaml
dawarich_app:
  DATABASE_PASSWORD: ${DATABASE_PASSWORD:-password}
dawarich_db:
  POSTGRES_PASSWORD:  ${POSTGRES_PASSWORD:-password}
```

库是用 `POSTGRES_PASSWORD` 初始化的，而 app 用 `DATABASE_PASSWORD` 以 `postgres` 用户连库。
两者不一致就会连不上，而且报错信息不会直接告诉你是这个原因。脚本生成一次、两处复用。

### 附赠：端口绑定是可以控制的

```yaml
ports:
  - "${DAWARICH_APP_PORT:-3000}:3000"
```

因为整个映射串是变量，所以设 `DAWARICH_APP_PORT=127.0.0.1:3000` 就展开成
`"127.0.0.1:3000:3000"` —— **只绑本机回环**。不需要写 override 文件。

（注意：不能用 `docker-compose.override.yml` 去改端口，因为 Compose 对 `ports`
这类多值字段是**拼接**而不是替换，结果会变成同时绑 `0.0.0.0` 和 `127.0.0.1`，
公网那个照样暴露。）

---

## 对外暴露：两条路

装完之后服务**只有服务器本机能访问**（这是故意的 —— 见下面「为什么不直连公网」）。
手机要用，必须选一条：

### 【推荐】没有域名 → Tailscale Serve

不需要买域名、不需要配证书、不暴露到公网：

```bash
# 服务器
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg 3000
tailscale serve status        # 看分配到的域名
```

拿到的是 `https://<机器名>.<你的tailnet>.ts.net`，**带有效 HTTPS 证书**，
只有你 tailnet 里的设备能访问。

手机装 Tailscale 登录同一账号，然后 `config.yaml` 里填这个 https 地址。

### 有域名 → Caddy 自动 HTTPS

```bash
sudo apt install -y caddy
sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
dawarich.你的域名.com {
    reverse_proxy 127.0.0.1:3000
    # 关掉访问日志 —— 避免把 api_key 写进日志文件
    log { output discard }
}
EOF
sudo systemctl reload caddy
```

Caddy 自动申请并续期 Let's Encrypt 证书。域名 A 记录指向服务器公网 IP。
`.env` 里 `APPLICATION_HOSTS` 要加上这个域名。

---

## 为什么不直连公网

Dawarich 的 API key 走 **URL query 参数**（`?api_key=...`）。明文 HTTP 直连意味着：

1. 密钥会出现在沿途每一个中间设备的日志里 —— 路由器、ISP、云厂商负载均衡
2. 位置轨迹是明文传输的
3. 绑在公网 IP 上的服务会在几小时内被全网扫描到

所以脚本默认绑 `127.0.0.1`，对外交给 Tailscale 或 Caddy 处理 TLS。

---

## 实测踩过的坑（2026-10 部署记录）

这几条是**实际部署时撞到的**，不是理论推演。照着做能省几个小时。

### 1. 用 snap 装 Tailscale → 证书签不出来

```bash
snap install tailscale
sudo tailscale cert <域名>
# → open ./xxx.crt.tmp...: permission denied
```

snap 的严格沙箱不让守护进程写任意路径，**`serve` 因此拿不到证书，只能发兜底自签证书**，
表现为 `curl` 报 `no alternative certificate subject name matches`。

**修**：卸掉换官方包。没有干净的绕过办法。
```bash
sudo snap remove tailscale
curl -fsSL https://tailscale.com/install.sh | sh
```

### 2. 重装后节点名多了 `-1` 后缀

重装并重新授权时，如果旧节点还挂在 tailnet 里占着名字，新节点会变成 `<原名>-1`。
`tailscale cert` 会直接告诉你：`must be one of ["<machine>.<tailnet>.ts.net"]`。

**修**：用新名字（并去 https://login.tailscale.com/admin/machines 删掉那个幽灵节点）。

### 3. 删掉 snap 后 `tailscale` 命令找不到

```
bash: /snap/bin/tailscale: No such file or directory
```

是 bash 的命令路径缓存还指着旧位置。`hash -r` 清一下即可。

### 4. ⚠️ 最隐蔽的一个：nginx 抢占了 Tailscale 接口的 443

```bash
sudo ss -tlnp | grep ':443'
# → 0.0.0.0:443  nginx
```

nginx 绑的是**通配地址**，所以它同时占住 `100.x.x.x:443`。
结果是：**发往 Tailscale IP 的 443 流量全部被 nginx 接走，`tailscale serve` 收不到任何请求** ——
而 `tailscale serve` 会显示 "Serve started and running"，不报任何错。

**修**：把 serve 挪到没被占的端口，**不动在跑的业务**。
```bash
sudo tailscale serve --https=443 off
sudo tailscale serve --bg --https=8443 http://127.0.0.1:3000
```
访问地址变成 `https://<域名>:8443`，端口写在 URL 里对客户端无影响。

（另一条路是把 nginx 的 `listen 443 ssl` 改成绑具体公网 IP，把接口让出来。
  但那会动到正在服务的业务，风险不值得 —— 除非你确实不能接受 URL 带端口号。）

### 5. 服务器自己解析不了 ts.net 名字

`getent hosts <节点>.<tailnet>.ts.net` 可能返回公网 IP 而不是 `100.x.x.x` ——
服务器上的 DNS 未必走 MagicDNS。**这不影响使用**（客户端走完整 Tailscale 栈，解析正常），
但会让你在服务器上做验证时连错地方。

**验证时强制指定 IP，绕开 DNS**：
```bash
curl --resolve <域名>:8443:$(tailscale ip -4) -sS -o /dev/null \
     -w "%{http_code}\n" "https://<域名>:8443/api/v1/health"
```

## 手机端接入

拿到 API key 后（`Settings → Account`）：

### GPSLogger（推荐）

- URL：`https://<你的地址>/api/v1/owntracks/points?api_key=<key>`
  （**复用 OwnTracks 端点**，官方推荐做法）
- 方法 POST，Header `Content-Type: application/json`
- Body：`%LAT %LON %TIMESTAMP %ACC %ALT %BATT %SPD`

> ⚠️ GPSLogger v134 起**「忽略电池优化」是强制项**，不设会被系统直接杀。
> 位置权限要设「始终允许」，还要加进厂商自启白名单。

### OwnTracks

HTTP 模式指向同一个端点。Android 14 某次安全补丁后 Significant Location Change
模式会静默失效，需把 `locatorPriority` 改成 `HighAccuracy`。

### 验证

```bash
curl -H "Authorization: Bearer <key>" \
  "https://<你的地址>/api/v1/visits?start_at=2026-10-13T00:00:00&end_at=2026-10-13T23:59:59"
```

---

## 日常运维

```bash
cd /opt/dawarich

docker compose ps                       # 状态
docker compose logs -f dawarich_app     # 日志

# 升级
docker compose down && docker compose pull && docker compose up -d

# 备份（重要）
docker compose exec dawarich_db pg_dump -U postgres dawarich_development > backup-$(date +%F).sql
```

> ⚠️ **永远不要 `docker compose down -v`。** `-v` 删数据卷，轨迹全没。
> 官方文档专门警告了这一点。

## 磁盘

50GB 绰绰有余。PostGIS 存轨迹点，一天几千个点也就几 MB，一年通常不到 1GB。
日志有轮转（`LOG_MAX_SIZE=100m` / `LOG_MAX_FILE=5`），最多占 500MB。

---

## Dawarich 帮你做掉的两件事

1. **停留点检测**（1.8.0 起）：自动识别你在某地停留，带 0–100 置信度
2. **交通方式推断**：按速度+加速度分类 walking / cycling / driving / running /
   train / bus / flying / boat / stationary

这两件事自己写要不少代码，现在白拿。
