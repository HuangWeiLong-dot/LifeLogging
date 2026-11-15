package com.lifelogging.bridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 定时闹钟 + 插拔电源时触发；只做一件事：发一条 battery/network。 */
public class HeartbeatReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Heartbeat.sendNow(context.getApplicationContext());
    }
}
