#!/usr/bin/env bash
# 不依赖 Gradle 的构建脚本：aapt2 → javac → d8 → zipalign → apksigner。
# 只需要 Android SDK 的 build-tools 和 JDK。
#
# 版本号来自 version.properties；升级时改 versionCode（必须递增）和 versionName。
# 签名用 keystore/release.jks + keystore.properties（都不进仓库）。
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_SDK_ROOT:-$LOCALAPPDATA/Android/Sdk}"
BT="$SDK/build-tools/36.1.0"
ANDROID_JAR="$SDK/platforms/android-36/android.jar"
[ -x "$BT/aapt2.exe" ] || BT="$SDK/build-tools/35.0.0"
[ -f "$BT/aapt2.exe" ] || { echo "找不到 build-tools：$BT"; exit 1; }
[ -f "$ANDROID_JAR" ] || { echo "找不到 android.jar：$ANDROID_JAR"; exit 1; }

# ── 版本 ────────────────────────────────────────────────────────
[ -f version.properties ] || { echo "缺少 version.properties"; exit 1; }
VCODE=$(sed -n 's/^versionCode=//p' version.properties | tr -d '[:space:]')
VNAME=$(sed -n 's/^versionName=//p' version.properties | tr -d '[:space:]')
[ -n "$VCODE" ] && [ -n "$VNAME" ] || { echo "version.properties 缺少 versionCode/versionName"; exit 1; }
echo "→ version $VNAME ($VCODE)"

# IDE（VSCode Java 扩展）靠 libs/android.jar 解析 android.* 的 import
mkdir -p libs
[ -f libs/android.jar ] || cp "$ANDROID_JAR" libs/android.jar

OUT=build
rm -rf "$OUT"
mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex"

echo "→ 编译资源"
"$BT/aapt2.exe" compile --dir res -o "$OUT/res.zip"

echo "→ 链接资源 + 生成 R.java"
"$BT/aapt2.exe" link \
  -o "$OUT/base.apk" \
  -I "$ANDROID_JAR" \
  --manifest AndroidManifest.xml \
  --java "$OUT/gen" \
  --min-sdk-version 26 \
  --target-sdk-version 34 \
  --version-code "$VCODE" \
  --version-name "$VNAME" \
  "$OUT/res.zip"

echo "→ 编译 Java"
find src "$OUT/gen" -name '*.java' > "$OUT/sources.txt"
# 用 -classpath 而不是 -bootclasspath：android.jar 里没有
# java.lang.invoke.LambdaMetafactory，用 bootclasspath 编译不了 lambda。
javac -source 8 -target 8 -nowarn -encoding UTF-8 -classpath "$ANDROID_JAR" \
  -d "$OUT/classes" @"$OUT/sources.txt"

echo "→ dex"
find "$OUT/classes" -name '*.class' > "$OUT/classes.txt"
"$BT/d8.bat" --lib "$ANDROID_JAR" --min-api 26 --output "$OUT/dex" @"$OUT/classes.txt"

echo "→ 打包 classes.dex"
python - "$OUT" <<'PY'
import os, sys, zipfile
out = sys.argv[1]
with zipfile.ZipFile(os.path.join(out, "base.apk"), "a", zipfile.ZIP_DEFLATED) as z:
    z.write(os.path.join(out, "dex", "classes.dex"), "classes.dex")
PY

echo "→ 对齐"
"$BT/zipalign.exe" -f 4 "$OUT/base.apk" "$OUT/aligned.apk"

# ── 签名 ────────────────────────────────────────────────────────
# 密钥不进仓库；丢了就只能卸载重装（签名不同无法升级）。
KS_PROPS=keystore.properties
if [ ! -f keystore/release.jks ]; then
  echo "⚠️  没有 keystore/release.jks，生成一个新的（签名将和之前的版本不同，需卸载重装）"
  mkdir -p keystore
  NEWPASS=$(python -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(24)))")
  keytool -genkeypair -v -keystore keystore/release.jks -storepass "$NEWPASS" \
    -alias androiddebugkey -keypass "$NEWPASS" -keyalg RSA -keysize 2048 \
    -validity 10000 -dname "CN=LifeBridge, OU=Phone, O=LifeLogging, C=CN" >/dev/null 2>&1
  printf 'storeFile=keystore/release.jks\nstorePassword=%s\nkeyAlias=androiddebugkey\nkeyPassword=%s\n' \
    "$NEWPASS" "$NEWPASS" > "$KS_PROPS"
fi
[ -f "$KS_PROPS" ] || { echo "缺少 $KS_PROPS"; exit 1; }

STORE_FILE=$(sed -n 's/^storeFile=//p' "$KS_PROPS")
STORE_PASS=$(sed -n 's/^storePassword=//p' "$KS_PROPS")
KEY_ALIAS=$(sed -n 's/^keyAlias=//p' "$KS_PROPS")
KEY_PASS=$(sed -n 's/^keyPassword=//p' "$KS_PROPS")

echo "→ 签名"
"$BT/apksigner.bat" sign \
  --ks "$STORE_FILE" --ks-pass "pass:$STORE_PASS" \
  --ks-key-alias "$KEY_ALIAS" --key-pass "pass:$KEY_PASS" \
  --out LifeBridge.apk "$OUT/aligned.apk"

"$BT/apksigner.bat" verify --print-certs LifeBridge.apk \
  | sed -n 's/^Signer #1 certificate SHA-256 digest: /→ 签名 SHA-256: /p'

echo "✓ 产物：$(pwd)/LifeBridge.apk  ($VNAME / $VCODE)"
