package com.lifelogging.lifepassion.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateIntAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.slideInVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.layout.positionInParent
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.lifelogging.lifepassion.data.Answer
import com.lifelogging.lifepassion.data.Control
import com.lifelogging.lifepassion.data.Emotion
import com.lifelogging.lifepassion.data.Persona
import com.lifelogging.lifepassion.data.Question

private val cardShape = androidx.compose.ui.graphics.RectangleShape

@Composable
private fun Card(content: @Composable () -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            
            .background(Ink.Surface)
            .border(1.dp, Ink.Line, cardShape)
            .padding(18.dp)
    ) { content() }
}

@Composable
fun TodayScreen(
    day: String,
    done: Int,
    total: Int,
    streak: Int,
    xpTotal: Int,
    onOpenInbox: () -> Unit,
    onOpenCollection: () -> Unit,
    weekPct: Float = 0f,
    monthPct: Float = 0f,
    yearPct: Float = 0f,
    discovery: String = "",
    breakdown: String = "",
) {
    val progress = if (total == 0) 0f else done.toFloat() / total

    // ⚠️ 必须能滚。加了周/月/年条和发现卡之后，固定高度会把「答题」
    //    按钮挤到屏幕外 —— 实测就是「找不到入口」。
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 22.dp, vertical = 26.dp),
    ) {
        Text("TODAY", fontSize = 11.sp, letterSpacing = 4.sp, color = Ink.Dim)
        Spacer(Modifier.height(6.dp))
        Text(day, fontSize = 22.sp, fontWeight = FontWeight.Bold, color = Ink.Text)

        Spacer(Modifier.height(22.dp))
        // 百分比不套圆圈：一个大数字 + 一条实心横条
        Row(verticalAlignment = Alignment.Bottom) {
            Text(
                "${(progress * 100).toInt()}",
                fontSize = 54.sp,
                fontWeight = FontWeight.Bold,
                color = Ink.Text,
            )
            Text("%", fontSize = 18.sp, color = Ink.Dim,
                modifier = Modifier.padding(bottom = 10.dp, start = 2.dp))
            Spacer(Modifier.weight(1f))
            Text(
                "$done of $total answered",
                fontSize = 11.sp, letterSpacing = 1.sp, color = Ink.Dim,
                modifier = Modifier.padding(bottom = 10.dp),
            )
        }
        Spacer(Modifier.height(10.dp))
        ProgressBar(progress)

        Spacer(Modifier.height(22.dp))
        // 主入口放在这里，不用滚就能看见
        Button(
            onClick = onOpenInbox,
            modifier = Modifier.fillMaxWidth().height(52.dp),
            colors = ButtonDefaults.buttonColors(
                containerColor = Ink.Text, contentColor = Ink.Bg
            ),
            shape = cardShape,
        ) {
            Text(
                if (done >= total) "REVIEW TODAY" else "ANSWER ${total - done}",
                letterSpacing = 3.sp,
                fontWeight = FontWeight.Bold,
                fontSize = 13.sp,
            )
        }

        Spacer(Modifier.height(26.dp))
        // 三项居中均分，不再靠左挤在一起
        val xpShown by animateIntAsState(xpTotal, tween(700), label = "xp")
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceEvenly,
        ) {
            Stat("STREAK", "$streak", "days")
            Stat("XP", "$xpShown", "total")
            Stat("LEFT", "${total - done}", "to answer")
        }

        Spacer(Modifier.height(26.dp))
        PeriodRow("WEEK", weekPct)
        Spacer(Modifier.height(10.dp))
        PeriodRow("MONTH", monthPct)
        Spacer(Modifier.height(10.dp))
        PeriodRow("YEAR", yearPct)

        if (discovery.isNotBlank()) {
            Spacer(Modifier.height(24.dp))
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, Ink.Line)
                    .padding(16.dp)
            ) {
                Column {
                    Text("THIS WEEK", fontSize = 9.sp, letterSpacing = 2.sp, color = Ink.Dim)
                    Spacer(Modifier.height(6.dp))
                    Text(discovery, fontSize = 13.sp, color = Ink.Text, lineHeight = 19.sp)
                }
            }
        }

        if (breakdown.isNotBlank()) {
            Spacer(Modifier.height(14.dp))
            Text(breakdown, fontSize = 10.sp, letterSpacing = 1.sp, color = Ink.Dim)
        }

        Spacer(Modifier.height(10.dp))
        Text(
            "Nothing is dropped. Old questions wait in Inbox.",
            fontSize = 10.sp, letterSpacing = 1.sp, color = Ink.Dim,
        )

        Spacer(Modifier.height(24.dp))
        OutlineAction(
            label = "Collector",
            caption = "What is being recorded, and whether it is still running",
            onClick = onOpenCollection,
        )
        Spacer(Modifier.height(28.dp))
    }
}

