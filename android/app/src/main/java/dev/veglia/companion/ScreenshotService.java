// Veglia · screenshot via AccessibilityService. Copyright (c) 2026 Evelyn & River — CC BY-NC-SA 4.0.
package dev.veglia.companion;

import android.accessibilityservice.AccessibilityService;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;

import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.Executor;
import java.util.concurrent.Executors;

import org.json.JSONObject;

public class ScreenshotService extends AccessibilityService {
    private static volatile ScreenshotService instance;
    private final Executor executor = Executors.newSingleThreadExecutor();

    public static ScreenshotService getInstance() {
        return instance;
    }

    // Watchdog: the accessibility service is kept alive by the system (it comes
    // back after a restart). We borrow its lifespan to guard the poll service.
    // If the system killed CompanionService, revive it — unless the user stopped it.
    private Handler watchdog;
    private final Runnable watchdogTick = new Runnable() {
        @Override
        public void run() {
            try {
                SharedPreferences prefs = getSharedPreferences("veglia_companion", MODE_PRIVATE);
                String url = prefs.getString("server_url", "");
                String tk = prefs.getString("token", "");
                boolean userStopped = prefs.getBoolean("user_stopped", false);
                if (!CompanionService.isRunning() && !userStopped && !url.isEmpty() && !tk.isEmpty()) {
                    Intent i = new Intent(ScreenshotService.this, CompanionService.class);
                    i.putExtra("server_url", url);
                    i.putExtra("token", tk);
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        startForegroundService(i);
                    } else {
                        startService(i);
                    }
                }
            } catch (Exception e) {
            }
            if (watchdog != null) {
                watchdog.postDelayed(this, 60000);
            }
        }
    };

    @Override
    public void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
        refreshImePkg();
        watchdog = new Handler(Looper.getMainLooper());
        watchdog.postDelayed(watchdogTick, 15000);
    }

    // -- foreground-app reporting ---------------------------------------------
    //
    // This is the half of Veglia that costs nothing: no picture, no bandwidth,
    // no "she is being watched" feeling — just a one-line answer to "what is she
    // doing right now". Earlier versions of this project farmed the job out to a
    // third-party automation app; that meant ads and a fragile hand-built recipe.
    // It lives here now, in the same accessibility service that already takes the
    // screenshots, because the permission it needs is one we have already asked for.

    /** Last package we reported, so an in-app Activity hop isn't a "switch". */
    private String lastPkg = null;

    public String getCurrentPkg() {
        return lastPkg;
    }

    /** Package name of the active keyboard, read once when the service connects. */
    private String imePkg;

    private void refreshImePkg() {
        try {
            String ime = android.provider.Settings.Secure.getString(
                    getContentResolver(), android.provider.Settings.Secure.DEFAULT_INPUT_METHOD);
            imePkg = (ime != null && ime.contains("/")) ? ime.substring(0, ime.indexOf('/')) : null;
        } catch (Exception e) {
            imePkg = null;
        }
    }

    /**
     * Keyboards and the notification shade draw *on top of* the current app —
     * they are not a change of what she is looking at. Without this check,
     * tapping a text box reads as "she left the app and opened a keyboard".
     */
    private boolean isOverlayPkg(String pkg) {
        if ("com.android.systemui".equals(pkg)) return true;   // shade / quick settings
        return imePkg != null && imePkg.equals(pkg);           // keyboard
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event == null
                || event.getEventType() != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
                || event.getPackageName() == null) {
            return;
        }
        String pkg = event.getPackageName().toString();
        if (isOverlayPkg(pkg)) return;
        if (pkg.equals(lastPkg)) return;

        lastPkg = pkg;
        SharedPreferences prefs = getSharedPreferences("veglia_companion", MODE_PRIVATE);
        reportAppSwitch(
                prefs.getString("server_url", ""),
                prefs.getString("token", ""),
                pkg);
    }

    /**
     * POST the package name that just came to the front.
     *
     * Runs on the service's own single-thread executor so a slow network never
     * blocks a system callback. Failures are dropped silently — the next switch
     * reports again, and a missed line matters far less than a stalled phone.
     */
    private void reportAppSwitch(String serverUrl, String token, String pkg) {
        if (serverUrl == null || serverUrl.isEmpty()
                || token == null || token.isEmpty()
                || pkg == null || pkg.isEmpty()) {
            return;
        }
        executor.execute(() -> {
            HttpURLConnection conn = null;
            try {
                JSONObject body = new JSONObject();
                body.put("app", pkg);
                body.put("event", "switch");

                conn = (HttpURLConnection) new URL(
                        serverUrl + "/phone/activity").openConnection();
                conn.setRequestMethod("POST");
                conn.setRequestProperty("X-Auth-Token", token);
                conn.setDoOutput(true);
                conn.setConnectTimeout(8000);
                conn.setReadTimeout(8000);
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
        });
    }

    @Override
    public void onInterrupt() {
    }

    @Override
    public void onDestroy() {
        instance = null;
        if (watchdog != null) {
            watchdog.removeCallbacksAndMessages(null);
            watchdog = null;
        }
        super.onDestroy();
    }

    public void doScreenshot(String serverUrl, String token) {
        if (Build.VERSION.SDK_INT < 30) return;

        takeScreenshot(Display.DEFAULT_DISPLAY, executor, new TakeScreenshotCallback() {
            @Override
            public void onSuccess(ScreenshotResult result) {
                try {
                    Bitmap hardwareBitmap = Bitmap.wrapHardwareBuffer(
                            result.getHardwareBuffer(), result.getColorSpace());
                    if (hardwareBitmap == null) return;

                    Bitmap bitmap = hardwareBitmap.copy(Bitmap.Config.ARGB_8888, false);
                    hardwareBitmap.recycle();
                    result.getHardwareBuffer().close();

                    ByteArrayOutputStream out = new ByteArrayOutputStream();
                    bitmap.compress(Bitmap.CompressFormat.JPEG, 92, out);
                    bitmap.recycle();

                    byte[] data = out.toByteArray();
                    if (data.length > 100) {
                        uploadScreenshot(data, serverUrl, token);
                    }
                } catch (Exception e) {
                    e.printStackTrace();
                }
            }

            @Override
            public void onFailure(int errorCode) {
            }
        });
    }

    private void uploadScreenshot(byte[] data, String serverUrl, String token) {
        try {
            String urlStr = serverUrl + "/phone/screenshot";
            HttpURLConnection conn = (HttpURLConnection) new URL(urlStr).openConnection();
            conn.setRequestMethod("POST");
            conn.setRequestProperty("X-Auth-Token", token);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "image/jpeg");
            conn.setRequestProperty("Content-Length", String.valueOf(data.length));
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(30000);

            OutputStream os = conn.getOutputStream();
            os.write(data);
            os.flush();
            os.close();

            conn.getResponseCode();
            conn.disconnect();
        } catch (Exception e) {
            e.printStackTrace();
        }
    }
}
