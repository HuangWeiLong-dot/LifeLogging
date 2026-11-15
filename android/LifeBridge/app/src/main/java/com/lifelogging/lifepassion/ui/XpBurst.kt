package com.lifelogging.lifepassion.ui

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlin.math.cos
import kotlin.math.sin
import kotlin.random.Random

/**
 * 答题后的爆发反馈。
 *
 * 光有一条「+10 XP」的横幅不够 —— 用户要的是「这一刻有分量」。
 * 这里做三件事叠在一起：粒子从中心炸开、大号数字弹一下、随后的
 * 经验条和数字自己滚上去（那部分在 TODAY 页用 animateIntAsState）。
 *
 * `trigger` 每次变化就重播一次；`onDone` 在动画结束时回调。
 */
@Composable
fun XpBurst(
    trigger: Int,
    label: String,
    onDone: () -> Unit,
) {
    if (trigger <= 0) return

    // 粒子方向在每次爆发时固定下来，避免每帧抖动
    val seed = remember(trigger) { Random(trigger) }
    val particles = remember(trigger) {
        List(28) {
            val angle = (it / 16f) * 6.2832f + seed.nextFloat() * 0.25f
            val speed = 0.55f + seed.nextFloat() * 0.75f
            Triple(angle, speed, 4f + seed.nextFloat() * 6f)
        }
    }

    val t = remember(trigger) { Animatable(0f) }
    val scale = remember(trigger) { Animatable(0.5f) }
    var visible by remember(trigger) { mutableStateOf(true) }

    LaunchedEffect(trigger) {
        visible = true
        scale.animateTo(1.15f, tween(220, easing = LinearOutSlowInEasing))
        t.animateTo(1f, tween(1100, easing = LinearOutSlowInEasing))
        scale.animateTo(1f, tween(120))
        visible = false
        onDone()
    }

    if (!visible) return

    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        // 压暗底：不压的话数字直接压在卡片上，读不出来
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(Color(0xD9000000))
        )
        Canvas(modifier = Modifier.fillMaxSize()) {
            drawBurst(t.value, particles)
        }
        Text(
            label,
            fontSize = (30 * scale.value).sp,
            fontWeight = FontWeight.Bold,
            letterSpacing = 2.sp,
            color = Ink.Text,
            modifier = Modifier.padding(bottom = 60.dp),
        )
    }
}

private fun DrawScope.drawBurst(progress: Float, particles: List<Triple<Float, Float, Float>>) {
    if (progress <= 0f) return
    val cx = size.width / 2f
    val cy = size.height / 2f
    val maxR = size.minDimension * 0.45f
    val fade = (1f - progress).coerceIn(0f, 1f)

    particles.forEach { (angle, speed, radius) ->
        val r = maxR * speed * progress
        val x = cx + cos(angle) * r
        val y = cy + sin(angle) * r * 1.4f
        drawCircle(
            color = Color(0xFFF2F2F2).copy(alpha = (fade * 1.2f).coerceAtMost(1f)),
            radius = radius * (1f - progress * 0.5f),
            center = Offset(x, y),
        )
    }

    // 内圈扩散环
    if (progress < 0.6f) {
        drawCircle(
            color = Color(0xFFF2F2F2).copy(alpha = (0.6f - progress) * 0.5f),
            radius = maxR * 0.5f * progress,
            center = Offset(cx, cy),
            style = androidx.compose.ui.graphics.drawscope.Stroke(2f),
        )
    }
}
