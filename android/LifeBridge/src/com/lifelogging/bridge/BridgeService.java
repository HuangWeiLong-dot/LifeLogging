package com.lifelogging.bridge;

import android.app.Notification;
import android.content.ComponentName;
import android.graphics.Bitmap;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.text.TextUtils;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 一个通知监听服务干两件事：
 *
 *   1. 媒体会话（MediaSession）→ music 事件：歌名 / 歌手 / 专辑 / 时长
 *      比 Tasker 的 %MTRACK 多出专辑和时长，歌手也不会丢。
 *   2. 支付通知 → payment 事件：金额 + 来源 App
 *      支付宝一般有通知；微信支付大多没有，所以它只是补充，不是账本。
 *
 * 注意：payment 事件**不进公开归档**（publish_life_raw 已按 metric 过滤）。
 */
public class BridgeService extends NotificationListenerService {

    private static final String[] PAY_APPS = {
        "com.eg.android.AlipayGphone",   // 支付宝
        "com.tencent.mm",                // 微信
    };
    // 金额两种写法都认：¥12.00 / 12.00元
    private static final Pattern AMOUNT = Pattern.compile(
        "(?:¥|￥)\\s*([0-9]+(?:\\.[0-9]{1,2})?)|([0-9]+(?:\\.[0-9]{1,2})?)\\s*元");
    // 必须是支付类文案，否则「5元优惠券」也会被当成消费
    private static final String[] PAY_WORDS = {
        "支付", "付款", "消费", "扣款", "转账", "收款", "已付", "退款",
    };

    private MediaSessionManager mediaManager;
    private MediaController controller;
    private String lastTrack = "";

    private String lastPay = "";
    private long lastPayAt = 0L;

    // ── 通知监听连接 ──────────────────────────────────────────────

