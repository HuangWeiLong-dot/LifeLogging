package com.lifelogging.lifepassion.net

import android.content.Context
import com.lifelogging.bridge.Config
import com.lifelogging.bridge.Http
import com.lifelogging.lifepassion.data.Answer
import com.lifelogging.lifepassion.data.Control
import com.lifelogging.lifepassion.data.Emotion
import com.lifelogging.lifepassion.data.Persona
import com.lifelogging.lifepassion.data.Question
import com.lifelogging.lifepassion.data.Store
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * 答案 → 中继。
 *
 * 复用采集模块的 `Http.post`：地址、X-API-Key、CF Access 三个头都在
 * 同一个设置页里，不另起一套配置。
 *
 * **答案自带题目原文。** 飞书下线后中继上已经没有题目文件，`GET /qa`
 * 无法再按题号对齐问题；把 `question` 放进 meta 之后，笔记本侧只靠
 * 事件表就能还原「问了什么 / 答了什么」。
 *
 * 端点沿用 `/ingest/phone`（`domain=question`、`metric=answer`），
 * 和原来飞书那条链路同形，`collect_phone_events.py` 会原样透传。
 */
object Relay {

    private const val ORIGIN = "lifepassion"

    fun send(ctx: Context, store: Store, a: Answer, q: Question?) {
        Http.post(ctx, body(a, q)) { code, _ ->
            if (code in 200..299) store.markSynced(a.qid)
        }
    }

    private val pool = Executors.newSingleThreadExecutor()

    /** 服务端生成的「带锚点的问题」。拿不到就是空 —— App 会退回本地那批。 */
    fun fetchQuestions(ctx: Context, day: String, onDone: (List<Question>) -> Unit) {
        pool.execute {
            val out = ArrayList<Question>()
            var c: HttpURLConnection? = null
            try {
                val full = Config.url(ctx)                 // .../ingest/phone
                val marker = full.indexOf("/ingest/")
                val base = if (marker > 0) full.substring(0, marker) else full
                c = (URL("$base/engage/questions?day=$day").openConnection() as HttpURLConnection)
                c.requestMethod = "GET"
                c.connectTimeout = 8000
                c.readTimeout = 10000
                val key = Config.key(ctx)
                if (key.isNotEmpty()) c.setRequestProperty("X-API-Key", key)
                val id = Config.cfId(ctx)
                val secret = Config.cfSecret(ctx)
                if (id.isNotEmpty()) c.setRequestProperty("CF-Access-Client-Id", id)
                if (secret.isNotEmpty()) c.setRequestProperty("CF-Access-Client-Secret", secret)
                if (c.responseCode in 200..299) {
                    val body = c.inputStream.bufferedReader().use { it.readText() }
                    val arr = JSONObject(body).optJSONArray("questions") ?: JSONArray()
                    for (i in 0 until arr.length()) {
                        val o = arr.getJSONObject(i)
                        val opts = ArrayList<String>()
                        o.optJSONArray("options")?.let { a2 ->
                            for (j in 0 until a2.length()) opts.add(a2.getString(j))
                        }
                        // 服务器给了选项就用选项，没给才退回文字
                        val ctrl = if (opts.isNotEmpty()) Control.CHOICE else runCatching {
                            Control.valueOf(o.optString("control", "TEXT"))
                        }.getOrDefault(Control.TEXT)
                        out.add(
                            Question(
                                id = o.getString("id"),
                                day = o.optString("day", day),
                                persona = runCatching {
                                    Persona.valueOf(o.optString("persona", "OBSERVER"))
                                }.getOrDefault(Persona.OBSERVER),
                                control = ctrl,
                                text = o.optString("text", ""),
                                hint = o.optString("hint", ""),
                                layer = o.optString("layer", "assoc"),
                                options = opts,
                            )
                        )
                    }
                }
            } catch (_: Exception) {
                // 服务器没生成过 / 网络不通 —— 静默退回本地题
            } finally {
                c?.disconnect()
            }
            onDone(out)
        }
    }

    /** 重置今日问答：把服务器上那天已经收到的答案也删掉。
     *
     *  不删的话，下一次 collect_phone_events 会把旧答案重新拉进事件表，
     *  看起来像「重置没生效」。
     */
    fun resetDay(ctx: Context, day: String) {
        pool.execute {
            var c: HttpURLConnection? = null
            try {
                val full = Config.url(ctx)
                val marker = full.indexOf("/ingest/")
                val base = if (marker > 0) full.substring(0, marker) else full
                c = (URL("$base/engage/reset").openConnection() as HttpURLConnection)
                c.requestMethod = "POST"
                c.connectTimeout = 8000
                c.readTimeout = 10000
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                val key = Config.key(ctx)
                if (key.isNotEmpty()) c.setRequestProperty("X-API-Key", key)
                val id = Config.cfId(ctx)
                val secret = Config.cfSecret(ctx)
                if (id.isNotEmpty()) c.setRequestProperty("CF-Access-Client-Id", id)
                if (secret.isNotEmpty()) c.setRequestProperty("CF-Access-Client-Secret", secret)
                c.outputStream.use { it.write("{\"day\":\"$day\"}".toByteArray()) }
                c.responseCode
            } catch (_: Exception) {
                // 服务器清不掉不影响本地重置
            } finally {
                c?.disconnect()
            }
        }
    }

    /** 启动时补发所有没成功的答案。 */
    fun syncAll(ctx: Context, store: Store, questions: List<Question>) {
        val byId = questions.associateBy { it.id }
        store.unsynced().forEach { a -> send(ctx, store, a, byId[a.qid]) }
    }

    private fun body(a: Answer, q: Question?): String {
        val meta = JSONObject().apply {
            put("qid", a.qid)
            put("day", a.day)
            put("persona", a.persona.name)
            // 题目原文：中继上没有题目文件之后，这是唯一的还原来源
            if (q != null) {
                put("question", q.text)
                put("control", q.control.name)
                put("layer", q.layer)
            }
            if (a.emotions.isNotEmpty()) {
                put("emotions", JSONArray(a.emotions))
                put("valence", meanEmotion(a, 0))
                put("arousal", meanEmotion(a, 1))
            }
            a.intensity?.let { put("intensity", it) }
            if (a.emotionIntensity.isNotEmpty()) {
                put("emotionIntensity", JSONObject().apply {
                    a.emotionIntensity.forEach { (k, n) -> put(k, n) }
                })
            }
            put("late", a.late)
            put("missing", a.missing)
            if (a.note.isNotBlank()) put("note", a.note)
        }
        return JSONObject().apply {
            put("ts", a.answeredAt / 1000L)
            put("domain", "question")
            put("metric", "answer")
            // 事件表里 `<题号>|<答案>|<备注>` 的老约定保持不动
            put("value_text", "${a.qid}|${a.value}|${a.note}".take(2000))
            put("source", ORIGIN)
            put("meta", meta)
        }.toString()
    }

    private fun meanEmotion(a: Answer, which: Int): Double {
        val vals = a.emotions.mapNotNull { name ->
            val e = Emotion.entries.firstOrNull { it.label == name } ?: return@mapNotNull null
            if (which == 0) e.valence else e.arousal
        }
        return if (vals.isEmpty()) 0.0 else vals.average()
    }
}
