#!/usr/bin/env bash
# 不依赖 Gradle 的构建脚本：aapt2 → javac → d8 → zipalign → apksigner。
# 只需要 Android SDK 的 build-tools 和 JDK。
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_SDK_ROOT:-$LOCALAPPDATA/Android/Sdk}"
BT="$SDK/build-tools/36.1.0"
ANDROID_JAR="$SDK/platforms/android-36/android.jar"
[ -x "$BT/aapt2.exe" ] || BT="$SDK/build-tools/35.0.0"
[ -f "$BT/aapt2.exe" ] || { echo "找不到 build-tools：$BT"; exit 1; }
[ -f "$ANDROID_JAR" ] || { echo "找不到 android.jar：$ANDROID_JAR"; exit 1; }

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
  "$OUT/res.zip"

echo "→ 编译 Java"
find src "$OUT/gen" -name '*.java' > "$OUT/sources.txt"
# 用 -classpath 而不是 -bootclasspath：android.jar 里没有
# java.lang.invoke.LambdaMetafactory，用 bootclasspath 编译不了 lambda。
# 交给 d8 做 desugar。
javac -source 8 -target 8 -nowarn -encoding UTF-8 -classpath "$ANDROID_JAR" \
  -d "$OUT/classes" @"$OUT/sources.txt"

echo "→ dex"
find "$OUT/classes" -name '*.class' > "$OUT/classes.txt"
"$BT/d8.bat" --lib "$ANDROID_JAR" --min-api 26 --output "$OUT/dex" @"$OUT/classes.txt"

echo "→ 打包 classes.dex"
python - "$OUT" <<'PY'
import shutil, sys, zipfile, os
out = sys.argv[1]
base = os.path.join(out, "base.apk")
with zipfile.ZipFile(base, "a", zipfile.ZIP_DEFLATED) as z:
    z.write(os.path.join(out, "dex", "classes.dex"), "classes.dex")
print("classes.dex 已加入")
PY

echo "→ 对齐"
"$BT/zipalign.exe" -f 4 "$OUT/base.apk" "$OUT/aligned.apk"

if [ ! -f debug.keystore ]; then
  echo "→ 生成调试签名"
  keytool -genkeypair -v -keystore debug.keystore -storepass android \
    -alias androiddebugkey -keypass android -keyalg RSA -keysize 2048 \
    -validity 10000 -dname "CN=LifeBridge, OU=Debug, O=LifeLogging, C=CN" >/dev/null
fi

echo "→ 签名"
"$BT/apksigner.bat" sign --ks debug.keystore --ks-pass pass:android \
  --key-pass pass:android --out LifeBridge.apk "$OUT/aligned.apk"

echo "✓ 产物：$(pwd)/LifeBridge.apk"
