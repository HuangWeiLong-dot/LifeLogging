package com.lifelogging.lifepassion.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

// 站点的那套：纯黑底、近白字、发丝线、宽字距。
object Ink {
    val Bg = Color(0xFF0A0A0A)
    val Surface = Color(0xFF111111)
    val Line = Color(0xFF2A2A2A)
    val Text = Color(0xFFF2F2F2)
    val Dim = Color(0xFF8A8A8A)
    val Accent = Color(0xFFF5F5F5)
}

val Mono = FontFamily.Monospace

private val scheme = darkColorScheme(
    primary = Ink.Text,
    onPrimary = Ink.Bg,
    background = Ink.Bg,
    onBackground = Ink.Text,
    surface = Ink.Surface,
    onSurface = Ink.Text,
    outline = Ink.Line,
)

private val type = Typography(
    displaySmall = TextStyle(
        fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.Bold,
        fontSize = 40.sp, letterSpacing = 4.sp, color = Ink.Text,
    ),
    titleMedium = TextStyle(
        fontFamily = FontFamily.SansSerif, fontWeight = FontWeight.Medium,
        fontSize = 15.sp, letterSpacing = 2.sp, color = Ink.Text,
    ),
    bodyMedium = TextStyle(
        fontFamily = FontFamily.SansSerif, fontSize = 14.sp,
        letterSpacing = 0.4.sp, color = Ink.Text,
    ),
    labelSmall = TextStyle(
        fontFamily = Mono, fontSize = 10.sp, letterSpacing = 1.6.sp, color = Ink.Dim,
    ),
)

@Composable
fun LifePassionTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = scheme, typography = type, content = content)
}