/** 一条实心横条。没有圆角、没有环。 */
@Composable
private fun ProgressBar(pct: Float) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(8.dp)
            .background(Ink.Line)
    ) {
        Box(
            modifier = Modifier
                .fillMaxWidth(pct.coerceIn(0f, 1f))
                .height(8.dp)
                .background(Ink.Text)
        )
    }
}

@Composable
private fun PeriodRow(label: String, pct: Float) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(label, fontSize = 9.sp, letterSpacing = 1.8.sp, color = Ink.Dim,
            modifier = Modifier.width(52.dp))
        Box(
            modifier = Modifier
                .weight(1f)
                .height(5.dp)
                .background(Ink.Line)
        ) {
            Box(
                modifier = Modifier
                    .fillMaxWidth(pct.coerceIn(0f, 1f))
                    .height(5.dp)
                    .background(Ink.Dim)
            )
        }
        Text(
            "${(pct * 100).toInt()}%",
            fontSize = 10.sp, color = Ink.Dim,
            modifier = Modifier.width(44.dp), textAlign = TextAlign.End,
        )
    }
}

@Composable
private fun Stat(label: String, value: String, unit: String) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text(value, fontSize = 24.sp, fontWeight = FontWeight.Bold, color = Ink.Text)
        Text(label, fontSize = 9.sp, letterSpacing = 2.sp, color = Ink.Dim)
        Text(unit, fontSize = 9.sp, color = Ink.Dim)
    }
}

