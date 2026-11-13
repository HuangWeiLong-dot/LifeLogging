# LifeBridge（Android 采集 App）

一个通知监听服务，把两样 Tasker 拿不到的数据补进中继：

| 数据 | 来源 | 比 Tasker 多什么 |
|---|---|---|
| 音乐 | `MediaSession` 元数据 | 歌手、专辑、时长 |
| 支付 | 支付宝 / 微信通知 | 金额 + 来源 |

事件直接 `POST /ingest/phone`，之后走的是和 Tasker 完全相同的链路
（`collect_phone_events.py` → `events` 表 → `normalize`）。

## 构建

不需要 Gradle。要求：Android SDK（build-tools + platform）和 JDK。

```bash
cd android/LifeBridge
bash build.sh          # → LifeBridge.apk
```

脚本顺序：`aapt2 compile → aapt2 link → javac → d8 → zipalign → apksigner`。
首次运行会把 SDK 的 `android.jar` 复制到 `libs/`（供 VSCode 的 Java 扩展解析
`import android.*`），并生成签名密钥。

## 版本迭代

改 `version.properties`：

```properties
versionCode=2      # 必须递增，否则不能覆盖安装
versionName=1.1
```

manifest 里**不写**版本号——写死会覆盖 `aapt2` 传入的参数。

## 签名

- 密钥：`keystore/release.jks`
- 口令：`keystore.properties`
- 两者都在 `.gitignore` 里，**不进仓库**

当前签名（SHA-256）：

```
c023913905e7a7964d56a181db6a652037deb4f3e473fa65a13c58f2bc62dbd5
```

> ⚠️ **这套密钥必须自己备份。** 丢了以后新包签名不同，只能卸载重装，
> 设置（地址 / 密钥）要重新填一遍。
>
> 密钥文件不在仓库里，换机器时记得一起带走。

## 安装与配置

1. `adb install -r LifeBridge.apk`，或把 APK 传到手机点击安装
2. 打开 App → **打开「通知使用权」设置** → 找到 **Life Bridge** 打开
3. 填四个字段（可从现有 Tasker 的 HTTP Request 里抄）：
   - 中继地址（默认 `https://relay.liveinpassion.me/ingest/phone`）
   - `X-API-Key`
   - `CF-Access-Client-Id` / `CF-Access-Client-Secret`（走 Tailscale 时留空）
4. 点 **发送测试事件**，确认显示 `✓ 已送达（HTTP 200）`
5. 放一首歌，中继里应出现带 `meta.artist` / `meta.album` 的 `music`

> 装好之后**停用 Tasker 的 Music Track Profile**，否则两个来源各发一条，
> 播放次数会翻倍。

## 隐私边界

- **通知正文不上站**：`publish_life_raw` 发布前删掉 `title`/`text` 等字段
- **支付记录完全不上站**：`publish_life_raw` 按 `metric=payment` 整条丢弃，
  站点聚合里也没有这个字段；金额只留在本地 `events.db` 和 `02-Phone`
- 通知内容只在本机落库，不经过任何第三方

## 已知限制

- **微信支付经常不发通知**，抓不到；支付宝一般有。支付事件只能当补充，
  真正的账本用支付宝/微信导出的 CSV（`double-entry-generator` 那类工具解析）
- 酷狗概念版（`com.kugou.android.lite`）如果没有把歌手写进 MediaSession，
  `meta.artist` 会是空的，只能退回读通知文字（AutoNotification）
- MediaSession 只在通知监听权限开启时可用；权限被系统回收后要重新打开
