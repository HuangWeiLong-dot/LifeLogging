#!/usr/bin/env bash
#
# 服务器侧部署：把代码更新到 origin/master，需要的话重启服务。
#
# **为什么要有它**
#
#   原来服务器上的代码是手工 `scp` 上去的（中继只有一个 stdlib 文件时，
#   「拷一个文件」确实最简单）。但加了 close_day 之后，服务器上的东西变成了
#   「整个 scripts/ + 一个 venv + 三个 unit + 一份 config」，手工拷的代价
#   开始超过收益：
#
#     · 拷漏一个文件，服务起来才报错（实测：unit 里的 ExecStart 指向
#       /opt/lifelogging/close_day.py，而代码在 scripts/ 下）
#     · 服务器上跑的和仓库里的会**悄悄漂移**，而且没有任何东西会提醒你
#       （实测：中继在服务器上落后了一个版本，直到新路由 404 才发现）
#
#   改成从 git 拉之后，「部署」= 一条命令，而且「服务器上跑的是哪一版」
#   有确定答案：`git -C /opt/lifelogging/repo rev-parse --short HEAD`。
#
# 用法（在服务器上，需要 root —— 它要 git pull 和 systemctl restart）：
#
#   sudo /opt/lifelogging/repo/server/deploy.sh
#
# 首次部署见 server/README.md。

set -euo pipefail

REPO="${REPO:-/opt/lifelogging/repo}"
BRANCH="${BRANCH:-master}"
RELAY_UNIT="lifelogging-relay"
FEISHU_BOT_UNIT="lifelogging-feishu-bot"
CLOSE_DAY_TIMER="lifelogging-close-day.timer"

die() { echo "FAIL $*" >&2; exit 1; }

[ -d "$REPO/.git" ] || die "$REPO 不是 git 仓库。首次部署见 server/README.md。"
cd "$REPO"

# ⚠️ 先检查本地改动。--ff-only 在有本地提交时会失败，但**工作区里被改过的
#    被跟踪文件**会让合并结果变得难以预料，那种情况该由人来看，不该脚本
#    自作主张。config/config.yaml 是 gitignored，所以服务器自己的那份配置
#    不会出现在这里 —— 这正是它该放的位置。
dirty=$(git status --porcelain --untracked-files=no)
if [ -n "$dirty" ]; then
    echo "$dirty" | sed 's/^/     /'
    die "工作区里有被改过的**被跟踪**文件，不自动合并。先处理掉它们。"
fi

before=$(git rev-parse HEAD)
echo "-> 当前 $(git rev-parse --short HEAD)  ($(git log -1 --format=%s | cut -c1-60))"

git fetch --quiet origin "$BRANCH"
git merge --ff-only --quiet "origin/$BRANCH" \
    || die "不能快进合并（本地有 origin/$BRANCH 之外的提交？）。先去看 git log。"

after=$(git rev-parse HEAD)
if [ "$before" = "$after" ]; then
    echo "-> 已经是最新，没有变化"
else
    echo "-> 更新 $(git rev-parse --short "$before") -> $(git rev-parse --short "$after")"
    git --no-pager log --oneline "$before..$after" | sed 's/^/     /'
fi

changed=$(git diff --name-only "$before" "$after")

# 中继是常驻进程，代码换了要重启才生效。
if echo "$changed" | grep -q '^server/phone-relay/'; then
    systemctl restart "$RELAY_UNIT"
    echo "-> 重启了 $RELAY_UNIT"
fi

# 飞书机器人也是常驻进程（长连接要一直挂着），代码换了同样得重启。
#
# ⚠️ 首次部署时这个 unit 还没装 —— 而新目录 deploy.sh 本来是完全认不出来的
#    （下面这段就是补上这个缺口）。所以必须先判断 unit 存不存在：裸
#    `systemctl restart` 在 unit 不存在时会返回非零，而脚本开着 `set -e`，
#    于是**整次部署会在这里中止**，后面该重启的中继也不重启了。
if echo "$changed" | grep -q '^server/feishu-bot/'; then
    if [ -f "/etc/systemd/system/${FEISHU_BOT_UNIT}.service" ]; then
        systemctl restart "$FEISHU_BOT_UNIT"
        echo "-> 重启了 $FEISHU_BOT_UNIT"
    else
        echo "-> $FEISHU_BOT_UNIT 还没装过，跳过。首次安装见 server/feishu-bot/README.md"
    fi
fi

# ⚠️ 这里**不装依赖**。如果这次更新往 requirements.txt 里加了包（比如 lark-oapi），
#    git pull 不会把它装进 venv，机器人会在 Restart=always 下无限 ImportError 重启。
#    加了依赖的更新，部署完要补一句：
#      sudo /opt/lifelogging/venv/bin/pip install -r /opt/lifelogging/repo/requirements.txt

# close_day / live_questions 都是 oneshot —— 每次运行都重新读一遍源码，
# 不需要重启，下一个 tick 自然会用上新版本。
if echo "$changed" | grep -qE '^(scripts/|server/close-day/)'; then
    echo "-> close-day 下次运行会自己用上新代码（oneshot，不用重启）"
fi
if echo "$changed" | grep -q '^scripts/live_questions\.py'; then
    echo "-> live-questions 下次 tick 会自己用上新代码（oneshot，不用重启）"
fi

# ⚠️ 但 **unit/timer 文件本身**改了不一样：deploy.sh 只做 git pull，
#    不会把 /etc/systemd/system/ 里那份换掉。改了 unit 就要手工重装：
#      sudo cp /opt/lifelogging/repo/server/feishu-bot/lifelogging-*.{service,timer} \
#              /etc/systemd/system/ && sudo systemctl daemon-reload
if echo "$changed" | grep -qE '^server/(feishu-bot|close-day)/.*\.(service|timer)$'; then
    echo "-> ⚠️  有 unit/timer 文件改动，但它们**不会**自动生效。手工重装："
    echo "       sudo cp /opt/lifelogging/repo/server/feishu-bot/lifelogging-*.{service,timer} /etc/systemd/system/"
    echo "       sudo cp /opt/lifelogging/repo/server/close-day/lifelogging-*.{service,timer} /etc/systemd/system/"
    echo "       sudo systemctl daemon-reload"
fi

echo "-> 完成。服务器现在跑的是 $(git rev-parse --short HEAD)"
