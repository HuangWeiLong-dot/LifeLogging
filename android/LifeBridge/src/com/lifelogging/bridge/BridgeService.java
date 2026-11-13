package com.lifelogging.bridge;

import android.app.Notification;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.Uri;
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
import java.io.InputStream;
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
    private boolean screenReceiverOn = false;

    // 屏幕亮灭 / 解锁。Tasker 的对应 Profile 可以停掉了。
    private final BroadcastReceiver screenReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            String action = intent.getAction();
            if (Intent.ACTION_SCREEN_ON.equals(action)) {
                sendScreen("screen_on");
            } else if (Intent.ACTION_SCREEN_OFF.equals(action)) {
                sendScreen("screen_off");
            } else if (Intent.ACTION_USER_PRESENT.equals(action)) {
                sendScreen("unlock");
            }
        }
    };

    private void sendScreen(String metric) {
        try {
            JSONObject e = new JSONObject();
            e.put("ts", System.currentTimeMillis() / 1000);
            e.put("domain", "phone");
            e.put("source", "lifebridge");
            e.put("metric", metric);
            JSONArray events = new JSONArray();
            events.put(e);
            JSONObject body = new JSONObject();
            body.put("events", events);
            Http.post(this, body.toString());
        } catch (Exception ignored) {
        }
    }

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

        // 屏幕亮灭 / 解锁：动态注册，不受 Android 8+ 隐式广播限制
        if (!screenReceiverOn) {
            try {
                IntentFilter filter = new IntentFilter();
                filter.addAction(Intent.ACTION_SCREEN_ON);
                filter.addAction(Intent.ACTION_SCREEN_OFF);
                filter.addAction(Intent.ACTION_USER_PRESENT);
                registerReceiver(screenReceiver, filter);
                screenReceiverOn = true;
            } catch (Exception ignored) {
            }
        }
    }

    @Override
    public void onDestroy() {
        if (screenReceiverOn) {
            try {
                unregisterReceiver(screenReceiver);
            } catch (Exception ignored) {
            }
            screenReceiverOn = false;
        }
        super.onDestroy();
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
            tickHandler.postDelayed(this, 10_000);
        }
    };

    /**
     * 播放中每 10 秒打点；**播放/暂停/拖动时立即补一条**。
     * 位置事件里带 meta.playing，/now 不再靠自己猜「是不是还在放」。
     */
    private void syncTicker(MediaController mc) {
        if (mc == null) {
            ticking = false;
            tickHandler.removeCallbacks(tick);
            return;
        }
        PlaybackState st = mc.getPlaybackState();
        boolean playing = st != null && st.getState() == PlaybackState.STATE_PLAYING;
        sendPosition();  // 状态一变就推，不用等下一个 10 秒
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
        if (st == null || md == null) return;

        String title = md.getString(MediaMetadata.METADATA_KEY_TITLE);
        if (TextUtils.isEmpty(title)) return;

        boolean playing = st.getState() == PlaybackState.STATE_PLAYING;
        long posMs = st.getPosition();
        if (playing) {
            long since = SystemClock.elapsedRealtime() - st.getLastPositionUpdateTime();
            if (since > 0) posMs += since;
        }
        if (posMs < 0) posMs = 0;

        try {
            JSONObject meta = new JSONObject();
            meta.put("playing", playing);
            JSONObject e = new JSONObject();
            e.put("ts", System.currentTimeMillis() / 1000);
            e.put("domain", "phone");
            e.put("source", "lifebridge");
            e.put("metric", "music_position");
            e.put("value_num", posMs / 1000);
            e.put("value_text", title);
            e.put("meta", meta);
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

        // 封面：缩到 96x96 JPEG 再 base64，一张约 2-4KB。
        // 站点通过 /now 直接渲染 data URI，不需要额外的图床。
        String cover = albumArt(md);

        // 同一首歌会反复回调（进度、缓冲），去重。
        // ⚠️ 必须把「这次有没有封面」算进去：播放器通常先给歌名、封面晚几百毫秒
        //    才到，按歌名去重会把第二次（带封面的）回调直接吞掉，
        //    表现就是换专辑后封面一直是空的。
        String signature = pkg + "|" + title + "|" + artist + "|" + (cover != null);
        if (signature.equals(lastTrack)) return;
        lastTrack = signature;

        try {
            JSONObject meta = new JSONObject();
            meta.put("app", pkg);
            meta.put("title", title);
            if (!TextUtils.isEmpty(artist)) meta.put("artist", artist);
            if (!TextUtils.isEmpty(album)) meta.put("album", album);
            if (durationMs > 0) meta.put("duration", durationMs / 1000);
            if (cover != null) meta.put("cover", cover);

            JSONObject e = new JSONObject();
            e.put("ts", System.currentTimeMillis() / 1000);
            e.put("domain", "phone");
            e.put("source", "lifebridge");
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
            e.put("source", "lifebridge");
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
    private String albumArt(MediaMetadata md) {
        Bitmap art = md.getBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART);
        if (art == null) art = md.getBitmap(MediaMetadata.METADATA_KEY_ART);
        if (art == null) {
            // 不少播放器（包括酷狗概念版的部分曲目）只给 URI，不给 Bitmap
            String uri = md.getString(MediaMetadata.METADATA_KEY_ALBUM_ART_URI);
            if (uri == null) uri = md.getString(MediaMetadata.METADATA_KEY_ART_URI);
            if (uri != null) art = bitmapFromUri(uri);
        }
        if (art == null) return null;
        try {
            // 160 而不是 96：站点按 132px 显示，96 是放大后糊；q75 在
            // 5-8KB 之间，/now 的 64KB 上限和 JSONL 体积都还宽裕。
            Bitmap scaled = Bitmap.createScaledBitmap(art, 160, 160, true);
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            scaled.compress(Bitmap.CompressFormat.JPEG, 75, bos);
            if (scaled != art) scaled.recycle();
            return Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP);
        } catch (Exception e) {
            return null;
        }
    }

    private Bitmap bitmapFromUri(String uri) {
        try (InputStream in = getContentResolver().openInputStream(Uri.parse(uri))) {
            return BitmapFactory.decodeStream(in);
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
