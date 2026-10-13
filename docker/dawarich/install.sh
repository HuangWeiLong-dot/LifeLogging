#!/usr/bin/env bash
# Dawarich 部署（Linux 服务器）
#
# 用法：
#   bash install.sh /opt/dawarich
#
# 这个脚本基于**实际读到的官方 compose**写的，不是凭印象。
# 三个关键事实（都有依据）：
#   1. 端口是 "${DAWARICH_APP_PORT:-3000}:3000"
#      → 设成 127.0.0.1:3000 就变成 "127.0.0.1:3000:3000"，只绑本机
#   2. SECRET_KEY_BASE 的默认值就是字面量 "CHANGE_ME"
#   3. TIME_ZONE 默认 Europe/London

set -euo pipefail

TARGET="${1:-/opt/dawarich}"
COMPOSE_URL="https://raw.githubusercontent.com/Freika/dawarich/master/docker/docker-compose.yml"
# 容器内端口固定 3000；宿主机绑到这个端口，且只绑 127.0.0.1
HOST_PORT="3000"

say()  { printf '\033[1;36m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ── 0. 前置检查 ─────────────────────────────────────────────────────
say "检查运行环境"

if ! command -v docker >/dev/null 2>&1; then
  die "没装 docker。先装：curl -fsSL https://get.docker.com | sh"
fi
if ! docker compose version >/dev/null 2>&1; then
  die "docker compose 插件不可用（新版 docker 自带，老版要单独装）"
fi
if ! command -v openssl >/dev/null 2>&1; then
  die "需要 openssl 生成密钥"
fi
if ! docker info >/dev/null 2>&1; then
  die "docker 守护进程没跑，或者当前用户没权限（试试 sudo usermod -aG docker \$USER 后重新登录）"
fi

# 内存（官方建议 2GB 起）
MEM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$MEM_MB" -gt 0 ] && [ "$MEM_MB" -lt 1800 ]; then
  warn "内存只有 ${MEM_MB}MB，官方建议 2GB 起。可能起不来。"
fi
ok "环境检查通过"

mkdir -p "$TARGET"
cd "$TARGET"

# ── 1. 拉官方 compose ───────────────────────────────────────────────
say "拉取官方 compose"
if [ -f docker-compose.yml ]; then
  cp docker-compose.yml "docker-compose.yml.bak.$(date +%s)"
  warn "已存在，备份为 docker-compose.yml.bak.*"
fi

if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$COMPOSE_URL" -o docker-compose.yml
elif command -v wget >/dev/null 2>&1; then
  wget -q "$COMPOSE_URL" -O docker-compose.yml
else
  die "需要 curl 或 wget"
fi

# 拉下来先校验一下，别拿到一份半截的 HTML 错误页
grep -q "dawarich_app" docker-compose.yml \
  || die "拉到的文件不像 compose（没找到 dawarich_app）。检查网络或官方地址是否变了。"
ok "已下载 docker-compose.yml ($(wc -c < docker-compose.yml) 字节)"

# ── 2. 生成 .env ────────────────────────────────────────────────────
if [ -f .env ]; then
  warn ".env 已存在，保留不覆盖（要重来先备份删掉它）"
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
  HOST_PORT="${DAWARICH_APP_PORT:-3000}"
  HOST_PORT="${HOST_PORT##*:}"
else
  say "生成 .env"

  SECRET_KEY_BASE="$(openssl rand -hex 64)"
  # app 用 DATABASE_PASSWORD 以 postgres 用户连库，
  # 而库初始化用的是 POSTGRES_PASSWORD —— 两者必须一致，否则连不上。
  DB_PASS="$(openssl rand -hex 24)"

  printf '服务器对外的地址（手机要能访问到；多个用逗号分隔，不带 http:// 前缀）：'
  read -r APP_HOSTS
  APP_HOSTS="${APP_HOSTS:-localhost,127.0.0.1}"

  cat > .env <<EOF
# ── Dawarich 配置 ───────────────────────────────────────────────────
# ⚠️ 这个文件含密钥，别提交到任何仓库

# 应用只能从本机访问，对外由 Tailscale Serve 或 Caddy 负责。
# 这样即使服务器有公网 IP，端口也不会被扫描到。
DAWARICH_APP_PORT=127.0.0.1:${HOST_PORT}

# 逗号分隔，不带协议前缀，逗号后不留空格
APPLICATION_HOSTS=${APP_HOSTS}
APPLICATION_PROTOCOL=http

# ⚠️ 官方默认值是字面量 CHANGE_ME —— 必须换掉
SECRET_KEY_BASE=${SECRET_KEY_BASE}

# ⚠️ 这两个必须同值
DATABASE_PASSWORD=${DB_PASS}
POSTGRES_PASSWORD=${DB_PASS}
DATABASE_USERNAME=postgres
POSTGRES_USER=postgres
DATABASE_NAME=dawarich_development
POSTGRES_DB=dawarich_development

REDIS_URL=redis://dawarich_redis:6379

RAILS_ENV=production
SELF_HOSTED=true
# ⚠️ 官方默认 Europe/London —— 不改成你的时区，日记里的时间会全部错位
TIME_ZONE=Asia/Shanghai
STORE_GEODATA=true

# 家庭实例一个 worker 就够（官方注释原话）
WEB_CONCURRENCY=1
APP_MEMORY_LIMIT=4G

# 2FA 加密密钥，不用 2FA 就留空
OTP_ENCRYPTION_PRIMARY_KEY=
OTP_ENCRYPTION_DETERMINISTIC_KEY=
OTP_ENCRYPTION_KEY_DERIVATION_SALT=

# 日志轮转，别把 50G 磁盘写满
LOG_MAX_SIZE=100m
LOG_MAX_FILE=5
EOF

  chmod 600 .env
  ok "已生成 .env（权限 600）"
fi

# ── 3. 启动 ─────────────────────────────────────────────────────────
say "启动容器（首次要拉镜像，几分钟）"
docker compose up -d

say "等待健康检查（最多 3 分钟）"
for i in $(seq 1 36); do
  if docker compose ps --format json 2>/dev/null | grep -q '"Health":"healthy"' \
     || curl -fsS "http://127.0.0.1:${HOST_PORT}/api/v1/health" >/dev/null 2>&1; then
    ok "服务已就绪"
    break
  fi
  printf '   ...等待中 %ds\r' $((i * 5))
  sleep 5
  if [ "$i" -eq 36 ]; then
    warn "超时了。看日志：docker compose logs -f dawarich_app"
  fi
done

echo
say "容器状态"
docker compose ps

# ── 4. 收尾提示 ─────────────────────────────────────────────────────
cat <<EOF

────────────────────────────────────────────────────────────────
下一步
────────────────────────────────────────────────────────────────

1) 打开 http://127.0.0.1:${HOST_PORT}  —— 注意是**服务器上的** 127.0.0.1，
   不是你自己电脑。所以在服务器上开 SSH 隧道：

     ssh -L ${HOST_PORT}:127.0.0.1:${HOST_PORT} 你的用户@服务器IP

   然后在自己电脑上开 http://localhost:${HOST_PORT} 完成首次注册。

