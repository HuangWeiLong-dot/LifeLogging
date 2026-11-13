package com.lifelogging.bridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 重启 / 覆盖安装后闹钟会丢，重新排一次。 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Context app = context.getApplicationContext();
        Heartbeat.schedule(app);
        Heartbeat.sendNow(app);
    }
}
