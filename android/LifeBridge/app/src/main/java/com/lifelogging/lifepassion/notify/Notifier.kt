package com.lifelogging.lifepassion.notify

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import com.lifelogging.lifepassion.MainActivity
import com.lifelogging.lifepassion.R

/**
 * 常驻/临时通知。
 *
 * 第一版只做「还有几题没答 + 点开进 App」。真正的通知栏快速答
 * （1–5 直接在通知上点）需要 RemoteInput / 自定义 View，
 * 放到下一版，避免一次改太多。
 */
object Notifier {

    private const val CHANNEL = "lifepassion_questions"
    private const val ID = 4101

    fun refresh(ctx: Context, pending: Int) {
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(
                CHANNEL, "Questions", NotificationManager.IMPORTANCE_LOW
            )
            ch.description = "How many questions still need an answer"
            nm.createNotificationChannel(ch)
        }

        if (pending <= 0) {
            nm.cancel(ID)
            return
        }

        val open = PendingIntent.getActivity(
            ctx, 0,
            Intent(ctx, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val n = NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(R.drawable.ic_lifepassion_fg)
            .setContentTitle("$pending questions need you")
            .setContentText("Open LifePassion to answer")
            .setOngoing(false)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .build()

        nm.notify(ID, n)
    }
}
