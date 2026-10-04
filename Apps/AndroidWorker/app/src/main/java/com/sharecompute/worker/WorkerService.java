package com.sharecompute.worker;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.drawable.Icon;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import org.json.JSONObject;

/**
 * Owns the pairing session so a test survives the screen locking or the app leaving the foreground.
 * Without it Android may freeze or kill a backgrounded worker mid-generation, and every joined
 * worker's disconnect fails the whole test on the laptop.
 */
public final class WorkerService extends Service {
    static final String ACTION_START = "com.sharecompute.worker.START";
    static final String ACTION_STOP = "com.sharecompute.worker.STOP";
    static final String EXTRA_PAIRING = "pairing";
    interface StatusListener { void status(String message); }

    private static final String CHANNEL = "worker";
    private static final int NOTIFICATION = 1;
    // A bounded test never needs this long; the timeout only stops a forgotten lock draining the battery.
    private static final long LOCK_TIMEOUT_MS = 4L * 60 * 60 * 1000;
    // All static state below is touched only on the main thread.
    private static final Handler main = new Handler(Looper.getMainLooper());
    private static StatusListener listener;
    private static String status = "Ready to pair";
    private static WorkerService instance;

    private WorkerSession session;
    private PowerManager.WakeLock cpu;
    private WifiManager.WifiLock wifi;
    private String shown;

    static void observe(StatusListener l) { listener = l; if (l != null) l.status(status); }
    private static void publish(String message) { status = message; if (listener != null) listener.status(message); }

    static void start(Context context, String pairing) {
        context.startForegroundService(new Intent(context, WorkerService.class).setAction(ACTION_START).putExtra(EXTRA_PAIRING, pairing));
    }
    static void stop(Context context) {
        // A START may still be queued before the service exists; intents are delivered in order,
        // so a STOP sent after it always wins and a fast Disconnect cannot be lost.
        context.startService(new Intent(context, WorkerService.class).setAction(ACTION_STOP));
    }

    @Override public void onCreate() {
        super.onCreate(); instance = this;
        getSystemService(NotificationManager.class).createNotificationChannel(
            new NotificationChannel(CHANNEL, "Compute worker", NotificationManager.IMPORTANCE_LOW));
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || ACTION_STOP.equals(intent.getAction())) { shutdown("Disconnected"); return START_NOT_STICKY; }
        // startForegroundService obliges startForeground promptly on every path, including failures.
        goForeground("Connecting to your laptop…");
        endSession();
        try {
            JSONObject pair = WorkerSession.parse(intent.getStringExtra(EXTRA_PAIRING));
            int port = NativeHost.ensure(pair.getLong("budget_mib") * 1048576,
                code -> main.post(() -> { if (instance != null) instance.shutdown("Native worker exited (" + code + "). Restart the app."); }));
            acquireLocks();
            final WorkerSession[] holder = new WorkerSession[1];
            holder[0] = new WorkerSession(pair, port, new WorkerSession.Listener() {
                public void update(String message) { main.post(() -> { if (session == holder[0]) { publish(message); notifyIfChanged(message); } }); }
                public void ended(String message) { main.post(() -> { if (session == holder[0]) shutdown(message); }); }
            });
            session = holder[0]; publish("Connecting…"); session.start();
        } catch (Exception e) { shutdown(e.getMessage()); }
        return START_NOT_STICKY;
    }

    private void goForeground(String text) {
        Notification n = notification(text); shown = text;
        if (Build.VERSION.SDK_INT >= 34) startForeground(NOTIFICATION, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(NOTIFICATION, n);
    }
    private void notifyIfChanged(String message) {
        // Telemetry arrives twice a second; only state changes are worth a notification update.
        String text = message.startsWith("Connected") ? "Connected — computing for your laptop" : message;
        if (!text.equals(shown)) { shown = text; getSystemService(NotificationManager.class).notify(NOTIFICATION, notification(text)); }
    }
    private Notification notification(String text) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        PendingIntent disconnect = PendingIntent.getService(this, 1, new Intent(this, WorkerService.class).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_sync).setContentTitle("ShareCompute is computing")
            .setContentText(text).setOngoing(true).setContentIntent(open)
            .addAction(new Notification.Action.Builder(Icon.createWithResource(this, android.R.drawable.ic_menu_close_clear_cancel), "Disconnect", disconnect).build())
            .build();
    }

    @SuppressWarnings("deprecation")
    private void acquireLocks() {
        if (cpu == null) {
            cpu = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ShareCompute:worker");
            cpu.setReferenceCounted(false);
        }
        cpu.acquire(LOCK_TIMEOUT_MS);
        // HIGH_PERF keeps Wi-Fi out of power save with the screen off, but Android 14 deprecated it and
        // LOW_LATENCY needs the screen on, so newer phones get no Wi-Fi lock: the link stays up, possibly slower.
        if (Build.VERSION.SDK_INT >= 34) return;
        if (wifi == null) {
            wifi = ((WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE)).createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "ShareCompute:worker");
            wifi.setReferenceCounted(false);
        }
        wifi.acquire();
    }
    private void releaseLocks() {
        if (cpu != null && cpu.isHeld()) cpu.release();
        if (wifi != null && wifi.isHeld()) wifi.release();
    }
    private void endSession() { if (session != null) { session.stop(); session = null; } }

    private void shutdown(String message) {
        endSession(); releaseLocks();
        stopForeground(STOP_FOREGROUND_REMOVE); stopSelf();
        publish(message == null ? "Disconnected" : message);
    }

    @Override public void onDestroy() { endSession(); releaseLocks(); instance = null; super.onDestroy(); }
    @Override public IBinder onBind(Intent intent) { return null; }
}