@Composable
fun InboxScreen(
    questions: List<Question>,
    answers: List<Answer>,
    onAnswer: (Question, String, List<String>, Int?, String, Map<String, Int>) -> Unit,
    onMissing: (Question) -> Unit,
    onRefresh: () -> Unit = {},
    xpOf: (Persona) -> Int = { 0 },
    onResetToday: () -> Unit = {},
    xpPopTrigger: Int = 0,
    xpPopPersona: Int = -1,
    onBarY: (Float) -> Unit = {},
) {
    // 每次重组都重建 map 会在答题时反复分配 —— 答案变了才重算
    val byId = remember(answers) { answers.associateBy { it.qid } }
    val listState = androidx.compose.foundation.lazy.rememberLazyListState()

    // 只有**开始滚动之后**才把四个人格吸到顶部。
    // 停在第一屏时它们是隐藏的 —— 题目卡片上已经写了这一题属于谁。
    val scrolled by androidx.compose.runtime.derivedStateOf {
        listState.firstVisibleItemIndex > 0 || listState.firstVisibleItemScrollOffset > 60
    }

    Box(modifier = Modifier.fillMaxSize()) {
        LazyColumn(
            state = listState,
            modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            item {
                Column(modifier = Modifier.padding(top = 20.dp, bottom = 4.dp)) {
                    Text("INBOX", fontSize = 11.sp, letterSpacing = 4.sp, color = Ink.Dim)
                    Spacer(Modifier.height(4.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "${questions.count { it.id !in byId }} open",
                            fontSize = 20.sp, fontWeight = FontWeight.Bold, color = Ink.Text,
                        )
                        Spacer(Modifier.weight(1f))
                        var confirmReset by remember { mutableStateOf(false) }
                        Box(
                            modifier = Modifier
                                .border(1.dp, Ink.Line)
                                .clickable {
                                    if (confirmReset) {
                                        onResetToday()
                                        confirmReset = false
                                    } else {
                                        confirmReset = true
                                    }
                                }
                                .padding(horizontal = 10.dp, vertical = 6.dp)
                        ) {
                            Text(
                                if (confirmReset) "SURE?" else "RESET",
                                fontSize = 9.sp, letterSpacing = 1.6.sp,
                                color = if (confirmReset) Ink.Text else Ink.Dim,
                            )
                        }
                        Spacer(Modifier.width(8.dp))
                        Box(
                            modifier = Modifier
                                .border(1.dp, Ink.Line)
                                .clickable { onRefresh() }
                                .padding(horizontal = 10.dp, vertical = 6.dp)
                        ) {
                            Text("REFRESH", fontSize = 9.sp, letterSpacing = 1.6.sp,
                                color = Ink.Text)
                        }
                    }
                }
            }
            val openCount = questions.count { it.id !in byId }
            if (openCount == 0) {
                item {
                    Box(
                        modifier = Modifier.fillMaxWidth().border(1.dp, Ink.Line).padding(20.dp)
                    ) {
                        Column {
                            Text("ALL ANSWERED", fontSize = 10.sp, letterSpacing = 2.sp,
                                color = Ink.Text)
                            Spacer(Modifier.height(6.dp))
                            Text(
                                "Today is complete. Come back tomorrow — or review what you wrote in Answers.",
                                fontSize = 12.sp, color = Ink.Dim, lineHeight = 18.sp,
                            )
                        }
                    }
                }
            }
            items(questions, key = { it.id }) { q ->
                QuestionCard(
                    q = q,
                    existing = byId[q.id],
                    onAnswer = onAnswer,
                    onMissing = onMissing,
                )
            }
            item { Spacer(Modifier.height(24.dp)) }
        }

        // 吸顶的四个人格。经验流动的落点就是这块。
        AnimatedVisibility(
            visible = scrolled,
            modifier = Modifier.align(Alignment.TopCenter),
            enter = androidx.compose.animation.slideInVertically { -it } +
                androidx.compose.animation.fadeIn(),
            exit = androidx.compose.animation.slideOutVertically { -it } +
                androidx.compose.animation.fadeOut(),
        ) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(Ink.Surface)
                    .onGloballyPositioned { c ->
                        // 进度条的基准线：整块底部往上一点点
                        val bottom = c.positionInParent().y + c.size.height
                        onBarY(bottom - 22f)
                    }
                    .padding(horizontal = 20.dp, vertical = 10.dp),
                horizontalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                Persona.entries.forEachIndexed { idx, p2 ->
                    // 数字**逐颗累加**：粒子一颗颗落下来，数字就 +1 一次，
                    // 进度条跟着走。不是一次性跳到目标值。
                    val targetXp = xpOf(p2)
                    var shown by remember { mutableStateOf(targetXp) }
                    LaunchedEffect(xpPopTrigger) {
                        if (xpPopTrigger == 0 || idx != xpPopPersona) {
                            shown = targetXp
                            return@LaunchedEffect
                        }
                        val from = shown
                        val steps = (targetXp - from).coerceAtLeast(1)
                        kotlinx.coroutines.delay(120)
                        repeat(steps) {
                            kotlinx.coroutines.delay(70)
                            shown = (from + it + 1).coerceAtMost(targetXp)
                        }
                        shown = targetXp
                    }
                    val xp = shown
                    // 粒子一颗颗撞上来时，对应那一列跳一下
                    val pop = remember { androidx.compose.animation.core.Animatable(1f) }
                    // ⚠️ 记住「进来时看到的 trigger」。只是滚动让吸顶条重新
                    //    进入组合时，trigger 没变，不能当成一次新命中。
                    var seenTrigger by remember { mutableStateOf(xpPopTrigger) }
                    LaunchedEffect(xpPopTrigger) {
                        if (xpPopTrigger == seenTrigger) return@LaunchedEffect
                        seenTrigger = xpPopTrigger
                        if (idx != xpPopPersona) return@LaunchedEffect
                        repeat(3) {
                            pop.animateTo(1.28f, tween(90))
                            pop.animateTo(1f, tween(130))
                            kotlinx.coroutines.delay(40)
                        }
                    }
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            p2.label.take(3),
                            fontSize = 8.sp, letterSpacing = 1.4.sp, color = Ink.Dim,
                        )
                        Spacer(Modifier.height(3.dp))
                        Text(
                            "$xp",
                            fontSize = 15.sp, fontWeight = FontWeight.Bold,
                            color = Ink.Text,
                            modifier = Modifier.graphicsLayer {
                                scaleX = pop.value; scaleY = pop.value
                            },
                        )
                        Spacer(Modifier.height(4.dp))
                        Box(
                            modifier = Modifier.fillMaxWidth().height(2.dp).background(Ink.Line)
                        ) {
                            Box(
                                modifier = Modifier
                                    .fillMaxWidth(((xp % 100) / 100f).coerceIn(0f, 1f))
                                    .height(2.dp)
                                    .background(Ink.Text)
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun QuestionCard(
    q: Question,
    existing: Answer?,
    onAnswer: (Question, String, List<String>, Int?, String, Map<String, Int>) -> Unit,
    onMissing: (Question) -> Unit,
    onRefresh: () -> Unit = {},
    xpOf: (Persona) -> Int = { 0 },
    onResetToday: () -> Unit = {},
    xpPopTrigger: Int = 0,
    xpPopPersona: Int = -1,
    onBarY: (Float) -> Unit = {},
) {
    var emotions by remember(q.id) {
        mutableStateOf(existing?.emotions ?: emptyList<String>())
    }
    var intensity by remember(q.id) { mutableStateOf(existing?.intensity) }
    var text by remember(q.id) { mutableStateOf(existing?.value ?: "") }
    // 每个情绪自己的强度：颜色说「哪一种」，数字说「多强」
    var perEmo by remember(q.id) {
        mutableStateOf(existing?.emotionIntensity ?: emptyMap<String, Int>())
    }

    Card {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    q.persona.label,
                    fontSize = 9.sp, letterSpacing = 2.sp, color = Ink.Dim,
                )
                Spacer(Modifier.weight(1f))
                if (existing != null) {
                    Text(
                        if (existing.missing) "MARKED MISSING" else "SAVED",
                        fontSize = 9.sp, letterSpacing = 1.4.sp, color = Ink.Dim,
                    )
                }
            }
            Text(q.text, fontSize = 15.sp, color = Ink.Text, lineHeight = 21.sp)
            Text(q.hint, fontSize = 10.sp, color = Ink.Dim, lineHeight = 14.sp)

            when (q.control) {
                Control.EMOTION -> {
                    EmotionPicker(emotions) { e ->
                        emotions = if (e.label in emotions) emotions - e.label
                        else emotions + e.label
                        if (e.label !in emotions) perEmo = perEmo - e.label
                        onAnswer(q, text, emotions, intensity, "", perEmo)
                    }
                    if (emotions.isNotEmpty()) {
                        Text(
                            "HOW STRONG · EACH ONE",
                            fontSize = 9.sp, letterSpacing = 2.sp, color = Ink.Dim,
                        )
                        emotions.forEach { label ->
                            val e = Emotion.entries.firstOrNull { it.label == label }
                            // 标签吃掉剩余宽度，刻度就被推到最右边 ——
                            // 最后一个格子和上方情绪格子的右边界对齐。
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Box(
                                    modifier = Modifier
                                        .size(12.dp)
                                        .background(e?.color ?: Ink.Line)
                                )
                                Text(
                                    label,
                                    fontSize = 11.sp, letterSpacing = 1.4.sp,
                                    color = Ink.Text,
                                    modifier = Modifier
                                        .weight(1f)
                                        .padding(start = 10.dp),
                                )
                                MiniScale(perEmo[label]) { n ->
                                    perEmo = perEmo + (label to n)
                                    intensity = perEmo.values.maxOrNull()
                                    onAnswer(q, text, emotions, intensity, "", perEmo)
                                }
                            }
                            Spacer(Modifier.height(6.dp))
                        }
                    }
                }
                Control.SCALE5 -> {
                    val labels = if (q.id.endsWith("-sleep"))
                        listOf("bad", "poor", "okay", "good", "great")
                    else
                        listOf("drained", "low", "okay", "good", "full")
                    ScaleFive(intensity, labels) { n ->
                        intensity = n
                        onAnswer(q, "$n", emotions, n, "", perEmo)
                    }
                }
                Control.TEXT -> {
                    OutlinedTextField(
                        value = text,
                        onValueChange = { text = it },
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 2,
                        placeholder = {
                            Text("one line is enough", fontSize = 12.sp, color = Ink.Dim)
                        },
                        shape = cardShape,
                    )
                    Button(
                        onClick = { onAnswer(q, text, emotions, intensity, text, perEmo) },
                        enabled = text.isNotBlank(),
                        modifier = Modifier.fillMaxWidth(),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = Ink.Text,
                            contentColor = Ink.Bg,
                            disabledContainerColor = Ink.Surface,
                            disabledContentColor = Ink.Dim,
                        ),
                        shape = cardShape,
                    ) {
                        Text(
                            if (text.isBlank()) "TYPE AN ANSWER FIRST" else "SUBMIT ANSWER",
                            letterSpacing = 2.sp,
                            fontWeight = FontWeight.Bold,
                        )
                    }
                }
                Control.BOOLEAN -> Unit
            }

            if (existing == null) {
                OutlineAction(
                    label = "Mark as missing",
                    caption = "I don't remember, or there is nothing to report",
                    onClick = { onMissing(q) },
                )
            }
        }
    }
}

