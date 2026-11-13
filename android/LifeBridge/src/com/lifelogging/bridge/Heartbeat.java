package com.lifelogging.bridge;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.os.BatteryManager;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * 定时上报电量 + 网络。
 *
 * 通知监听只在「有事发生」时才回调（换歌、支付），状态类数据不能靠它。
 * 这里用 AlarmManager 的 inexact repeating 每 15 分钟发一次，
 * 插拔电源 / 网络切换时另外立即补发（见 HeartbeatReceiver / BridgeService）。
 *
 * 用 inexact 而不是精确闹钟：省电，且云端本来也不需要秒级准确。
 */
public final class Heartbeat {

    public static final String ACTION_HEARTBEAT = "com.lifelogging.bridge.HEARTBEAT";
    private static final long INTERVAL_MS = 15L * 60 * 1000;

    private Heartbeat() {}

    /** 幂等：重复调用只会更新同一个 PendingIntent。 */
    public static void schedule(Context context) {
        Context app = context.getApplicationContext();
        AlarmManager am = (AlarmManager) app.getSystemService(Context.ALARM_SERVICE);
        if (am == null) return;

        Intent intent = new Intent(app, HeartbeatReceiver.class).setAction(ACTION_HEARTBEAT);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pi = PendingIntent.getBroadcast(app, 0, intent, flags);

        am.setInexactRepeating(
            AlarmManager.RTC_WAKEUP,
            System.currentTimeMillis() + 5000,
            INTERVAL_MS,
            pi
        );
    }

    /** 立刻发一条 battery + network（一条请求带两个事件）。 */
    public static void sendNow(Context context) {
        Context app = context.getApplicationContext();
        JSONArray events = new JSONArray();
        long now = System.currentTimeMillis() / 1000;

        try {
            Intent state = app.registerReceiver(
                null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
            if (state != null) {
                int level = state.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                int scale = state.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
                int status = state.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
                if (level >= 0 && scale > 0) {
                    boolean charging =
                        status == BatteryManager.BATTERY_STATUS_CHARGING
                        || status == BatteryManager.BATTERY_STATUS_FULL;
                    JSONObject meta = new JSONObject();
                    meta.put("charging", charging);
                    JSONObject e = new JSONObject();
                    e.put("ts", now);
                    e.put("domain", "phone");
                    e.put("metric", "battery");
                    e.put("value_num", Math.round(level * 100.0 / scale));
                    e.put("meta", meta);
                    events.put(e);
                }
            }
        } catch (Exception ignored) {
        }

        try {
            ConnectivityManager cm =
                (ConnectivityManager) app.getSystemService(Context.CONNECTIVITY_SERVICE);
            NetworkInfo info = cm == null ? null : cm.getActiveNetworkInfo();
            String mode = "off";
            if (info != null && info.isConnected()) {
                if (info.getType() == ConnectivityManager.TYPE_WIFI) mode = "wifi";
                else if (info.getType() == ConnectivityManager.TYPE_MOBILE) mode = "mobile";
            }
            JSONObject e = new JSONObject();
            e.put("ts", now);
            e.put("domain", "phone");
            e.put("metric", "network");
            e.put("value_text", mode);
            events.put(e);
        } catch (Exception ignored) {
        }

        if (events.length() == 0) return;
        try {
            JSONObject body = new JSONObject();
            body.put("events", events);
            Http.post(app, body.toString());
        } catch (Exception ignored) {
        }
    }
}
