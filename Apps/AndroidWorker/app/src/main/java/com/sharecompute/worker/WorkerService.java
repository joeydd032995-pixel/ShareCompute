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
    // Telemetry choice: the session sends stats to the laptop every 500 ms, but the log gets a "native" line every
    // 10 s (an 8-minute run is about 50 lines, not 960). Memory is looked at every 2 s so a sudden drop is caught
    // close to when it happens, and written only when it moves materially (see EventLog.memoryWatch).
    private static final long TICK_MS = 2000;
    private static final int NATIVE_EVERY = 5;
    // All static state below is touched only on the main thread.
    private static final Handler main = new Handler(Looper.getMainLooper());
    private static StatusListener listener;
    private static String status = "Ready to pair";
    private static WorkerService instance;

    private WorkerSession session;
    private PowerManager.WakeLock cpu;
    private WifiManager.WifiLock wifi;
    private String shown;
    private int ticks;
    private boolean ticking;
    private PowerManager.OnThermalStatusChangedListener thermal;
    private final Runnable tick = () -> sample();

    static void observe(StatusListener l) { listener = l; if (l != null) l.status(status); }
    private static void publish(String message) { status = message; if (listener != null) listener.status(message); }

    static void start(Context context, String pairing) {
        EventLog.event("start_requested");
        context.startForegroundService(new Intent(context, WorkerService.class).setAction(ACTION_START).putExtra(EXTRA_PAIRING, pairing));
    }
    static void stop(Context context) {
        EventLog.event("stop_requested");
        // A START may still be queued before the service exists; intents are delivered in order,
        // so a STOP sent after it always wins and a fast Disconnect cannot be lost.
        context.startService(new Intent(context, WorkerService.class).setAction(ACTION_STOP));
    }

    @Override public void onCreate() {
        super.onCreate(); instance = this;
        EventLog.init(this); EventLog.event("service_create");
        watchThermal();
        getSystemService(NotificationManager.class).createNotificationChannel(
            new NotificationChannel(CHANNEL, "Compute worker", NotificationManager.IMPORTANCE_LOW));
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        EventLog.init(this);
        EventLog.event("service_command", "action", intent == null ? "null-intent" : intent.getAction(), "start_id", startId, "flags", flags);
        if (intent == null || ACTION_STOP.equals(intent.getAction())) { shutdown("Disconnected"); return START_NOT_STICKY; }
        // startForegroundService obliges startForeground promptly on every path, including failures.
        goForeground("Connecting to your laptop…");
        endSession();
        try {
            JSONObject pair = WorkerSession.parse(intent.getStringExtra(EXTRA_PAIRING));
            EventLog.memory(this, "session-start"); previous = null; WeightCache.begin(this, nativeStats());
            int port = NativeHost.ensure(pair.getLong("budget_mib") * 1048576, WeightCache.dir(this).getPath(),
                code -> main.post(() -> { if (instance != null) instance.shutdown("Native worker exited (" + code + "). Restart the app."); }));
            acquireLocks();
            final WorkerSession[] holder = new WorkerSession[1];
            holder[0] = new WorkerSession(pair, port, new WorkerSession.Listener() {
                public void update(String message) { main.post(() -> { if (session == holder[0]) { publish(message); notifyIfChanged(message); } }); }
                public void ended(String message) { main.post(() -> { if (session == holder[0]) shutdown(message); }); }
            });
            session = holder[0]; publish("Connecting…"); session.start(); startTicking();
        } catch (Exception e) { EventLog.event("start_failed", "cause", EventLog.cause(e)); shutdown(e.getMessage()); }
        return START_NOT_STICKY;
    }

    private void goForeground(String text) {
        Notification n = notification(text); shown = text;
        if (Build.VERSION.SDK_INT >= 34) startForeground(NOTIFICATION, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(NOTIFICATION, n);
        EventLog.event("foreground", "sdk", Build.VERSION.SDK_INT);
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
        EventLog.event("wakelock_acquired", "timeout_h", LOCK_TIMEOUT_MS / 3600000);
        // HIGH_PERF keeps Wi-Fi out of power save with the screen off, but Android 14 deprecated it and
        // LOW_LATENCY needs the screen on, so newer phones get no Wi-Fi lock: the link stays up, possibly slower.
        if (Build.VERSION.SDK_INT >= 34) { EventLog.event("wifilock_skipped", "why", "inert from API 34"); return; }
        if (wifi == null) {
            wifi = ((WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE)).createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "ShareCompute:worker");
            wifi.setReferenceCounted(false);
        }
        wifi.acquire(); EventLog.event("wifilock_acquired");
    }
    private void releaseLocks() {
        // "held" false for the CPU lock here would mean its 4 h timeout already released it.
        boolean cpuHeld = cpu != null && cpu.isHeld(), wifiHeld = wifi != null && wifi.isHeld();
        if (cpuHeld) cpu.release();
        if (wifiHeld) wifi.release();
        if (cpu != null || wifi != null) EventLog.event("locks_released", "cpu_was_held", cpuHeld, "wifi_was_held", wifiHeld);
    }
    private void endSession() {
        stopTicking();
        if (session != null) { logNative("end"); WeightCache.end(nativeStats()); EventLog.memory(this, "session-end"); session.stop(); session = null; }
    }

    private void shutdown(String message) {
        EventLog.event("shutdown", "reason", message == null ? "Disconnected" : message);
        endSession(); releaseLocks();
        stopForeground(STOP_FOREGROUND_REMOVE); stopSelf();
        publish(message == null ? "Disconnected" : message);
    }

    @Override public void onDestroy() {
        EventLog.event("service_destroy");
        endSession(); releaseLocks(); instance = null;
        if (thermal != null && Build.VERSION.SDK_INT >= 29) { try { getSystemService(PowerManager.class).removeThermalStatusListener(thermal); } catch (Throwable ignored) {} }
        super.onDestroy();
    }
    @Override public void onTrimMemory(int level) { super.onTrimMemory(level); EventLog.event("trim_memory", "who", "service", "level", EventLog.trimLevel(level)); EventLog.memory(this, "trim"); }
    @Override public void onLowMemory() { super.onLowMemory(); EventLog.event("low_memory", "who", "service"); EventLog.memory(this, "low-memory"); }
    // The user swiped the app away. The foreground service normally survives it; the line shows whether it did.
    @Override public void onTaskRemoved(Intent rootIntent) { super.onTaskRemoved(rootIntent); EventLog.event("task_removed", "session", session != null); }

    // ---- logging: nothing below may affect the session; every path is guarded ----
    private static long[] nativeStats() { try { return NativeWorker.stats(); } catch (Throwable t) { return null; } }

    private void watchThermal() {
        if (Build.VERSION.SDK_INT < 29) return;
        try {
            PowerManager pm = getSystemService(PowerManager.class);
            EventLog.event("thermal", "status", EventLog.thermalName(pm.getCurrentThermalStatus()), "when", "start");
            thermal = status -> EventLog.event("thermal", "status", EventLog.thermalName(status), "when", "changed");
            pm.addThermalStatusListener(getMainExecutor(), thermal);
        } catch (Throwable t) { EventLog.event("thermal_unavailable", "cause", EventLog.cause(t)); }
    }
    private void startTicking() { if (!ticking) { ticking = true; ticks = 0; main.postDelayed(tick, TICK_MS); } }
    private void stopTicking() { ticking = false; main.removeCallbacks(tick); }
    private void sample() {
        if (!ticking) return;
        try {
            long[] stats = nativeStats(); WeightCache.observe(stats);
            EventLog.memoryWatch(this);
            if (++ticks % NATIVE_EVERY == 0) logNative("sample");
        } catch (Throwable ignored) {}
        if (ticking) main.postDelayed(tick, TICK_MS);
    }
    private long[] previous;
    private void logNative(String when) {
        try {
            long[] s = nativeStats(); if (s == null || s.length < 6) return;
            long[] p = previous != null ? previous : new long[6];
            EventLog.event("native", "when", when, "allocated_bytes", s[0], "peak_bytes", s[1], "graph_calls", s[2], "cache_hit_bytes", s[3],
                "cache_stored_bytes", s[4], "cache_rejected", s[5], "new_graphs", s[2] - p[2], "rss_mib", EventLog.rssMib());
            previous = s;
        } catch (Throwable ignored) {}
    }
    @Override public IBinder onBind(Intent intent) { return null; }
}
