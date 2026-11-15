package com.lifelogging.bridge;

import android.content.Context;
import android.content.SharedPreferences;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * 采集端顺手留一份**本地信号**，给问答端算缺口用。
 *
 * 为什么不在服务器上算：笔记本一天只醒几小时，服务器上虽然有手机事件，
 * 但没有「这一刻」的上下文。端上本来就已经收到屏幕 / 解锁 / 媒体的广播，
 * 存在本地几乎零成本，而且离线也能用。
 *
 * 只存**最近一次的时刻**和**当天的计数**，不存事件流 —— 那是中继的活。
 */
public final class SignalStore {

    private static final String PREFS = "lifebridge_signals";

    private SignalStore() {}

    private static SharedPreferences sp(Context c) {
        return c.getApplicationContext()
                .getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static String today() {
        return new SimpleDateFormat("yyyy-MM-dd", Locale.US).format(new Date());
    }

    /** 每次屏幕/解锁事件调用一次。 */
    public static void onScreen(Context c, String metric) {
        try {
            SharedPreferences p = sp(c);
            String day = today();
            int base = day.equals(p.getString("count_day", ""))
                    ? p.getInt(metric + "_count", 0) : 0;
            p.edit()
                    .putLong(metric + "_at", System.currentTimeMillis())
                    .putInt(metric + "_count", base + 1)
                    .putString("count_day", day)
                    .apply();
        } catch (Exception ignored) {
        }
    }

    /** 换歌时调用。 */
    public static void onMedia(Context c, String title, String artist) {
        try {
            sp(c).edit()
                    .putString("media_title", title == null ? "" : title)
                    .putString("media_artist", artist == null ? "" : artist)
                    .putLong("media_at", System.currentTimeMillis())
                    .apply();
        } catch (Exception ignored) {
        }
    }

    public static long lastAt(Context c, String metric) {
        return sp(c).getLong(metric + "_at", 0L);
    }

    public static int count(Context c, String metric) {
        SharedPreferences p = sp(c);
        if (!today().equals(p.getString("count_day", ""))) return 0;
        return p.getInt(metric + "_count", 0);
    }

    public static String mediaTitle(Context c) { return sp(c).getString("media_title", ""); }
    public static String mediaArtist(Context c) { return sp(c).getString("media_artist", ""); }
    public static long mediaAt(Context c) { return sp(c).getLong("media_at", 0L); }
}
