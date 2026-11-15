package com.lifelogging.lifepassion

import android.Manifest
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.compose.runtime.DisposableEffect
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlinx.coroutines.delay
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.slideInVertically
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.lifelogging.lifepassion.data.Answer
import com.lifelogging.lifepassion.data.Persona
import com.lifelogging.lifepassion.data.Question
import com.lifelogging.lifepassion.data.Store
import com.lifelogging.lifepassion.engine.Engine
import com.lifelogging.lifepassion.net.Relay
import com.lifelogging.lifepassion.notify.Notifier
import com.lifelogging.lifepassion.ui.AnswersScreen
import com.lifelogging.lifepassion.ui.CollectorScreen
import com.lifelogging.lifepassion.ui.InboxScreen
import com.lifelogging.lifepassion.ui.IntroScreen
import com.lifelogging.lifepassion.ui.XpBurst
import com.lifelogging.lifepassion.ui.XpFlow
import com.lifelogging.lifepassion.ui.Ink
import com.lifelogging.lifepassion.ui.LifePassionTheme
import com.lifelogging.lifepassion.ui.PersonaScreen
import com.lifelogging.lifepassion.ui.TodayScreen

class MainActivity : ComponentActivity() {

    private val askPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            askPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        // 合并之后这个包就是唯一入口：开 App 即启动采集前台服务，
        // 否则用户还得先点进 COLLECTION SETTINGS 才把采集打开。
        // 服务本身是幂等的（重复 start 只会重发同一条通知）。
        ContextCompat.startForegroundService(
            this,
            Intent(this, com.lifelogging.bridge.BridgeForegroundService::class.java),
        )

        // 深色界面：状态栏/导航栏用浅色图标
        runCatching {
            val view = window.decorView
            androidx.core.view.WindowCompat.getInsetsController(window, view)
                .isAppearanceLightStatusBars = false
            androidx.core.view.WindowCompat.getInsetsController(window, view)
                .isAppearanceLightNavigationBars = false
        }

        // 向系统申请 120Hz（设备支持才会生效，不支持时自动退回）。
        // 不写这一句的话，窗口默认可能被钉在 60Hz，动画看着发顿。
        runCatching {
            val a = window.attributes
            a.preferredRefreshRate = 120f
            window.attributes = a
        }

        setContent {
            LifePassionTheme {
                Surface(color = Ink.Bg, modifier = Modifier.fillMaxSize()) {
                    App()
                }
            }
        }
    }
}

private enum class Tab(val label: String) {
    TODAY("TODAY"), INBOX("INBOX"), PERSONAS("PERSONAS"),
    ANSWERS("ANSWERS"), COLLECTOR("COLLECTOR")
}

