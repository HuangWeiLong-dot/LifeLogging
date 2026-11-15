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
        val options: List<String> = emptyList(),
        val followUps: Map<String, List<String>> = emptyMap(),
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
        Spec("social", Persona.ROMANTICIST, Control.CHOICE,
            "Who did you spend time with today?",
            "Location only says you were nearby, not who with.",
            options = listOf("Alone", "Family", "Friends", "Colleague", "Partner", "Online only"),
            followUps = mapOf(
                "Family" to listOf("Ordinary day", "A long talk", "Some tension", "They needed me"),
                "Friends" to listOf("Just hanging out", "Deep talk", "Something happened"),
                "Colleague" to listOf("Work only", "Went beyond work"),
                "Partner" to listOf("Easy", "Strained", "Important conversation"),
                "Online only" to listOf("Group chat", "One person", "Mostly scrolling"),
            )),
        Spec("food", Persona.OBSERVER, Control.CHOICE,
            "How did eating go today?",
            "Meals do not produce events — only you know this.",
            options = listOf("Regular meals", "Skipped one", "Ate late", "Snacked instead", "Overate"),
            followUps = mapOf(
                "Skipped one" to listOf("Too busy", "Not hungry", "Forgot", "No food around"),
                "Ate late" to listOf("Work ran long", "Was out", "Not hungry earlier"),
                "Snacked instead" to listOf("Working", "Bored", "Low energy"),
                "Overate" to listOf("Was starving", "Social meal", "Stress"),
            )),
        Spec("rest", Persona.IDEALIST, Control.CHOICE,
            "Did you get a real break today?",
            "Rest and entertainment look the same in the data.",
            options = listOf("Stepped away >30 min", "Short breaks only", "Almost none"),
            followUps = mapOf(
                "Stepped away >30 min" to listOf("Walked outside", "Napped", "Errands", "Sat quietly"),
                "Short breaks only" to listOf("Phone between tasks", "Stretched", "Made food"),
                "Almost none" to listOf("Deadline", "Kept going out of habit", "Did not notice"),
            )),
        Spec("learn", Persona.IDEALIST, Control.CHOICE,
            "What kind of learning happened today?",
            "A window title never says what you understood.",
            options = listOf("Nothing new", "Work skill", "Technical", "Language", "Reading", "Hands-on practice"),
            followUps = mapOf(
                "Work skill" to listOf("Solved a bug", "Learned a tool", "Understood the domain"),
                "Technical" to listOf("Concept clicked", "Followed a tutorial", "Read source"),
                "Hands-on practice" to listOf("Repeatable now", "Still shaky", "Broke something"),
            )),
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
                    options = s.options,
                    followUps = s.followUps,
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