    @Override
    public void onListenerConnected() {
        try {
            mediaManager = (MediaSessionManager) getSystemService(MEDIA_SESSION_SERVICE);
            ComponentName me = new ComponentName(this, BridgeService.class);
            mediaManager.addOnActiveSessionsChangedListener(this::bindSessions, me);
            bindSessions(mediaManager.getActiveSessions(me));
        } catch (Exception ignored) {
            // 没有通知使用权时这里会抛 SecurityException
        }

        // 网络切换立即补发一条。CONNECTIVITY_CHANGE 在 Android 8+ 已不给
        // manifest 接收器，所以只能在服务里注册回调。
        try {
            ConnectivityManager cm =
                (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            if (cm != null) {
                cm.registerDefaultNetworkCallback(new ConnectivityManager.NetworkCallback() {
                    @Override
                    public void onAvailable(Network network) {
                        Heartbeat.sendNow(BridgeService.this);
                    }

                    @Override
                    public void onLost(Network network) {
                        Heartbeat.sendNow(BridgeService.this);
                    }
                });
            }
        } catch (Exception ignored) {
        }
    }

    // ── 媒体会话 ─────────────────────────────────────────────────

    private void bindSessions(List<MediaController> sessions) {
        if (sessions == null) return;
        String want = Config.mediaPackage(this);
        for (MediaController mc : sessions) {
            String pkg = mc.getPackageName();
            if (!want.isEmpty() && !want.equals(pkg)) continue;
            if (controller != null) {
                controller.unregisterCallback(metadataCallback);
            }
            controller = mc;
            mc.registerCallback(metadataCallback);
            syncTicker(mc);
            publishTrack(mc.getMetadata(), pkg);
            return;
        }
    }

    private final MediaController.Callback metadataCallback = new MediaController.Callback() {
        @Override
        public void onMetadataChanged(MediaMetadata metadata) {
            if (controller != null) {
                publishTrack(metadata, controller.getPackageName());
            }
        }

        @Override
        public void onPlaybackStateChanged(PlaybackState state) {
            syncTicker(controller);
        }
    };

    // ── 播放位置（歌词高亮用）────────────────────────────────────

    private final Handler tickHandler = new Handler(Looper.getMainLooper());
    private boolean ticking = false;

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (!ticking) return;
            sendPosition();
            tickHandler.postDelayed(this, 30_000);
        }
    };

    /** 只在播放中打点；暂停就停，避免位置一直往上漂。 */
    private void syncTicker(MediaController mc) {
        if (mc == null) {
            ticking = false;
            tickHandler.removeCallbacks(tick);
            return;
        }
        PlaybackState st = mc.getPlaybackState();
        boolean playing = st != null && st.getState() == PlaybackState.STATE_PLAYING;
        if (playing && !ticking) {
            ticking = true;
            tickHandler.post(tick);
        } else if (!playing && ticking) {
            ticking = false;
            tickHandler.removeCallbacks(tick);
        }
    }

    private void sendPosition() {
        MediaController mc = controller;
        if (mc == null) return;
        PlaybackState st = mc.getPlaybackState();
        MediaMetadata md = mc.getMetadata();
        if (st == null || md == null || st.getState() != PlaybackState.STATE_PLAYING) return;

        String title = md.getString(MediaMetadata.METADATA_KEY_TITLE);
        if (TextUtils.isEmpty(title)) return;

        long posMs = st.getPosition();
        long since = SystemClock.elapsedRealtime() - st.getLastPositionUpdateTime();
        if (since > 0) posMs += since;
        if (posMs < 0) posMs = 0;

        try {
            JSONObject e = new JSONObject();
            e.put("ts", System.currentTimeMillis() / 1000);
            e.put("domain", "phone");
            e.put("metric", "music_position");
            e.put("value_num", posMs / 1000);
            e.put("value_text", title);
            JSONArray events = new JSONArray();
            events.put(e);
            JSONObject body = new JSONObject();
            body.put("events", events);
            Http.post(this, body.toString());
        } catch (Exception ignored) {
        }
    }

    private void publishTrack(MediaMetadata md, String pkg) {
        if (md == null) return;

        String title = md.getString(MediaMetadata.METADATA_KEY_TITLE);
        String artist = md.getString(MediaMetadata.METADATA_KEY_ARTIST);
        String album = md.getString(MediaMetadata.METADATA_KEY_ALBUM);
        long durationMs = md.getLong(MediaMetadata.METADATA_KEY_DURATION);

        if (TextUtils.isEmpty(title)) return;

        // 同一首歌会反复回调（进度、缓冲），去重
        String signature = pkg + "|" + title + "|" + artist;
        if (signature.equals(lastTrack)) return;
        lastTrack = signature;

        try {
            JSONObject meta = new JSONObject();
            meta.put("app", pkg);
            meta.put("title", title);
            if (!TextUtils.isEmpty(artist)) meta.put("artist", artist);
            if (!TextUtils.isEmpty(album)) meta.put("album", album);
            if (durationMs > 0) meta.put("duration", durationMs / 1000);

            // 封面：缩到 96x96 JPEG 再 base64，一张约 2-4KB。
            // 站点通过 /now 直接渲染 data URI，不需要额外的图床。
            String cover = albumArt(md);
            if (cover != null) meta.put("cover", cover);

            JSONObject e = new JSONObject();
            e.put("ts", System.currentTimeMillis() / 1000);
            e.put("domain", "phone");
            e.put("metric", "music");
            // 兼容现有解析：没有歌手时就是纯歌名
            e.put("value_text", TextUtils.isEmpty(artist) ? title : artist + " - " + title);
            e.put("meta", meta);

            JSONObject body = new JSONObject();
            JSONArray events = new JSONArray();
            events.put(e);
            body.put("events", events);
            Http.post(this, body.toString());
        } catch (Exception ignored) {
        }
    }

    // ── 支付通知 ─────────────────────────────────────────────────

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        String pkg = sbn.getPackageName();
        if (!isPaymentApp(pkg)) return;

        Notification n = sbn.getNotification();
        if (n == null) return;

        Bundle ex = n.extras;
        String title = text(ex.getCharSequence(Notification.EXTRA_TITLE));
        String body = text(ex.getCharSequence(Notification.EXTRA_TEXT));
        String big = text(ex.getCharSequence(Notification.EXTRA_BIG_TEXT));
        String all = (title + " " + body + " " + big).trim();

        boolean payWord = false;
        for (String w : PAY_WORDS) {
            if (all.contains(w)) {
                payWord = true;
                break;
            }
        }
        if (!payWord) return;

        Matcher m = AMOUNT.matcher(all);
        if (!m.find()) return;

        String digits = m.group(1) != null ? m.group(1) : m.group(2);
        double amount;
        try {
            amount = Double.parseDouble(digits);
        } catch (Exception e) {
            return;
        }
        if (amount <= 0) return;

        // 同一条通知可能被重复投递；90 秒内同样的金额+文案只记一次
        String signature = pkg + "|" + amount + "|" + all;
        long now = System.currentTimeMillis();
        if (signature.equals(lastPay) && now - lastPayAt < 90_000L) return;
        lastPay = signature;
        lastPayAt = now;

        try {
            JSONObject meta = new JSONObject();
            meta.put("app", pkg);
            meta.put("title", title);
            meta.put("text", body);

            JSONObject e = new JSONObject();
            e.put("ts", now / 1000);
            e.put("domain", "phone");
            e.put("metric", "payment");
            e.put("value_num", amount);
            e.put("value_text", pkg.contains("Alipay") ? "Alipay" : "WeChat");
            e.put("meta", meta);

            JSONObject bodyJson = new JSONObject();
            JSONArray events = new JSONArray();
            events.put(e);
            bodyJson.put("events", events);
            Http.post(this, bodyJson.toString());
        } catch (Exception ignored) {
        }
    }

    /** 专辑封面 → 96x96 JPEG base64；没有就返回 null。 */
    private static String albumArt(MediaMetadata md) {
        Bitmap art = md.getBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART);
        if (art == null) art = md.getBitmap(MediaMetadata.METADATA_KEY_ART);
        if (art == null) return null;
        try {
            Bitmap scaled = Bitmap.createScaledBitmap(art, 96, 96, true);
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            scaled.compress(Bitmap.CompressFormat.JPEG, 60, bos);
            if (scaled != art) scaled.recycle();
            return Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP);
        } catch (Exception e) {
            return null;
        }
    }

    private static boolean isPaymentApp(String pkg) {
        for (String p : PAY_APPS) {
            if (p.equals(pkg)) return true;
        }
        return false;
    }

    private static String text(CharSequence cs) {
        return cs == null ? "" : cs.toString();
    }
}