/** 首次启动：说明这四个 page 是干嘛的，然后再进主界面。 */
@Composable
fun IntroScreen(onStart: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().padding(28.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text("LIFEPASSION", fontSize = 26.sp, fontWeight = FontWeight.Bold,
            letterSpacing = 4.sp, color = Ink.Text)
        Spacer(Modifier.height(14.dp))
        Text(
            "Everything else about your day is recorded automatically. " +
            "This app only asks for the part the record cannot see.",
            fontSize = 13.sp, color = Ink.Dim, lineHeight = 20.sp,
        )
        Spacer(Modifier.height(28.dp))
        listOf(
            "OBSERVER" to "facts and numbers, checked against how the day felt",
            "ADVOCATE" to "what you said you would do, and whether it happened",
            "ROMANTICIST" to "moments, music, mood — the texture of the day",
            "IDEALIST" to "whether the shape of the week is the one you want",
        ).forEach { (name, desc) ->
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .border(1.dp, Ink.Line)
                    .padding(14.dp)
            ) {
                Column {
                    Text(name, fontSize = 10.sp, letterSpacing = 2.sp, color = Ink.Text)
                    Spacer(Modifier.height(4.dp))
                    Text(desc, fontSize = 11.sp, color = Ink.Dim, lineHeight = 16.sp)
                }
            }
            Spacer(Modifier.height(8.dp))
        }
        Spacer(Modifier.height(20.dp))
        Button(
            onClick = onStart,
            modifier = Modifier.fillMaxWidth(),
            colors = ButtonDefaults.buttonColors(
                containerColor = Ink.Text, contentColor = Ink.Bg
            ),
            shape = cardShape,
        ) { Text("START", letterSpacing = 3.sp, fontWeight = FontWeight.Bold) }
    }
}

