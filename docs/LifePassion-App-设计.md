# LifePassion（App）设计

> 2026-11-15 定稿方向。取代飞书机器人作为**主动采集**的唯一入口。
> 被动采集不动：ActivityWatch / Dawarich / Gadgetbridge / 中继全部保留。

---

## 0. 一句话

**被动采集回答「发生了什么」，LifePassion 只回答「数据里没有的那部分」。**

飞书下线的真实原因不是渠道不好，是**交互层承载不了**：文字消息做不了按钮、颜色、
撤回、进度感。换 App 不是为了好看，是为了让「答一题」的成本低到能持续一年。

---

## 0.1 语言

- **界面与问题：英文。**
- **答案：不限制语言。** 中文、英文、混写都直接存原文，不做归一化 ——
  答案是要被读的，不是要被解析成整齐格式的。分析层需要数值时，从
  `emotions` / `intensity` 这类**结构化字段**取，不从正文里猜。

## 1. 边界

| | 内容 |
|---|---|
| **被动（自动）** | 屏幕分类、手机亮屏/解锁/应用、位置停留、步数心率、音乐、通知计数、电量、网络、天气、睡眠窗口 |
| **主动（只有人能答）** | 心情与情绪、体力、睡眠质量、人际、吃饭、休息、学习、饮水/咖啡因等身体输入、承诺与完成情况 |
| **绝不做** | 录屏、录音、键盘记录、读取通知正文以外的私人内容 |

**飞书机器人**：完全下线。`server/feishu-bot/` 保留代码但停用，不再部署。
**站点**：`/life` 只读展示；**公开站点不提供答题**。CMS 后台可答题（管理端，需登录）。

---

## 2. 工程形态

- **Gradle + Kotlin + Jetpack Compose**（用户已有 Android Studio / Gradle）
- 原地改造 `android/LifeBridge`，拆成两个 module：

```
android/LifeBridge/
├── collect/     ← 现有 871 行 Java 采集：通知监听、媒体、电量、网络、心跳
└── engage/      ← 新增：问答、成就、展示（Compose）
    ├── ui/          Today / Inbox / Persona / Journal / Settings
    ├── data/        本地 Room（题目、答案、XP、待答队列）
    ├── notify/      通知栏快速答
    └── engine/      提问调度（规则 + LLM）
```

采集与问答**互不依赖**：采集挂了不影响答题，答题挂了不影响采集。

### 为什么不是 WebView

站点视觉可以复用，但通知栏快速答、离线队列、动效、生物识别都要原生。
**改为**：把站点的设计 token（色板、字体、发丝线、留白）抄进 Compose 主题，
而不是嵌页面。这样动效和手势是原生的，视觉仍然一致。

---

## 3. 信息架构（UI 全英文）

```
┌─ Today ──────────────────────────────┐
│  ◯ 完成度环  12/14                    │
│  STREAK 23 days   Lv.7 OBSERVER       │
│                                       │
│  ▸ 3 questions need you   → Inbox     │
│  ▸ 2 gaps found today     → Inbox     │
└───────────────────────────────────────┘
┌─ Inbox ──────────────────────────────┐
│  TODAY                                │
│   ⬤ Mood      ●●●●○  pick a colour    │
│   ⬤ Energy    1  2  3  4  5           │
│   ⬤ Food      + quick pick            │
│  EARLIER                              │
│   11-14 · Computer split ... (2d ago) │
│   [ answer all ]  [ mark missing ]    │
└───────────────────────────────────────┘
┌─ Persona ────────────────────────────┐
│  OBSERVER    ▓▓▓▓▓░░░  Lv.7           │
│  ADVOCATE    ▓▓▓░░░░░  Lv.4           │
│  ROMANTICIST ▓▓▓▓▓▓▓░  Lv.9           │
│  IDEALIST    ▓░░░░░░░  Lv.2           │
└───────────────────────────────────────┘
┌─ Journal ────────────────────────────┐
│  Today's recap（由 close_day 生成）    │
│  This week's discovery                │
└───────────────────────────────────────┘
┌─ Answers ────────────────────────────┐
│  最近回答，按时间倒序（飞书那套的延续）  │
│  11-15 21:40  Mood    joy+calm · 4    │
│  11-15 21:38  Energy  4               │
│  11-14 23:02  Learning 相机方面的实操    │
│  [ edit ]                             │
└───────────────────────────────────────┘
```

