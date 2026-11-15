#!/usr/bin/env bash
# 一条命令：编译 → 装到 USB 连着的手机 → 拉起界面。
#
#   ./dev.sh          增量安装 debug 包（最快，几秒）
#   ./dev.sh release  安装 release 包（最终版）
#
# 前置：手机打开「开发者选项 → USB 调试」，插线后手机上点「允许」。
set -e
cd "$(dirname "$0")"

ADB="${ANDROID_HOME:-$LOCALAPPDATA/Android/Sdk}/platform-tools/adb.exe"
[ -x "$ADB" ] || ADB=adb

if ! "$ADB" devices | grep -qE "device$"; then
  echo "没有检测到已授权的设备。"
  echo "  1) 手机：设置 → 关于手机 → 连点版本号 7 次 → 开发者选项"
  echo "  2) 打开「USB 调试」，插线，手机上点「允许」"
  echo "  3) 检查：$ADB devices"
  exit 1
fi

TASK=installDebug
[ "${1:-}" = "release" ] && TASK=installRelease

./gradlew "$TASK"
"$ADB" shell am start -n com.lifelogging.bridge/com.lifelogging.lifepassion.MainActivity >/dev/null
echo "已启动。"
