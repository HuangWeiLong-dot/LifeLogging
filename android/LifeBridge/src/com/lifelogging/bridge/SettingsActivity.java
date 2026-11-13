package com.lifelogging.bridge;

import android.app.Activity;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

/** 唯一的界面：填中继地址 / 密钥，检查通知使用权。 */
public class SettingsActivity extends Activity {

    private EditText url;
    private EditText key;
    private EditText cfId;
    private EditText cfSecret;
    private EditText mediaPkg;
    private TextView status;
    private TextView last;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);

        url = findViewById(R.id.url);
        key = findViewById(R.id.key);
        cfId = findViewById(R.id.cfId);
        cfSecret = findViewById(R.id.cfSecret);
        mediaPkg = findViewById(R.id.mediaPkg);
        status = findViewById(R.id.status);
        last = findViewById(R.id.last);

        url.setText(Config.url(this));
        key.setText(Config.key(this));
        cfId.setText(Config.cfId(this));
        cfSecret.setText(Config.cfSecret(this));
        mediaPkg.setText(Config.mediaPackage(this));

        findViewById(R.id.save).setOnClickListener(v -> {
            Config.prefs(this).edit()
                .putString("url", url.getText().toString().trim())
                .putString("key", key.getText().toString().trim())
                .putString("cf_id", cfId.getText().toString().trim())
                .putString("cf_secret", cfSecret.getText().toString().trim())
                .putString("media_pkg", mediaPkg.getText().toString().trim())
                .apply();
            last.setText("已保存。触发一次换歌就能在中继里看到事件。");
        });

        findViewById(R.id.openAccess).setOnClickListener(v -> {
            try {
                startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS));
            } catch (Exception ignored) {
            }
        });

        findViewById(R.id.test).setOnClickListener(v -> sendTest());

        // 打开一次就把心跳排上（幂等），并立刻补一条电量/网络
        Heartbeat.schedule(this);
        Heartbeat.sendNow(this);
        startBridgeService();

        // Android 13+ 发通知要运行时授权；不给也不影响采集，只是看不到常驻通知
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(
                new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
    }

    private void startBridgeService() {
        Intent intent = new Intent(this, BridgeForegroundService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        boolean granted = notificationAccessGranted();
        status.setText(granted
            ? "通知使用权：已开启 ✓"
            : "通知使用权：未开启 —— 点下面的按钮，找到「Life Bridge」打开");
        status.setTextColor(granted ? 0xFF2E7D32 : 0xFFC62828);
    }

    private boolean notificationAccessGranted() {
        String flat = Settings.Secure.getString(
            getContentResolver(), "enabled_notification_listeners");
        return flat != null && flat.contains(getPackageName());
    }

    /**
     * 发一条测试事件，确认地址 / 密钥 / Tunnel 头都对。
     * 必须等 HTTP 结果再显示 —— 只弹「已发送」的话，401/403 也看不出来。
     */
    private void sendTest() {
        last.setText("发送中…");
        try {
            JSONObject meta = new JSONObject();
            meta.put("app", "test");
            JSONObject e = new JSONObject();
            e.put("ts", System.currentTimeMillis() / 1000);
            e.put("domain", "phone");
            // 专用 metric：不会被当成音乐或别的数据
            e.put("metric", "bridge_test");
            e.put("value_text", "Life Bridge - Connectivity Test");
            e.put("meta", meta);
            JSONArray events = new JSONArray();
            events.put(e);
            JSONObject body = new JSONObject();
            body.put("events", events);

            Http.post(this, body.toString(), (code, error) -> runOnUiThread(() -> {
                if (code == 200) {
                    last.setText("✓ 已送达（HTTP 200）。中继当天会多一条 bridge_test。");
                } else if (code > 0) {
                    last.setText("✗ 中继返回 HTTP " + code
                        + (code == 401 ? "：X-API-Key 不对" : "")
                        + (code == 403 ? "：Cloudflare Access 的 Token 不对或没加进 Policy" : ""));
                } else {
                    last.setText("✗ 连不上：" + (error == null ? "未知错误" : error)
                        + "\n检查地址、手机网络，以及是否走了 Tunnel 但没填 Token。");
                }
            }));
        } catch (Exception ex) {
            last.setText("✗ 构造请求失败：" + ex.getMessage());
        }
    }
}