> **Answers 页是飞书那套的等价物。** 机器人以前会把「已答」逐条回显，
> 那是唯一能回看自己答过什么的地方。这个能力必须在 App 里保留：倒序、可编辑、
> 显示情绪色块与强度。删掉它等于把用户写过的东西藏起来。

**收工仪式（J2）**：答完最后一题 → 一屏总结（完成度、XP、今日一句话）→ 明天再来。

---

## 4. 提问引擎（三层）

```
每小时调度一次（保底）
   │
   ├─ ① FIXED 层：七维，每天必问
   │     心情(情绪+强度) / 体力 / 睡眠质量 / 人际 / 吃饭 / 休息 / 学习
   │
   ├─ ② RULE 层：被动数据有缺口才问
   │     长静默 / 无位置 / 无饮食 / 睡眠窗口缺失 / 步数异常 / 定了"我去睡了"后仍有活动
   │
   └─ ③ ASSOC 层：LLM 用**加工过的特征**生成具体问题
          "14:20–15:10 你在看《XXX》，那天心情 2 分——是在转移注意力吗？"
```

### 4.1 N1 的实际语义

用户指出「每小时分析不合理，应按数据变化」。落地为：

- **每小时跑一次**（保底，成本固定）
- 但只有**特征指纹变化超过阈值**时才调 LLM：
  - 指纹 = 当天关键特征的量化向量（手机亮屏分钟、电脑时长、位置点数、步数、最近的媒体块）
  - 变化 < 阈值 → 跳过 LLM，只跑规则层
- **同一指纹不重复分析**（N2），结果缓存到当日
- 预算：`daily_cap = ¥3`（用户确认够用），超出自动降级为"只跑规则层"

### 4.2 问题必须带锚点

每一题都携带 `anchor`：来自真实数据的数字、时刻、媒体名。
没有锚点的问题不允许生成 —— 这是飞书那批废题的根因。

### 4.3 「永不丢弃，必须回答」（I1）

- 题目进入 Inbox 后**不自动过期**
- 旧题不重复推送，只在 Inbox 里带 `2d ago` 标记
- **但**提供「标记为缺失」（M2）：这是一个有效答案，不是逃避
- 晚答的题记 `answered_late = true`，分析时可降权（M1）

---

## 5. 情绪模型（L1–L3）

**颜色 = 情绪类别；数字 = 强度。两者分别记录，再合成。**

| 情绪 | 色 | valence | arousal |
|---|---|---|---|
| anger        | red    | −0.8 | +0.9 |
| anticipation | orange | +0.4 | +0.7 |
| joy          | yellow | +0.9 | +0.8 |
| trust        | green  | +0.7 | +0.2 |
| calm         | teal   | +0.5 | −0.6 |
| sadness      | blue   | −0.8 | −0.5 |
| fear         | indigo | −0.7 | +0.8 |
| surprise     | purple |  0.0 | +0.8 |
| numb         | grey   | −0.2 | −0.8 |

- **多选**：选多个情绪 → valence/arousal 取均值，同时保留原始集合
- **强度 1–5**：单独一个控件，和颜色独立（同一屏，两个输入）
- 入库：`meta.emotions = ["joy","calm"]`，`meta.intensity = 4`
- 派生：`meta.valence`, `meta.arousal`（给分析和跨数据关联用）

---

## 6. 人格与经验（O1–O3）

四人格：**Observer / Advocate / Romanticist / Idealist**，各有独立等级。

| 人格 | 问什么 | 例子 |
|---|---|---|
| OBSERVER | 事实、数据对照、时间线 | "电脑 12h，和你感觉的一致吗？" |
| ADVOCATE | 承诺、责任、未完成 | "昨天说要改 LLS，动了吗？" |
| ROMANTICIST | 感受、情绪、审美 | "那首歌响起时你在想什么？" |
| IDEALIST | 意义、方向、长期 | "这周的重复，是你想要的吗？" |

**XP**
- 答完一题 = 10 XP；补齐一个缺口 = 20 XP
- 当天完成度 100% = +50 XP
- 答**该人格**的题，该人格 ×1.5
- 连续天数乘数：7 天 ×1.5，30 天 ×2

**等级**：不做 Lv.1–100 的线性，用站点调性的称号递进
（`WANDERER → WITNESS → CHRONICLER → ARCHIVIST → KEEPER`），每级在环上推进。

