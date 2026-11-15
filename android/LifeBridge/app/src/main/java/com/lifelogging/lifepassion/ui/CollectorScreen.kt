package com.lifelogging.lifepassion.ui

import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.app.NotificationManagerCompat
import com.lifelogging.bridge.Config
import com.lifelogging.bridge.Http
import com.lifelogging.bridge.SignalStore
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 采集端：**它录了什么、还活着吗、往哪发。**
 *
 * 原来这块是一个独立的 Java 设置页（`SettingsActivity`），只有输入框和
 * 一个测试按钮，看不出「现在到底在不在采」。这里改成状态优先：
 * 先给结论（监听权限 / 最近一次事件 / 今天记了多少），配置放后面。
 */
@Composable
fun CollectorScreen(ctx: Context) {
    val hm = remember { SimpleDateFormat("HH:mm:ss", Locale.US) }
    var url by remember { mutableStateOf(Config.url(ctx)) }
    var key by remember { mutableStateOf(Config.key(ctx)) }
    var result by remember { mutableStateOf("") }
    var saved by remember { mutableStateOf(false) }

    val listenerOk = remember {
        NotificationManagerCompat.getEnabledListenerPackages(ctx).contains(ctx.packageName)
    }
    val lastUnlock = SignalStore.lastAt(ctx, "unlock")
    val unlocks = SignalStore.count(ctx, "unlock")
    val screenOn = SignalStore.count(ctx, "screen_on")
    val mediaTitle = SignalStore.mediaTitle(ctx)
    val mediaArtist = SignalStore.mediaArtist(ctx)
    val mediaAt = SignalStore.mediaAt(ctx)

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 22.dp, vertical = 26.dp),
    ) {
        Text("COLLECTOR", fontSize = 11.sp, letterSpacing = 4.sp, color = Ink.Dim)
        Spacer(Modifier.height(6.dp))
        Text(
            "What the phone records without asking.",
            fontSize = 18.sp, fontWeight = FontWeight.Bold, color = Ink.Text,
        )

        Spacer(Modifier.height(24.dp))
        StatusRow("NOTIFICATION ACCESS", if (listenerOk) "GRANTED" else "NOT GRANTED", listenerOk)
        StatusRow("RELAY", if (url.startsWith("http")) shortHost(url) else "NOT SET", url.startsWith("http"))
        StatusRow("API KEY", if (key.isBlank()) "NOT SET" else "SET (${key.take(4)}…)", key.isNotBlank())
        StatusRow(
            "LAST UNLOCK",
            if (lastUnlock > 0) hm.format(Date(lastUnlock)) else "—",
            lastUnlock > 0,
        )
        StatusRow("UNLOCKS TODAY", "$unlocks", unlocks > 0)
        StatusRow("SCREEN-ON TODAY", "$screenOn", screenOn > 0)
        StatusRow(
            "NOW PLAYING",
            if (mediaAt > 0) {
                val name = if (mediaArtist.isBlank()) mediaTitle else "$mediaArtist — $mediaTitle"
                "$name (${hm.format(Date(mediaAt))})"
            } else "—",
            mediaAt > 0,
        )

        Spacer(Modifier.height(22.dp))
        if (!listenerOk) {
            OutlineAction(
                label = "Open notification access",
                caption = "Without this, media / payment / screen events stop arriving",
            ) {
                ctx.startActivity(
                    Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                )
            }
            Spacer(Modifier.height(10.dp))
        }

        OutlineAction(
            label = "Send a test event",
            caption = "Posts one probe to the relay and shows the HTTP result",
        ) {
            result = "sending…"
            Http.post(ctx, testBody()) { code, err ->
                Handler(Looper.getMainLooper()).post {
                    result = if (code in 200..299) "✓ delivered (HTTP $code)"
                    else "✗ ${err ?: ("HTTP $code")}"
                }
            }
        }
        if (result.isNotBlank()) {
            Spacer(Modifier.height(8.dp))
            Text(result, fontSize = 11.sp, color = Ink.Text)
        }

        Spacer(Modifier.height(28.dp))
        Text("CONNECTION", fontSize = 9.sp, letterSpacing = 2.sp, color = Ink.Dim)
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = url,
            onValueChange = { url = it; saved = false },
            label = { Text("relay ingest URL", fontSize = 11.sp) },
            modifier = Modifier.fillMaxWidth(),
            shape = androidx.compose.ui.graphics.RectangleShape,
            textStyle = androidx.compose.ui.text.TextStyle(fontSize = 12.sp, color = Ink.Text),
        )
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = key,
            onValueChange = { key = it; saved = false },
            label = { Text("X-API-Key", fontSize = 11.sp) },
            modifier = Modifier.fillMaxWidth(),
            shape = androidx.compose.ui.graphics.RectangleShape,
            textStyle = androidx.compose.ui.text.TextStyle(fontSize = 12.sp, color = Ink.Text),
        )
        Spacer(Modifier.height(12.dp))
        OutlineAction(
            label = if (saved) "Saved" else "Save connection",
            caption = "Used by every collector and by answer sync",
        ) {
            Config.prefs(ctx).edit()
                .putString("url", url.trim())
                .putString("key", key.trim())
                .apply()
            saved = true
        }

        Spacer(Modifier.height(28.dp))
        Text(
            "Payments and notification text never leave the phone in the published copy; " +
            "only local events keep them.",
            fontSize = 10.sp, color = Ink.Dim, lineHeight = 15.sp,
        )
        Spacer(Modifier.height(28.dp))
    }
}

@Composable
private fun StatusRow(label: String, value: String, ok: Boolean) {
    // ⚠️ 不要用「左右两栏 + weight」。歌名一长（"Daniel Caesar — TOO DEEP..."）
    //    就把左边的标签挤成竖排单字母。上下两行永远安全。
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 8.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .size(8.dp)
                    .background(if (ok) Ink.Text else Ink.Line)
            )
            Text(
                label,
                fontSize = 9.sp, letterSpacing = 1.6.sp, color = Ink.Dim,
                modifier = Modifier.padding(start = 10.dp),
            )
        }
        Text(
            value,
            fontSize = 12.sp,
            color = if (ok) Ink.Text else Ink.Dim,
            lineHeight = 17.sp,
            modifier = Modifier.padding(start = 18.dp, top = 3.dp),
        )
    }
}

private fun shortHost(url: String): String =
    url.removePrefix("https://").removePrefix("http://").substringBefore("/")

private fun testBody(): String {
    val e = JSONObject().apply {
        put("ts", System.currentTimeMillis() / 1000)
        put("domain", "manual")
        put("metric", "bridge_test")
        put("value_text", "from lifepassion app")
        put("source", "lifebridge")
    }
    return JSONObject().put("events", JSONArray().put(e)).toString()
}
