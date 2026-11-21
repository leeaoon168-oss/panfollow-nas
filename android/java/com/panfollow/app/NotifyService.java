package com.panfollow.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * 后台常驻：连 NAS 的 /api/events 长连接，收到事件就发系统通知。
 * 这样即使 App 退到后台、甚至从最近任务划掉，也能收到下载完成/新集提醒。
 */
public class NotifyService extends Service {

    private static final String TAG = "kdocs-notify";
    private static final String CH_ID = "kdocs_events";
    private static final String CH_SERVICE = "kdocs_service";
    private static final int FG_ID = 1001;

    private volatile boolean running = true;
    private Thread worker;
    private long lastId = 0;
    private int seq = 2000;

    @Override
    public void onCreate() {
        super.onCreate();
        createChannels();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(FG_ID, buildRunning());
        if (worker == null || !worker.isAlive()) {
            running = true;
            worker = new Thread(this::loop, "kdocs-sse");
            worker.setDaemon(true);
            worker.start();
        }
        return START_STICKY;
    }

    private void createChannels() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        NotificationChannel ev = new NotificationChannel(
                CH_ID, "PanFollow 消息", NotificationManager.IMPORTANCE_HIGH);
        ev.setDescription("下载完成、新集、失败提醒");
        ev.enableVibration(true);
        nm.createNotificationChannel(ev);

        NotificationChannel sv = new NotificationChannel(
                CH_SERVICE, "后台监听", NotificationManager.IMPORTANCE_MIN);
        sv.setDescription("保持后台连接用的常驻通知，可忽略");
        sv.setShowBadge(false);
        nm.createNotificationChannel(sv);
    }

    private Notification buildRunning() {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CH_SERVICE)
                : new Notification.Builder(this);
        b.setContentTitle("PanFollow 在后台运行")
                .setContentText("有新集或下载完成会通知你")
                .setSmallIcon(android.R.drawable.stat_notify_chat)
                .setOngoing(true);
        return b.build();
    }

    private void loop() {
        while (running) {
            HttpURLConnection conn = null;
            try {
                String base = baseUrl();
                if (base.isEmpty()) {
                    Thread.sleep(5000);
                    continue;
                }
                URL u = new URL(base + "/api/events?since=" + lastId);
                conn = (HttpURLConnection) u.openConnection();
                conn.setRequestProperty("Accept", "text/event-stream");
                conn.setRequestProperty("Cache-Control", "no-cache");
                conn.setConnectTimeout(10000);
                conn.setReadTimeout(0);
                conn.connect();
                BufferedReader r = new BufferedReader(
                        new InputStreamReader(conn.getInputStream(), "UTF-8"));
                String line;
                while (running && (line = r.readLine()) != null) {
                    if (!line.startsWith("data:")) continue;
                    String js = line.substring(5).trim();
                    if (js.isEmpty()) continue;
                    try {
                        JSONObject o = new JSONObject(js);
                        long id = o.optLong("id", 0);
                        if (id > lastId) lastId = id;
                        long ts = o.optLong("ts", 0);
                        if (System.currentTimeMillis() / 1000 - ts > 30) continue; // 老事件不打扰
                        push(o.optString("title"), o.optString("body"), o.optString("kind"));
                    } catch (Exception ignored) {
                    }
                }
            } catch (Exception e) {
                Log.w(TAG, "sse: " + e);
            } finally {
                if (conn != null) conn.disconnect();
            }
            try {
                Thread.sleep(5000);
            } catch (InterruptedException ignored) {
            }
        }
    }

    private String baseUrl() {
        SharedPreferences sp = getSharedPreferences("cfg", MODE_PRIVATE);
        String v = sp.getString("server_url", "http://192.168.1.100:8099");
        while (v.endsWith("/")) v = v.substring(0, v.length() - 1);
        return v;
    }

    private void push(String title, String body, String kind) {
        if (title == null || title.isEmpty()) return;
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            Intent i = new Intent(this, MainActivity.class);
            i.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
            PendingIntent pi = PendingIntent.getActivity(this, 0, i,
                    Build.VERSION.SDK_INT >= 23
                            ? PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
                            : PendingIntent.FLAG_UPDATE_CURRENT);

            Notification.Builder b = Build.VERSION.SDK_INT >= 26
                    ? new Notification.Builder(this, CH_ID)
                    : new Notification.Builder(this);
            b.setContentTitle(title)
                    .setContentText(body == null ? "" : body)
                    .setStyle(new Notification.BigTextStyle().bigText(body == null ? "" : body))
                    .setSmallIcon(android.R.drawable.stat_notify_chat)
                    .setAutoCancel(true)
                    .setContentIntent(pi);
            if (!"err".equals(kind)) {
                b.setDefaults(Notification.DEFAULT_ALL);
            }
            nm.notify(seq++, b.build());
        } catch (Exception e) {
            Log.w(TAG, "push: " + e);
        }
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