**动效（O3，参考 Duolingo）**：数字滚动、进度环填充、答对颤动、卡片翻入。
**不克制** —— 克制会让「每天来一次」变成负担。

---

## 7. 跨数据关联（Q 的核心）

用户指出：**听过的歌、看过的剧、窗口标题都应该和心情、和之前吃过的东西关联。**

### 7.1 已有的原料

- `music` 事件：歌名/歌手/专辑 + 时间点
- `screen` blocks：窗口标题（含剧集名、文件名）
- `phone` topApps / 前台区间
- `location` 停留
- `health` 睡眠/心率/步数
- `question` 答：情绪、强度、自由文本

### 7.2 关联的生成方式

**不在采集时硬关联**（会很脆），而是在**提问时动态组装**：

```
取当天情绪最低/最高的 1–2 个时刻
  → 取该时刻前后 2 小时内的媒体块 / 窗口标题 / 位置 / 上一餐
  → 组装成一个"上下文包"，交给 LLM
  → LLM 产出一道**具体**的问题（必须引用真实名字与时刻）
```

示例输出：

> `You rated 2 at 21:40 while "Blue in Green" played for 41 minutes — was the music
> making it worse, or was it already like that before it started?`

这类问题才是「只有人能回答、且答案有信息量」的。

### 7.3 反向：分析驱动提问（E4）

`patterns.py` 发现稳定关系后，生成**验证型**问题：

- 已有：`Computer hours ↔ Sleep midpoint (+0.68, n=28)`
- 生成："上周你有三天 23 点后还在电脑上，第二天都起得更晚——昨晚你在电脑上待到几点？"

**必须带具体日期与数字**（用户明确要求：不能用 raw 数据，要 LLM 加工成人话）。

---

## 8. 数据契约

### 8.1 App ↔ 中继（复用现有鉴权：X-API-Key + CF Access）

```
GET  /engage/questions?day=YYYY-MM-DD
     → { questions: [{ id, day, layer, persona, kind, control,
                       text, anchor, options, created_at }] }

POST /engage/answer
     { id, value, emotions?, intensity?, note?, answered_at }
     → { ok: true, xp: { persona, gained, level, progress } }

POST /engage/missing
     { id, reason: "skipped" | "cannot_remember" }

GET  /engage/state?day=
     → { completion: {done, total}, streak, personas: [...] }
```

### 8.2 落库

仍然写 `events` 表：

```
domain   = "question"
metric   = "answer"
value_text = 答案原文（选项 id / 自由文本）
meta = {
  qid, persona, layer, kind,
  emotions: [...], intensity, valence, arousal,
  answered_late, marked_missing,
  anchor: {...}          # 生成这题时引用的数据
}
```

答案**覆盖**旧值（C4）；`meta.revision` 记录改了几次，供分析。

### 8.3 采集部分不动

`collect/` 仍走 `POST /ingest/phone`，`source=lifebridge`，与现在完全一致。

---

## 9. 里程碑

| 阶段 | 内容 | 可验证的结果 |
|---|---|---|
| **M1** | Gradle + Compose 骨架、Today / Inbox、通知快速答（1–5） | 能在通知栏答一道题并落库 |
| **M2** | 三层提问引擎 + 情绪控件（多选 + 强度） | 一天自动生成 7 维 + 缺口题 |
| **M3** | XP / 人格 / streak / 动效 | 答完有进度环与升级动画 |
| **M4** | ASSOC 层 + patterns 反驱动 | 能收到引用真实媒体名的问题 |
| **M5** | 收工仪式 + 周/月/年环 + 本周发现 | 一天结束有闭环 |

---

## 10. 明确不做

- 不在公开站点提供答题（只读展示）
- 不恢复飞书
- 不为了「全量」去问数据已经知道的事（飞书的 3 道废题是反面教材）
- 不做无锚点的开放式问题（"今天感觉如何"这种）

---

## 11. 待定（做的时候再定）

- 通知的免打扰判定：推断窗口 vs 手动"我去睡了" vs 两者
  —— 用户已定：**都要做**，且"点了睡了之后仍有活动"本身就是第二天要追问的信号
- 队列上限：通用值 500（几乎不会触发）
- 每周发现（weekly discovery）的具体生成频率与展示位置
