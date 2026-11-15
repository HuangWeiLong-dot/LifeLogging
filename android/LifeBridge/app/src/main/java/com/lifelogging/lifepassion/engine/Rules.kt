package com.lifelogging.lifepassion.engine

import android.content.Context
import com.lifelogging.bridge.SignalStore
import com.lifelogging.lifepassion.data.Control
import com.lifelogging.lifepassion.data.Persona
import com.lifelogging.lifepassion.data.Question
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

/**
 * RULE 层 + ASSOC 层的**端上**实现。
 *
 * 数据来自采集模块留在本地的 `SignalStore`：最近一次解锁 / 亮屏 / 熄屏，
 * 当天的计数，以及最近在听什么。这些信号本来就在手机上，不需要服务器。
 *
 * 两条硬规则（飞书那批废题就是栽在这里）：
 *   1. 没有缺口就不出题。安静的一天只问固定七维。
 *   2. 每道题必须带锚点 —— 具体的小时数、时刻、歌名。没有锚点的不生成。
 */
object Rules {

    private val hm = SimpleDateFormat("HH:mm", Locale.US)

    private fun hourOf(ts: Long): Int {
        val c = Calendar.getInstance()
        c.timeInMillis = ts
        return c.get(Calendar.HOUR_OF_DAY)
    }

    /** 生成当天的动态题。返回空列表是正常情况。 */
    fun generate(ctx: Context, day: String): List<Question> {
        val now = System.currentTimeMillis()
        val out = ArrayList<Question>(3)

        val lastUnlock = SignalStore.lastAt(ctx, "unlock")
        val unlocks = SignalStore.count(ctx, "unlock")
        val hour = hourOf(now)

        // ① 长时间静默：白天、距上次解锁超过 3 小时
        if (lastUnlock > 0 && hour in 9..21) {
            val gapH = (now - lastUnlock) / 3_600_000.0
            if (gapH >= 3.0) {
                val since = hm.format(Date(lastUnlock))
                out.add(
                    Question(
                        id = "$day-gap-silent",
                        day = day,
                        persona = Persona.OBSERVER,
                        control = Control.TEXT,
                        text = "Your phone has been untouched since $since " +
                                "(${String.format(Locale.US, "%.1f", gapH)}h ago). What were you doing?",
                        hint = "Silence is not the same as absence — the data cannot tell them apart.",
                        layer = "rule",
                    )
                )
            }
        }

        // ② 整块白天没有解锁：可能没带手机，也可能真的没碰
        if (unlocks == 0 && hour >= 12) {
            out.add(
                Question(
                    id = "$day-gap-dark",
                    day = day,
                    persona = Persona.OBSERVER,
                    control = Control.TEXT,
                    text = "No unlocks recorded today. Did you leave the phone somewhere?",
                    hint = "Zero unlocks usually means the phone was not with you, not that you rested.",
                    layer = "rule",
                )
            )
        }

        // ③ ASSOC：最近听过的歌 + 当时的心情
        val mediaAt = SignalStore.mediaAt(ctx)
        val title = SignalStore.mediaTitle(ctx)
        val artist = SignalStore.mediaArtist(ctx)
        if (mediaAt > 0 && title.isNotBlank() && now - mediaAt < 4 * 3_600_000L) {
            val whenStr = hm.format(Date(mediaAt))
            val name = if (artist.isBlank()) title else "$artist — $title"
            out.add(
                Question(
                    id = "$day-assoc-media",
                    day = day,
                    persona = Persona.ROMANTICIST,
                    control = Control.TEXT,
                    text = "At $whenStr you were playing \"$name\". Did it match how you felt, " +
                            "or were you trying to change it?",
                    hint = "Music is one of the few things the record knows the name of.",
                    layer = "assoc",
                )
            )
        }

        return out
    }
}
