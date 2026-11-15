package com.lifelogging.lifepassion.engine

import android.content.Context
import com.lifelogging.lifepassion.data.Control
import com.lifelogging.lifepassion.data.Persona
import com.lifelogging.lifepassion.data.Question
import com.lifelogging.lifepassion.data.Store

/**
 * 第一版提问引擎：FIXED 层（七维 + 一条问责）。
 *
 * RULE / ASSOC 两层要等被动数据能进到 App 里再做 —— 现在 App 只拿到
 * 本机没有的原始事件，硬造缺口只会问出飞书那种废题。
 */
object Engine {

    private data class Spec(
        val key: String,
        val persona: Persona,
        val control: Control,
        val text: String,
        val hint: String,
    )

    private val FIXED = listOf(
        Spec("mood", Persona.ROMANTICIST, Control.EMOTION,
            "How did today feel?",
            "Pick the colours that were present, then how strong they were."),
        Spec("energy", Persona.OBSERVER, Control.SCALE5,
            "How was your physical energy?",
            "1 drained — 5 full."),
        Spec("sleep", Persona.OBSERVER, Control.SCALE5,
            "How did you sleep last night?",
            "1 bad — 5 great."),
        Spec("social", Persona.ROMANTICIST, Control.TEXT,
            "Who did you spend time with? What did you talk about?",
            "Location only says you were nearby."),
        Spec("food", Persona.OBSERVER, Control.TEXT,
            "What did you eat today? Were you ever too hungry or too full?",
            "Meals do not produce events."),
        Spec("rest", Persona.IDEALIST, Control.TEXT,
            "Did you step away from the screen? Doing what?",
            "Rest and entertainment look the same in the data."),
        Spec("learn", Persona.IDEALIST, Control.TEXT,
            "What did you learn today that you did not know before?",
            "A window title never says what you understood."),
    )

    fun questionsFor(day: String, store: Store): List<Question> {
        val qs = ArrayList<Question>(FIXED.size + 1)
        FIXED.forEach { s ->
            qs.add(
                Question(
                    id = "$day-${s.key}",
                    day = day,
                    persona = s.persona,
                    control = s.control,
                    text = s.text,
                    hint = s.hint,
                )
            )
        }
        // 问责题：把前一天「没做完的事」带过来。第一版先只做占位——
        // 真正的来源是前一天最后那道 carry-over 的答案。
        val carry = store.answers()
            .firstOrNull { it.qid.endsWith("-carry") && it.day < day }
            ?.value?.trim()
        qs.add(
            Question(
                id = "$day-carry",
                day = day,
                persona = Persona.ADVOCATE,
                control = Control.TEXT,
                text = if (carry.isNullOrBlank())
                    "What did not get done today that you want to carry into tomorrow?"
                else
                    "Yesterday you said you wanted to carry this over: \"$carry\". Did it happen?",
                hint = "This becomes tomorrow's accountability thread.",
            )
        )
        return qs
    }

    /** 固定七维 + 问责 + 当天的规则/关联题。 */
    fun withDynamic(ctx: Context, day: String, store: Store): List<Question> =
        questionsFor(day, store) + Rules.generate(ctx, day)

    fun pending(day: String, store: Store): List<Question> {
        val done = store.answeredQids()
        return questionsFor(day, store).filter { it.id !in done }
    }

    init {
        // 保持引用，避免未使用告警时被误删
        Persona.entries.size
    }
}
