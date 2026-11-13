package com.lifelogging.bridge;

import android.content.Context;
import android.content.SharedPreferences;

/** 设置存在 SharedPreferences 里，Entry Activity 负责编辑。 */
public final class Config {
    private static final String PREFS = "lifebridge";

    private Config() {}

    public static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    public static String url(Context c) {
        return prefs(c).getString("url", "https://relay.liveinpassion.me/ingest/phone");
    }

    public static String key(Context c) {
        return prefs(c).getString("key", "");
    }

    public static String cfId(Context c) {
        return prefs(c).getString("cf_id", "");
    }

    public static String cfSecret(Context c) {
        return prefs(c).getString("cf_secret", "");
    }

    /** 只监听这个包名的媒体会话；留空 = 任意播放器。 */
    public static String mediaPackage(Context c) {
        return prefs(c).getString("media_pkg", "com.kugou.android.lite");
    }
}