2) 注册后进入  Settings → Account  拿 API Key。

3) 把 API Key 和访问地址填进本项目的 config.yaml：

     sources:
       dawarich:
         enabled: true
         api_base: "http://<你的访问地址>:${HOST_PORT}"
         api_key: "<刚拿到的 key>"

────────────────────────────────────────────────────────────────
对外暴露（现在只有本机能访问，手机还连不上）
────────────────────────────────────────────────────────────────

【推荐】没有域名 → 用 Tailscale Serve，不需要买域名、不需要配证书、
         也不会暴露到公网：

     curl -fsSL https://tailscale.com/install.sh | sh
     sudo tailscale up
     sudo tailscale serve --bg ${HOST_PORT}

   然后 API 地址用 https://<机器名>.<你的tailnet>.ts.net
   （具体域名跑 tailscale serve status 看）

【有域名】→ 用 Caddy 自动 HTTPS，配置见本项目 docker/dawarich/README.md

⚠️ 不管走哪条，都**不要**把 DAWARICH_APP_PORT 改回 0.0.0.0。
   Dawarich 的 API key 走 URL query 参数，明文暴露在公网上会写进
   沿途所有中间设备的日志。

────────────────────────────────────────────────────────────────
日常运维
────────────────────────────────────────────────────────────────

升级：  cd $TARGET && docker compose down && docker compose pull && docker compose up -d
日志：  docker compose logs -f dawarich_app
备份：  docker compose exec dawarich_db pg_dump -U postgres dawarich_development > backup.sql

⚠️ 永远不要执行 docker compose down -v  ——  -v 会删掉数据卷，轨迹全没。
EOF
