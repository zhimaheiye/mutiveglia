// Veglia · foreground poll service. Copyright (c) 2026 Evelyn & River — CC BY-NC-SA 4.0.
package dev.veglia.companion;

import android.app.ActivityManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.ComponentName;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.PowerManager;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

import org.json.JSONObject;

public class CompanionService extends Service {
    private static final String CHANNEL_ID = "veglia_companion";
    private static final int NOTIFICATION_ID = 1;
    private static volatile boolean running = false;

    private String serverUrl;
    private String token;
    // Package of the app your AI lives in. Empty = summoning is off, and the
    // server can shout all it likes without anything happening on this phone.
    private String homePackage;
    private Handler pollHandler;
    private HandlerThread pollThread;

    public static boolean isRunning() {
        return running;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        createNotificationChannel();
        Notification notification = buildNotification();
        startForeground(NOTIFICATION_ID, notification);

        if (intent != null) {
            serverUrl = intent.getStringExtra("server_url");
            token = intent.getStringExtra("token");
            homePackage = intent.getStringExtra("home_package");
        }
        // On STICKY restart the intent is null: heal from saved config instead of dying.
        if (serverUrl == null || token == null) {
            SharedPreferences prefs = getSharedPreferences("veglia_companion", MODE_PRIVATE);
            serverUrl = prefs.getString("server_url", null);
            token = prefs.getString("token", null);
        }
        if (homePackage == null) {
            homePackage = getSharedPreferences("veglia_companion", MODE_PRIVATE)
                    .getString("home_package", "");
        }
        if (serverUrl == null || token == null || serverUrl.isEmpty() || token.isEmpty()) {
            stopSelf();
            return START_NOT_STICKY;
        }

        if (!running) {
            running = true;
            startPolling();
        }

        return START_STICKY;
    }

    private void startPolling() {
        pollThread = new HandlerThread("PollThread");
        pollThread.start();
        pollHandler = new Handler(pollThread.getLooper());
        pollHandler.post(this::pollLoop);
    }

    private long lastHeartbeatTs = 0;
    private boolean lastReportedInteractive = false;

    private void pollLoop() {
        if (!running) return;
        try {
            checkAndSendHeartbeat();
            String cmd = pollServer();
            if ("peek".equals(cmd)) {
                ScreenshotService ss = ScreenshotService.getInstance();
                if (ss != null) {
                    ss.doScreenshot(serverUrl, token);
                }
            } else if ("summon".equals(cmd)) {
                summonHome();
            }
        } catch (Exception e) {
        }
        if (running) {
            pollHandler.postDelayed(this::pollLoop, 3000);
        }
    }

    private void checkAndSendHeartbeat() {
        if (serverUrl == null || token == null || serverUrl.isEmpty() || token.isEmpty()) {
            return;
        }
        try {
            ScreenshotService ss = ScreenshotService.getInstance();
            String currentPkg = ss != null ? ss.getCurrentPkg() : null;
            if (currentPkg == null || currentPkg.isEmpty()) {
                return;
            }

            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            boolean isInteractive = pm != null && pm.isInteractive();
            long now = System.currentTimeMillis();

            boolean screenTurnedOff = lastReportedInteractive && !isInteractive;
            boolean heartbeatDue = isInteractive && (now - lastHeartbeatTs >= 30000);

            if (screenTurnedOff || heartbeatDue) {
                sendHeartbeat(serverUrl, token, currentPkg, isInteractive);
                lastHeartbeatTs = now;
                lastReportedInteractive = isInteractive;
            }
        } catch (Exception ignored) {
        }
    }

