# 让手机不依赖 Tailscale：Cloudflare Tunnel + Access

## 要解决什么

**Android 同一时刻只允许一个 VPN 服务。** Tailscale 占着那个槽位，Hiddify 也是 ——
后开的会把前一个踢掉。

代价不只是「连不上」：手机的 Tasker 要往中继推四样东西（解锁 / 切应用 / 屏幕亮灭 /
Gadgetbridge 导出），还要轮询提问，**全走 Tailscale**。而 Tasker 没有队列 ——
开着代理那段时间的数据，丢就是真丢了，中继和笔记本都不会知道。

（笔记本不受影响：Windows 上 Tailscale 和 Clash 能共存。这个坑只在手机上。）

**做法**：把中继通过 Cloudflare Tunnel 挂到一个 HTTPS 域名上，用 Access 只放
你自己的设备进来。手机**完全不需要 Tailscale**，Hiddify 想开就开。

## 为什么这次改动特别小

cloudflared 跑在**和中继同一台服务器**上，所以它可以直接连中继现在绑的那个地址：

```
手机 ──https://relay.你的域名──► Cloudflare ──隧道──► cloudflared
                                                       │
                                                       ▼ http://100.127.41.49:8788
                                                  中继（没动过）
笔记本 ──http://100.127.41.49:8788（Tailscale，没动过）
close_day ──http://127.0.0.1:8788（同机）
```

所以：**中继不用改、笔记本不用改、一行代码不用改。** 只有手机换一条路。

（一开始我想让中继改绑环回、笔记本也走隧道 —— 那是多余的。cloudflared 又不是
只能连 127.0.0.1。）

> ✅ 顺带一个好性质：中继仍然只绑 Tailscale 地址，**公网直接打不到它**。
> 对外的只有隧道那一个前门。

---

## 前置

- 域名在 Cloudflare 上 —— 本例用 `relay.liveinpassion.me`

> 📌 **`liveinpassion.me` 是另一个项目的域名，这个子域是刻意挂在它下面的。**
> 技术上不会互相影响：**Cloudflare Access 的策略是按 hostname 隔离的**，
> `relay.liveinpassion.me` 和那个项目的 `cms.liveinpassion.me` 各自一套规则、
> 各自一条 DNS 记录。共用的只是 Cloudflare 账号本身。
> 如果哪天想把这两套彻底分开，换一个域名只需要改本文档里的 hostname ——
> 服务器上没有任何东西写死它。
- 服务器上能 sudo
- **tailscaled 在跑**（中继绑的是 100.x 地址）

下面用 `relay.liveinpassion.me` 举例，换成你想要的子域名。

## 1. 服务器：装 cloudflared

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install -y cloudflared
cloudflared --version
```

## 2. 建隧道

```bash
cloudflared tunnel login          # 浏览器里授权，选 liveinpassion.me
cloudflared tunnel create lifelogging
```

配置 `/etc/cloudflared/config.yml`。

⚠️ **用一个 shell 变量，别手抄 Tunnel ID。** Tunnel ID 在这份配置里要出现**两次**
（`tunnel:` 和 `credentials-file:`），手抄一份漏一处的话 cloudflared 起不来，
而报错只有一句笼统的「control process exited with error code」——
真原因要去 `journalctl -u cloudflared` 才看得到。（实测就这么栽过一次。）

```bash
# 把这里换成 `cloudflared tunnel create` 输出的那个 ID
TUNNEL_ID=9d082d97-7ac7-4725-ab9e-492ae5a89fd6

sudo mkdir -p /etc/cloudflared
# ⚠️ 定界符 YAML **不加引号** —— 加了 $TUNNEL_ID 就不会展开
sudo tee /etc/cloudflared/config.yml >/dev/null <<YAML
tunnel: $TUNNEL_ID
credentials-file: /root/.cloudflared/$TUNNEL_ID.json
ingress:
  # ⚠️ 指中继**实际绑的地址**（Tailscale 那个），不是 127.0.0.1。
  #    中继只绑了 100.x，写 127.0.0.1 的话隧道建起来也是 502 ——
  #    和之前那个「curl 什么都不返回」是同一个坑。
  - hostname: relay.liveinpassion.me
    service: http://100.127.41.49:8788
  - service: http_status:404