@Composable
private fun App() {
    val ctx = LocalContext.current
    val store = remember { Store(ctx) }

    var day by remember { mutableStateOf(store.today()) }
    var answers by remember { mutableStateOf(store.answers()) }
    var questions by remember { mutableStateOf(Engine.withDynamic(ctx, day, store)) }
    var tab by remember { mutableStateOf(Tab.TODAY) }
    var introDone by remember { mutableStateOf(store.introSeen()) }
    var flow by remember { mutableStateOf(0) }
    var flowPersona by remember { mutableStateOf(0) }
    var barY by remember { mutableStateOf(0f) }
    var burst by remember { mutableStateOf(0) }
    var burstLabel by remember { mutableStateOf("") }
    // 平时只给一条安静的确认，1.2 秒后自己消失 —— 不压暗、不挡操作
    var toast by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(toast) {
        if (toast != null) {
            delay(1200)
            toast = null
        }
    }

    // 跨过零点后自动切到新的一天；启动时补发没同步成功的答案
    LaunchedEffect(Unit) {
        val t = store.today()
        if (t != day) {
            day = t
            questions = Engine.withDynamic(ctx, day, store)
        }
        Relay.syncAll(ctx, store, questions)
    }

    // ── 服务器问题的刷新 ──────────────────────────────────
    // 只在**前台**轮询：后台每 5 分钟唤醒一次没有意义（服务器每 3 小时才
    // 重新生成一批），而且会白白耗电。切回前台立即拉一次，前台期间每
    // 5 分钟补一次，离开前台循环自动取消。
    fun pullRemote() {
        Relay.fetchQuestions(ctx, day) { remote ->
            if (remote.isEmpty()) return@fetchQuestions
            val have = questions.associateBy { it.id }.toMutableMap()
            remote.forEach { q -> if (!have.containsKey(q.id)) have[q.id] = q }
            questions = have.values.toList()
        }
    }

    val owner = LocalLifecycleOwner.current
    var resumed by remember { mutableStateOf(false) }
    DisposableEffect(owner) {
        val obs = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_RESUME -> resumed = true
                Lifecycle.Event.ON_PAUSE -> resumed = false
                else -> Unit
            }
        }
        owner.lifecycle.addObserver(obs)
        onDispose { owner.lifecycle.removeObserver(obs) }
    }
    LaunchedEffect(resumed) {
        if (!resumed) return@LaunchedEffect
        while (true) {
            pullRemote()
            delay(5 * 60 * 1000L)
        }
    }

    fun submit(q: Question, value: String, emotions: List<String>, intensity: Int?, note: String, perEmo: Map<String, Int> = emptyMap()) {
        val wasAnswered = answers.any { it.qid == q.id }
        val before = answers.count { a -> questions.any { it.id == a.qid } }
        store.save(
            Answer(
                qid = q.id,
                day = q.day,
                persona = q.persona,
                value = value,
                emotions = emotions,
                intensity = intensity,
                note = note,
                answeredAt = System.currentTimeMillis(),
                late = q.day != store.today(),
                emotionIntensity = perEmo,
            )
        )
        answers = store.answers()
        Relay.send(ctx, store, answers.first { it.qid == q.id }, q)
        if (!wasAnswered) {
            // 答该人格的题 → 该人格 +10
            store.addXp(q.persona, 10)
            flowPersona = Persona.entries.indexOf(q.persona)
            flow += 1
            toast = "✓ +10 XP · ${q.persona.label}"
        } else {
            toast = "✓ updated"
        }

        // 只有**当天全部答完**才放那个整屏特效。平时每一题都炸一次
        // 是在打断做题 —— 用户的原话：「每次点击都会出现这种感觉」。
        val after = questions.count { x -> store.answers().any { it.qid == x.id } }
        if (questions.isNotEmpty() && after == questions.size && before < questions.size) {
            store.addXp(Persona.OBSERVER, 50)
            burstLabel = "DAY COMPLETE"
            burst += 1
        }
        val done = questions.count { x -> answers.any { it.qid == x.id } }
        if (done == questions.size) {
            store.markDayCompleteIfNeeded(day)
            store.addXp(Persona.OBSERVER, 0)
        }
        Notifier.refresh(ctx, questions.count { x -> answers.none { it.qid == x.id } })
    }

    fun markMissing(q: Question) {
        store.save(
            Answer(
                qid = q.id, day = q.day, persona = q.persona,
                value = "", answeredAt = System.currentTimeMillis(),
                late = q.day != store.today(), missing = true,
            )
        )
        answers = store.answers()
    }

    val done = questions.count { q -> answers.any { it.qid == q.id } }
    val xpTotal = Persona.entries.sumOf { store.xp(it) }

    // 周期完成度：按「每天 8 道」估，答过的天按实际答案数算
    fun doneSince(days: Int): Int {
        val from = java.time.LocalDate.now().minusDays((days - 1).toLong()).toString()
        return answers.count { it.day >= from }
    }
    val weekDone = doneSince(7)
    val monthDone = doneSince(30)
    val yearDone = doneSince(365)
    val perDay = 8
    val discovery = remember(answers) { buildDiscovery(answers) }
    val breakdown = remember(questions) {
        val byLayer = questions.groupingBy { it.layer }.eachCount()
        buildString {
            append("${questions.size} today")
            listOf("fixed" to "routine", "rule" to "from gaps", "assoc" to "from data")
                .forEach { (k, label) ->
                    val n = byLayer[k] ?: 0
                    if (n > 0) append(" · $n $label")
                }
        }
    }

    if (!introDone) {
        IntroScreen(onStart = {
            store.markIntroSeen()
            introDone = true
        })
        return
    }

    // ⚠️ targetSdk 36 起强制 edge-to-edge：不自己留出系统栏，内容会钻到
    //    状态栏和导航栏底下（实测第一版就是这样，标题直接被状态栏盖住）。
    Column(
        modifier = Modifier
            .fillMaxSize()
            .windowInsetsPadding(WindowInsets.safeDrawing)
    ) {
        Box(modifier = Modifier.weight(1f)) {
            when (tab) {
                Tab.TODAY -> TodayScreen(
                    day = day,
                    done = done,
                    total = questions.size,
                    streak = store.streak(),
                    xpTotal = xpTotal,
                    onOpenInbox = { tab = Tab.INBOX },
                    weekPct = (weekDone.toFloat() / (7 * perDay)).coerceIn(0f, 1f),
                    monthPct = (monthDone.toFloat() / (30 * perDay)).coerceIn(0f, 1f),
                    yearPct = (yearDone.toFloat() / (365 * perDay)).coerceIn(0f, 1f),
                    discovery = discovery,
                    breakdown = breakdown,
                    onOpenCollection = { tab = Tab.COLLECTOR },
                )
                Tab.INBOX -> InboxScreen(
                    questions = questions,
                    answers = answers,
                    onAnswer = ::submit,
                    onMissing = ::markMissing,
                    onRefresh = { pullRemote() },
                    xpOf = { store.xp(it) },
                    xpPopTrigger = flow,
                    xpPopPersona = flowPersona,
                    onBarY = { barY = it },
                    onResetToday = {
                        Relay.resetDay(ctx, day)
                        store.resetDay(day)
                        answers = store.answers()
                        questions = Engine.withDynamic(ctx, day, store)
                        flow = 0
                        burst = 0
                    },
                )
                Tab.PERSONAS -> PersonaScreen(xpOf = { store.xp(it) })
                Tab.ANSWERS -> AnswersScreen(answers = answers)
                Tab.COLLECTOR -> CollectorScreen(ctx)
            }
            XpFlow(trigger = flow, personaIndex = flowPersona, targetY = barY)
            XpBurst(trigger = burst, label = burstLabel) { }

            // 安静确认：底部居中一条细带，不压暗、不拦触摸
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.BottomCenter,
            ) {
                androidx.compose.animation.AnimatedVisibility(
                    visible = toast != null,
                    enter = fadeIn() + slideInVertically { it / 2 },
                ) {
                    Box(
                        modifier = Modifier
                            .padding(bottom = 18.dp)
                            .background(Ink.Surface)
                            .border(1.dp, Ink.Line)
                            .padding(horizontal = 14.dp, vertical = 8.dp)
                    ) {
                        Text(
                            toast ?: "",
                            fontSize = 10.sp,
                            letterSpacing = 1.4.sp,
                            color = Ink.Text,
                        )
                    }
                }
            }
        }
        BottomBar(current = tab, onPick = { tab = it })
    }
}

