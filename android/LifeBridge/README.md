# LifeBridge（Android 采集 App）

一个通知监听服务 + 心跳，把 Tasker 拿不到或不稳定的数据补进中继：

| 数据 | 触发 | 说明 |
|---|---|---|
| 音乐 | 换歌时 | 歌名 / 歌手 / 专辑 / 时长；**96×96 封面**（base64） |
| 播放位置 | 播放中每 30 秒 | 歌词高亮用；暂停即停 |
| 屏幕亮灭 / 解锁 | 系统广播 | `screen_on` / `screen_off` / `unlock` |
| 电量 + 网络 | 每 15 分钟 + 插拔电源 / 网络切换 | 走 AlarmManager 的 inexact repeating |
| 支付 | 支付宝 / 微信通知 | 金额 + 来源；微信大多不发通知 |
| 连通性测试 | 设置页按钮 | `bridge_test`，会显示 HTTP 结果 |

事件直接 `POST /ingest/phone`，之后走的是和 Tasker 完全相同的链路
（`collect_phone_events.py` → `events` 表 → `normalize`）。

## 构建

不需要 Gradle。要求：Android SDK（build-tools + platform）和 JDK。

```bash
cd android/LifeBridge
bash build.sh          # → LifeBridge.apk
```

顺序：`aapt2 compile → aapt2 link → javac → d8 → zipalign → apksigner`。
首次运行会把 SDK 的 `android.jar` 复制到 `libs/`（供 VSCode 的 Java 扩展解析
`import android.*`），并按需生成签名密钥。

## 版本迭代

改 `version.properties`：

```properties
versionCode=8      # 必须递增，否则不能覆盖安装
versionName=1.7
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

## 安装与配置

1. `adb install -r LifeBridge.apk`，或把 APK 传到手机点击安装
2. 打开 App → **打开「通知使用权」设置** → 找到 **Life Bridge** 打开
3. 填四个字段（可从现有 Tasker 的 HTTP Request 里抄）：
   - 中继地址（默认 `https://relay.liveinpassion.me/ingest/phone`）
   - `X-API-Key`
   - `CF-Access-Client-Id` / `CF-Access-Client-Secret`（走 Tailscale 时留空）
4. 点 **发送测试事件**，确认显示 `✓ 已送达（HTTP 200）`
5. 放一首歌，中继里应出现带 `meta.artist` / `meta.album` / `meta.cover` 的 `music`

> 装好后可以停用 Tasker 里这些 Profile：音乐、屏幕亮灭、电量、网络。
> 同一个数据两个来源都发会重复计数（中继只按时间戳去重）。

## 常驻通知

打开 App 后会启动一个前台服务，通知栏常驻一条「LifeBridge 正在采集」。
它有两个作用：看得见服务在跑；foreground service 的优先级比普通后台进程高，
通知监听服务不容易被系统回收。Android 13+ 首次打开会请求通知权限，
不允许也不影响采集，只是看不到这条通知。

## 隐私边界

- **通知正文不上站**：`publish_life_raw` 发布前删掉 `title`/`text` 等字段
- **支付记录完全不上站**：`publish_life_raw` 按 `metric=payment` 整条丢弃，
  站点聚合里也没有这个字段；金额只留在本地 `events.db` 和 `02-Phone`
- 封面只在实时卡片上用，公开归档里会去掉（一天几十首会撑大 archive）

## 已知限制

- **微信支付经常不发通知**，抓不到；支付宝一般有。支付事件只能当补充，
  真正的账本用支付宝/微信导出的 CSV（`double-entry-generator` 那类工具解析）
- 酷狗概念版（`com.kugou.android.lite`）多数歌能拿到完整元数据；
  多歌手用中文顿号分隔（`A、B`），后端查歌词前会归一化成英文逗号
- MediaSession / 屏幕广播都依赖通知使用权和 Service 存活；被系统回收后
  重新打开 App 即可恢复
- 心跳用 inexact 闹钟，系统省电模式下可能延后到 20–30 分钟
