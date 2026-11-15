package com.lifelogging.lifepassion.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.animateIntAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.lifelogging.lifepassion.data.Emotion

/**
 * 站点是发丝线 + 直角，这里保持一致：**没有一处圆角**。
 */

/** 情绪是类别，不是冷暖渐变；多选。选中 = 填色 + 勾。 */
@Composable
fun EmotionPicker(selected: List<String>, onToggle: (Emotion) -> Unit) {
    val count by animateIntAsState(selected.size, tween(280), label = "emoCount")
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                if (count == 0) "NONE SELECTED YET" else "$count SELECTED",
                fontSize = 9.sp, letterSpacing = 1.6.sp,
                color = if (count == 0) Ink.Dim else Ink.Text,
            )
            Spacer(Modifier.weight(1f))
            Text("TAP AGAIN TO REMOVE", fontSize = 8.sp, letterSpacing = 1.sp, color = Ink.Dim)
        }
        Emotion.entries.chunked(2).forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                row.forEach { e ->
                    val on = e.label in selected
                    val bg by animateColorAsState(
                        if (on) e.color else Color.Transparent, label = "emotion"
                    )
                    Box(
                        modifier = Modifier
                            .weight(1f)
                            .height(58.dp)
                            .background(bg)
                            .border(
                                if (on) 2.dp else 1.dp,
                                if (on) e.color else Ink.Line,
                            )
                            .clickable { onToggle(e) },
                        // 顶对齐：标签和右上角的色块在同一条基准线上，
                        // 一排看过去是齐的（居中会让标签上下浮动）。
                        contentAlignment = Alignment.TopCenter,
                    ) {
                        // 色板常显：没选中时也能看见「这个情绪是什么颜色」
                        Box(
                            modifier = Modifier
                                .align(Alignment.TopEnd)
                                .padding(6.dp)
                                .size(10.dp)
                                .background(if (on) Ink.Bg else e.color)
                        )
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                        ) {
                            Text(
                                e.label,
                                fontSize = 9.sp,
                                letterSpacing = 1.sp,
                                fontWeight = FontWeight.Medium,
                                textAlign = TextAlign.Center,
                                color = if (on) Ink.Bg else Ink.Text,
                            )
                            Spacer(Modifier.height(3.dp))
                            Text(
                                if (on) "SELECTED" else "TAP TO PICK",
                                fontSize = 7.sp,
                                letterSpacing = 1.2.sp,
                                color = if (on) Ink.Bg else Ink.Dim,
                            )
                        }
                    }
                }
                repeat(2 - row.size) { Spacer(Modifier.weight(1f)) }
            }
        }
    }
}

/**
 * 1–5。**每一档都带一个词** —— 只给数字的话，读的人不知道 3 意味着什么。
 */
@Composable
fun IntensityPicker(
    level: Int?,
    captions: List<String>,
    onPick: (Int) -> Unit,
) {
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        (1..5).forEach { n ->
            val on = level == n
            Box(
                modifier = Modifier
                    .weight(1f)
                    .height(62.dp)
                    .background(if (on) Ink.Text else Color.Transparent)
                    .border(if (on) 2.dp else 1.dp, if (on) Ink.Text else Ink.Line)
                    .clickable { onPick(n) },
                contentAlignment = Alignment.Center,
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(
                        "$n",
                        fontSize = 18.sp,
                        fontWeight = FontWeight.Bold,
                        color = if (on) Ink.Bg else Ink.Text,
                    )
                    Text(
                        captions.getOrElse(n - 1) { "" }.uppercase(),
                        fontSize = 7.sp,
                        letterSpacing = 0.8.sp,
                        textAlign = TextAlign.Center,
                        color = if (on) Ink.Bg else Ink.Dim,
                    )
                }
            }
        }
    }
}

/** 通用 1–5（体力 / 睡眠质量 / 情绪强度）。 */
@Composable
fun ScaleFive(level: Int?, captions: List<String>, onPick: (Int) -> Unit) {
    IntensityPicker(level = level, captions = captions, onPick = onPick)
}

/** 进度环：完成度、人格等级都用它。 */
@Composable
fun Ring(progress: Float, size: Int = 132, label: String = "") {
    val p by animateFloatAsState(targetValue = progress.coerceIn(0f, 1f), label = "ring")
    Box(modifier = Modifier.size(size.dp), contentAlignment = Alignment.Center) {
        androidx.compose.foundation.Canvas(modifier = Modifier.size(size.dp)) {
            val stroke = 6f
            drawArc(
                color = Ink.Line,
                startAngle = -90f,
                sweepAngle = 360f,
                useCenter = false,
                style = androidx.compose.ui.graphics.drawscope.Stroke(stroke),
            )
            drawArc(
                color = Ink.Text,
                startAngle = -90f,
                sweepAngle = 360f * p,
                useCenter = false,
                style = androidx.compose.ui.graphics.drawscope.Stroke(
                    stroke, cap = androidx.compose.ui.graphics.StrokeCap.Butt
                ),
            )
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(
                "${(p * 100).toInt()}%",
                fontSize = if (size < 80) 13.sp else 26.sp,
                fontWeight = FontWeight.Bold,
                color = Ink.Text,
            )
            if (label.isNotEmpty()) {
                Text(label, style = androidx.compose.material3.MaterialTheme.typography.labelSmall)
            }
        }
    }
}

/** 明确可点的次级操作（missing / refresh 这类）。 */
@Composable
fun OutlineAction(
    label: String,
    caption: String = "",
    onClick: () -> Unit,
) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, Ink.Line)
            .clickable { onClick() }
            .padding(vertical = 12.dp, horizontal = 10.dp),
    ) {
        Column {
            Text(label.uppercase(), fontSize = 10.sp, letterSpacing = 1.8.sp, color = Ink.Text)
            if (caption.isNotEmpty()) {
                Spacer(Modifier.height(3.dp))
                Text(caption, fontSize = 9.sp, letterSpacing = 0.6.sp, color = Ink.Dim)
            }
        }
    }
}