/** 紧凑的 1–5，只画数字，没有词 —— 每行一个情绪时用。 */
@Composable
private fun MiniScale(value: Int?, onPick: (Int) -> Unit) {
    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        (1..5).forEach { n ->
            val on = value == n
            Box(
                modifier = Modifier
                    .size(34.dp)
                    .background(if (on) Ink.Text else androidx.compose.ui.graphics.Color.Transparent)
                    .border(1.dp, if (on) Ink.Text else Ink.Line)
                    .clickable { onPick(n) },
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    "$n",
                    fontSize = 12.sp,
                    fontWeight = if (on) FontWeight.Bold else FontWeight.Normal,
                    color = if (on) Ink.Bg else Ink.Dim,
                )
            }
        }
    }
}

@Composable
fun PersonaScreen(xpOf: (Persona) -> Int) {
    // ⚠️ 四张卡**均分整屏高度**。之前用 LazyColumn，四张卡只占上半屏，
    //    下面一大片空白。用权重分配之后有留白但不空，边距由外层 padding 给。
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(horizontal = 20.dp, vertical = 20.dp),
    ) {
        Text("PERSONAS", fontSize = 11.sp, letterSpacing = 4.sp, color = Ink.Dim)
        Spacer(Modifier.height(4.dp))
        Text(
            "Four ways of looking at the same day.",
            fontSize = 18.sp, fontWeight = FontWeight.Bold, color = Ink.Text,
        )
        Spacer(Modifier.height(18.dp))

        Persona.entries.forEach { p ->
            val xp = xpOf(p)
            val level = xp / 100 + 1
            val pct = (xp % 100) / 100f
            Box(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .border(1.dp, Ink.Line)
                    .padding(18.dp),
                contentAlignment = Alignment.CenterStart,
            ) {
                Column(verticalArrangement = Arrangement.Center) {
                    Row(verticalAlignment = Alignment.Bottom) {
                        Text(
                            p.label,
                            fontSize = 14.sp, letterSpacing = 2.sp, color = Ink.Text,
                        )
                        Spacer(Modifier.weight(1f))
                        Text("Lv.$level", fontSize = 15.sp, color = Ink.Text)
                    }
                    Spacer(Modifier.height(10.dp))
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(6.dp)
                            .background(Ink.Line)
                    ) {
                        Box(
                            modifier = Modifier
                                .fillMaxWidth(pct.coerceIn(0f, 1f))
                                .height(6.dp)
                                .background(Ink.Text)
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "$xp XP · ${100 - (xp % 100)} to next level",
                        fontSize = 9.sp, letterSpacing = 1.sp, color = Ink.Dim,
                    )
                }
            }
            Spacer(Modifier.height(12.dp))
        }
    }
}

