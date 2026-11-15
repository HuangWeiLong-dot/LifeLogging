package com.lifelogging.bridge;

import android.content.Context;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** 单线程顺序 POST 到中继。 */
public final class Http {
    private static final ExecutorService POOL = Executors.newSingleThreadExecutor();

    private Http() {}

    /** 结果回调；code 为 HTTP 状态码，失败时 -1 并带 error。 */
    public interface Result {
        void onResult(int code, String error);
    }

    public static void post(Context context, String body) {
        post(context, body, null);
    }

    public static void post(final Context context, final String body, final Result cb) {
        final Context app = context.getApplicationContext();
        POOL.execute(() -> {
            HttpURLConnection c = null;
            int code = -1;
            String error = null;
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
                code = c.getResponseCode();
            } catch (Exception e) {
                error = e.getClass().getSimpleName() + ": " + e.getMessage();
            } finally {
                if (c != null) c.disconnect();
            }
            if (cb != null) cb.onResult(code, error);
        });
    }
}