/** 本周发现：完全本地算，不调模型 —— 先把「有反馈」这件事做出来。 */
private fun buildDiscovery(answers: List<Answer>): String {
    val from = java.time.LocalDate.now().minusDays(6).toString()
    val week = answers.filter { it.day >= from && !it.missing }
    if (week.isEmpty()) return "Nothing answered this week yet."
    val moods = week.flatMap { it.emotions }
    val top = moods.groupingBy { it }.eachCount().maxByOrNull { it.value }
    val scored = week.mapNotNull { it.intensity }
    val avg = if (scored.isEmpty()) null else scored.average()
    return buildString {
        append("${week.size} answers this week")
        top?.let { append(" · most often felt ${it.key.lowercase()}") }
        avg?.let { append(" · avg intensity ${String.format(java.util.Locale.US, "%.1f", it)}") }
        append(".")
    }
}

/** 图标全部用基本几何形画 —— 不加图标库，也不引入圆角。 */
@Composable
private fun TabIcon(t: Tab, on: Boolean) {
    val c = if (on) Ink.Text else Ink.Dim
    Canvas(modifier = Modifier.size(15.dp)) {
        val w = size.width
        val s = Stroke(width = 1.4f)
        when (t) {
            Tab.TODAY -> drawCircle(c, radius = w * 0.34f, style = s)
            Tab.INBOX -> {
                drawRect(c, topLeft = Offset(0f, w * 0.12f),
                    size = Size(w, w * 0.76f), style = s)
                drawLine(c, Offset(w * 0.25f, w * 0.36f),
                    Offset(w * 0.75f, w * 0.36f), 1.4f)
                drawLine(c, Offset(w * 0.25f, w * 0.58f),
                    Offset(w * 0.62f, w * 0.58f), 1.4f)
            }
            Tab.PERSONAS -> {
                val q = w * 0.42f
                drawRect(c, Offset(0f, 0f), Size(q, q), style = s)
                drawRect(c, Offset(w - q, 0f), Size(q, q), style = s)
                drawRect(c, Offset(0f, w - q), Size(q, q), style = s)
                drawRect(c, Offset(w - q, w - q), Size(q, q), style = s)
            }
            Tab.ANSWERS -> {
                for (i in 0..2) {
                    val y = w * (0.2f + i * 0.3f)
                    drawLine(c, Offset(0f, y), Offset(w * (if (i == 2) 0.6f else 1f), y), 1.4f)
                }
            }
            Tab.COLLECTOR -> {
                drawLine(c, Offset(w * 0.2f, w), Offset(w * 0.2f, w * 0.5f), 1.4f)
                drawLine(c, Offset(w * 0.5f, w), Offset(w * 0.5f, w * 0.2f), 1.4f)
                drawLine(c, Offset(w * 0.8f, w), Offset(w * 0.8f, w * 0.65f), 1.4f)
            }
        }
    }
}

@Composable
private fun BottomBar(current: Tab, onPick: (Tab) -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(Ink.Surface)
            .padding(vertical = 14.dp),
        horizontalArrangement = Arrangement.SpaceEvenly,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Tab.entries.forEach { t ->
            val on = t == current
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier
                    .weight(1f)
                    .clickable { onPick(t) }
                    .padding(horizontal = 2.dp)
            ) {
                // ⚠️ 这里必须用 tab 自己的宽度，不能用 fillMaxWidth(1f) ——
                //    那会让选中的 tab 吃掉整行，另外四个被挤出屏幕
                //    （实测：底部只剩 TODAY）。
                Box(
                    modifier = Modifier
                        .height(2.dp)
                        .fillMaxWidth()
                        .background(if (on) Ink.Text else Color.Transparent)
                )
                Spacer(Modifier.height(8.dp))
                TabIcon(t, on)
                Text(
                    t.label,
                    fontSize = 8.sp,
                    letterSpacing = 1.sp,
                    fontWeight = if (on) FontWeight.Bold else FontWeight.Normal,
                    color = if (on) Ink.Text else Ink.Dim,
                    modifier = Modifier.padding(top = 6.dp),
                )
            }
        }
    }
}
