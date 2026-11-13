package com.lifelogging.bridge;

import android.app.Activity;
import android.content.Intent;
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

    /** 发一条测试事件，确认地址 / 密钥 / Tunnel 头都对。 */
    private void sendTest() {
        try {
            JSONObject meta = new JSONObject();
            meta.put("app", "test");
            JSONObject e = new JSONObject();
            e.put("ts", System.currentTimeMillis() / 1000);
            e.put("domain", "phone");
            e.put("metric", "music");
            e.put("value_text", "Life Bridge - Connectivity Test");
            e.put("meta", meta);
            JSONArray events = new JSONArray();
            events.put(e);
            JSONObject body = new JSONObject();
            body.put("events", events);
            Http.post(this, body.toString());
            last.setText("已发送测试事件。去中继确认，或看笔记本的 collect_phone_events 输出。");
        } catch (Exception ex) {
            last.setText("发送失败：" + ex.getMessage());
        }
    }
}