@Composable
fun AnswersScreen(answers: List<Answer>) {
    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Column(modifier = Modifier.padding(top = 20.dp)) {
                Text("ANSWERS", fontSize = 11.sp, letterSpacing = 4.sp, color = Ink.Dim)
                Spacer(Modifier.height(4.dp))
                Text(
                    "${answers.size} recorded",
                    fontSize = 20.sp, fontWeight = FontWeight.Bold, color = Ink.Text,
                )
            }
        }
        if (answers.isEmpty()) {
            item {
                Text(
                    "Nothing yet. Answer something in Inbox.",
                    fontSize = 12.sp, color = Ink.Dim,
                )
            }
        }
        items(answers, key = { it.qid }) { a ->
            Card {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(a.day, fontSize = 9.sp, letterSpacing = 1.4.sp, color = Ink.Dim)
                        Spacer(Modifier.weight(1f))
                        Text(
                            a.persona.label,
                            fontSize = 9.sp, letterSpacing = 1.4.sp, color = Ink.Dim,
                        )
                    }
                    if (a.missing) {
                        Text("marked missing", fontSize = 13.sp, color = Ink.Dim)
                    } else if (a.emotions.isNotEmpty()) {
                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            a.emotions.forEach { name ->
                                val e = Emotion.entries.firstOrNull { it.label == name }
                                Box(
                                    modifier = Modifier
                                        .size(14.dp)
                                        .clip(androidx.compose.ui.graphics.RectangleShape)
                                        .background(e?.color ?: Color.Gray)
                                )
                            }
                            if (a.intensity != null) {
                                Text("· ${a.intensity}/5", fontSize = 12.sp, color = Ink.Dim)
                            }
                        }
                    }
                    val body = if (a.value.isNotBlank()) a.value else a.note
                    if (body.isNotBlank()) {
                        Text(body, fontSize = 14.sp, color = Ink.Text)
                    }
                }
            }
        }
        item { Spacer(Modifier.height(24.dp)) }
    }
}