YAML

# 先确认两件事都对上，再往下走
sudo cat /etc/cloudflared/config.yml
sudo ls -la /root/.cloudflared/          # 这个 json 必须存在，名字要和上面一致
```

```bash
cloudflared tunnel route dns lifelogging relay.liveinpassion.me
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

**先确认隧道能通到中继**（Access 还没配，这时候应该是公开可达的，配完就锁上）：

```bash
KEY=$(grep '^RELAY_API_KEY=' /etc/lifelogging/relay.env | cut -d= -f2)
curl -sS https://relay.liveinpassion.me/health
```

## 3. Cloudflare Access：只放你自己进来

后台 → Zero Trust → Access → Applications → Add → Self-hosted：

| 项 | 值 |
|---|---|
| Application name | LifeLogging Relay |
| Domain | `relay.liveinpassion.me` |
| Session duration | 一个月（手机上别老要你重登） |

Policy：`Allow`，Include → **Emails** → 你的邮箱。

### 3b. 手机上的 Tasker 怎么认证

Tasker 走不了浏览器的登录流程，要用 **Service Token**：

Zero Trust → Access → Service Auth → **Create Service Token**，拿到
`Client ID` / `Client Secret`（**只显示一次**）。然后在上面那条 Policy 里
再加一条 Include → **Service Token** → 选刚建的这个。

Tasker 里**每一个** HTTP Request 动作都要加这两个 header：

```
CF-Access-Client-Id: <Client ID>
CF-Access-Client-Secret: <Client Secret>
```

一共 **6 处**：四个上报 Profile（解锁 / 切应用 / 屏幕亮 / 屏幕灭）+
Gadgetbridge 上传 + 提问轮询。漏掉任何一处，那一处就会在 Cloudflare 那层被挡，
**而且症状是 302/403，看起来像鉴权失败，不像漏了 header**。

## 4. 只有手机改地址

Tasker 里所有指向 `http://100.127.41.49:8788` 的 URL 改成
`https://relay.liveinpassion.me`（**https，且不带端口**）。

笔记本和服务器的 `config.yaml` **一个字都不用改**。

**手机浏览器打开表单**：

```
https://relay.liveinpassion.me/ask?key=<API key>
```

第一次会走一遍 Access 登录（邮箱验证码），之后一个月内不用再登。

> 密钥出现在查询串里，会进 Cloudflare 的日志。Access 已经是真认证了，但那层
> `?key=` 作为第二道仍然留着。

## 验证清单

```bash
# 1. 隧道活着
systemctl is-active cloudflared

# 2. 中继没被动过（仍然只绑 Tailscale 地址）
ss -tlnp | grep 8788

# 3. 笔记本这条路没坏
curl -sS -H "X-API-Key: $KEY" http://100.127.41.49:8788/health

# 4. 不带 token 从外面访问 → 应该被 Access 拦（302 到登录页）
curl -sS -o /dev/null -w "%{http_code}\n" https://relay.liveinpassion.me/health

# 5. 带 Service Token → 应该直接通
curl -sS -H "CF-Access-Client-Id: <id>" -H "CF-Access-Client-Secret: <secret>" \
     https://relay.liveinpassion.me/health
```

**最后是真的拿手机试**：关掉 Tailscale、开着 Hiddify，让 Tasker 跑一次上报，
看中继上有没有新事件：

```bash
curl -sS -H "X-API-Key: $KEY" http://100.127.41.49:8788/health
#   today_events 应该开始涨
```

## 稳妥的切换顺序

**别一次把 6 个 Tasker 动作全改掉。** 先改**一个**（比如解锁上报），确认中继上
开始收到事件；再改其余的。不然一步没配好，你会分不清是 Cloudflare、Service Token
还是 Tasker 的问题。

改完都验过之后，手机上的 Tailscale 可以卸了（或留着不用它）。

## 之后作废的东西

- `../phone-relay/README.md` 里「绑定地址 = Tailscale 地址」那一节，对**手机**不再适用
  （中继本身仍然绑 100.x，只是手机改走隧道了）
- 本文档假设的是「cloudflared 连 Tailscale 地址」。如果哪天中继改绑了环回，
  这里的 `service:` 也要跟着改
