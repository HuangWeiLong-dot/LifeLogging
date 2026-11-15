package com.lifelogging.lifepassion.ui

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import kotlin.math.sin

/**
 * 答题后的经验流动。
 *
 * 一撮小点从**题目所在的位置**（屏幕下方）飞向右上角的经验芯片，
 * 到达之后芯片里的数字再滚上去（那部分由 `animateIntAsState` 负责）。
 *
 * 为什么不做成「从卡片的精确坐标出发」：卡片在滚动容器里，取全局坐标
 * 要跟着滚动一起重算，很容易在快速滚动时飞歪。用固定的起止比例，
 * 视觉上一样成立，而且永远不会错位。
 */
@Composable
fun XpFlow(trigger: Int, personaIndex: Int = 0, targetY: Float = 0f) {
    if (trigger <= 0) return

    val t = remember(trigger) { Animatable(0f) }
    LaunchedEffect(trigger) {
        t.animateTo(1f, tween(950, easing = LinearOutSlowInEasing))
    }
    if (t.value >= 1f) return

    Canvas(modifier = Modifier.fillMaxSize()) {
        val n = 10
        repeat(n) { i ->
            // 一颗一颗流过去：把头尾拉开，形成连续的水流而不是一团
            // 每颗从自己的时刻出发、飞固定时长 —— 这样**到达**也是逐颗的，
            // 而不是一起起飞、一起撞线。
            val start = (i / n.toFloat()) * 0.55f
            val flight = 0.45f
            val p = ((t.value - start) / flight).coerceIn(0f, 1f)
            if (p <= 0f) return@repeat
            // ⚠️ 到达就**不再画**。原来 p 被钳在 1，粒子到了终点还留在那儿，
            //    后面到的一颗颗叠上去，就成了一排不动的点。
            if (p >= 1f) return@repeat

            // 起点在下方散开
            val startX = size.width * 0.50f + (i - n / 2f) * 26f
            val startY = size.height * 0.82f
            // 终点落在**这一题所属人格**那一列，而不是固定最右边
            val colX = size.width * ((personaIndex + 0.5f) / 4f)
            val endX = colX + (i - n / 2f) * 3.5f
            // 终点高度由吸顶条实测得到 —— 之前写死 0.085 会落在空白处
            val endY = if (targetY > 0f) targetY else size.height * 0.085f

            // 加速飞出、末端收束
            val e = p * p
            val x = startX + (endX - startX) * e
            val baseY = startY + (endY - startY) * e
            // 一点弧线，别走直线
            val y = baseY - sin(p * Math.PI).toFloat() * 46f

            drawCircle(
                color = Ink.Text,
                radius = 5.5f * (1f - p * 0.45f),
                center = Offset(x, y),
                // 末段快速淡出，消失的那一刻正好落在进度条上
                alpha = (1f - p * p).coerceIn(0f, 1f),
            )
        }
    }
}