    private void sendHeartbeat(String url, String tk, String pkg, boolean isInteractive) {
        new Thread(() -> {
            HttpURLConnection conn = null;
            try {
                JSONObject body = new JSONObject();
                body.put("app", pkg);
                body.put("event", "heartbeat");
                body.put("screenInteractive", isInteractive);

                conn = (HttpURLConnection) new URL(url + "/phone/activity").openConnection();
                conn.setRequestMethod("POST");
                conn.setRequestProperty("X-Auth-Token", tk);
                conn.setDoOutput(true);
                conn.setConnectTimeout(5000);
                conn.setReadTimeout(5000);
                conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");

                byte[] data = body.toString().getBytes("UTF-8");
                conn.setFixedLengthStreamingMode(data.length);
                OutputStream os = conn.getOutputStream();
                os.write(data);
                os.close();
                conn.getResponseCode();
            } catch (Exception ignored) {
            } finally {
                if (conn != null) conn.disconnect();
            }
        }).start();
    }

    /** Bring the app your AI lives in back to the front of the screen.
     *
     *  Try moving the existing task forward first: she may have been halfway
     *  through typing something, and a cold start would throw that away. Be
     *  aware that since API 21 getRunningTasks() only reports the caller's own
     *  tasks, so on any modern phone this loop finds nothing and the launcher
     *  intent below is what actually fires. It is kept because it costs one
     *  cheap call and does the kinder thing wherever it still works.
     */
    private void summonHome() {
        if (homePackage == null || homePackage.isEmpty()) return;
        try {
            ActivityManager am = (ActivityManager) getSystemService(ACTIVITY_SERVICE);
            if (am != null) {
                for (ActivityManager.RunningTaskInfo task : am.getRunningTasks(100)) {
                    ComponentName base = task.baseActivity;
                    ComponentName top = task.topActivity;
                    boolean isHome = (base != null && homePackage.equals(base.getPackageName()))
                            || (top != null && homePackage.equals(top.getPackageName()));
                    if (isHome) {
                        am.moveTaskToFront(task.id, ActivityManager.MOVE_TASK_WITH_HOME);
                        return;
                    }
                }
            }

            Intent launch = getPackageManager().getLaunchIntentForPackage(homePackage);
            if (launch != null) {
                launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                        | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
                        | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
                startActivity(launch);
            }
        } catch (Exception ignored) {
            // Some vendor ROMs (MIUI, ColorOS, Funtouch…) refuse background
            // activity starts unless "display pop-up windows while running in
            // the background" is granted by hand. Swallow it: the poll loop
            // must survive even when this one call is denied.
        }
    }

    private String pollServer() throws Exception {
        String urlStr = serverUrl + "/phone/poll";
        HttpURLConnection conn = (HttpURLConnection) new URL(urlStr).openConnection();
        conn.setRequestProperty("X-Auth-Token", token);
        conn.setConnectTimeout(10000);
        conn.setReadTimeout(15000);
        conn.setRequestMethod("GET");
        try {
            int code = conn.getResponseCode();
            if (code == 200) {
                java.io.InputStream is = conn.getInputStream();
                java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
                byte[] buf = new byte[1024];
                int n;
                while ((n = is.read(buf)) > 0) bos.write(buf, 0, n);
                String body = new String(bos.toByteArray(), "UTF-8");
                if (body.contains("\"peek\"")) return "peek";
                if (body.contains("\"summon\"")) return "summon";
            }
            return null;
        } finally {
            conn.disconnect();
        }
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "Veglia Service",
                    NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("keeps the connection alive");
            getSystemService(NotificationManager.class).createNotificationChannel(channel);
        }
    }

    private Notification buildNotification() {
        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder = new Notification.Builder(this, CHANNEL_ID);
        } else {
            builder = new Notification.Builder(this);
        }
        return builder
                .setContentTitle("Veglia")
                .setContentText("connected")
                .setSmallIcon(android.R.drawable.ic_menu_compass)
                .setOngoing(true)
                .build();
    }

    @Override
    public void onDestroy() {
        running = false;
        if (pollThread != null) {
            pollThread.quitSafely();
        }
        super.onDestroy();
    }
}
