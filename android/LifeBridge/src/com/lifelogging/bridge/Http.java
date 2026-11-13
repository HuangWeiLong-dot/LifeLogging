package com.lifelogging.bridge;

import android.content.Context;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** 单线程顺序 POST 到中继；失败静默（Android 上重试队列反而更容易丢数据）。 */
public final class Http {
    private static final ExecutorService POOL = Executors.newSingleThreadExecutor();

    private Http() {}

    public static void post(final Context context, final String body) {
        final Context app = context.getApplicationContext();
        POOL.execute(() -> {
            HttpURLConnection c = null;
            try {
                c = (HttpURLConnection) new URL(Config.url(app)).openConnection();
                c.setRequestMethod("POST");
                c.setConnectTimeout(8000);
                c.setReadTimeout(10000);
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8");

                String key = Config.key(app);
                if (!key.isEmpty()) c.setRequestProperty("X-API-Key", key);
                String id = Config.cfId(app);
                String secret = Config.cfSecret(app);
                if (!id.isEmpty()) c.setRequestProperty("CF-Access-Client-Id", id);
                if (!secret.isEmpty()) c.setRequestProperty("CF-Access-Client-Secret", secret);

                try (OutputStream os = c.getOutputStream()) {
                    os.write(body.getBytes(StandardCharsets.UTF_8));
                }
                c.getResponseCode();  // 触发请求
            } catch (Exception ignored) {
                // 网络失败就不发了：中继那边没有队列，重试也只会把时间戳拖后
            } finally {
                if (c != null) c.disconnect();
            }
        });
    }
}
