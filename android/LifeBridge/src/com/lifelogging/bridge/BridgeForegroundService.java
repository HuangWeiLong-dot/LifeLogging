package com.lifelogging.bridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

/**
 * 常驻通知：一是让用户看得见它在跑，二是 foreground service 的优先级
 * 比普通后台进程高，通知监听服务不容易被系统回收。
 */
public class BridgeForegroundService extends Service {

    private static final String CHANNEL = "lifebridge_status";
    private static final int NOTIFICATION_ID = 42;

    @Override
    public void onCreate() {
        super.onCreate();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                CHANNEL, "LifeBridge 运行状态", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("采集服务是否在运行");
            channel.setShowBadge(false);
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) nm.createNotificationChannel(channel);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification notification = build();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
        return START_STICKY;
    }

    private Notification build() {
        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
            ? new Notification.Builder(this, CHANNEL)
            : new Notification.Builder(this);
        return builder
            .setContentTitle("LifeBridge 正在采集")
            .setContentText("音乐 · 屏幕 · 电量 · 网络 · 通知")
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .setShowWhen(false)
            .build();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
