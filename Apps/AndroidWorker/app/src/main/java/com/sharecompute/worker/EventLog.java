package com.sharecompute.worker;

import android.app.ActivityManager;
import android.content.Context;
import android.os.Build;
import android.os.Process;
import android.os.SystemClock;
import org.json.JSONException;
import org.json.JSONObject;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicLong;

/**
 * What this phone did, one JSON object per line, for the operator to hand over after a failure (F42:
 * its cause stayed unsettled because nothing was captured). Line shape mirrors the coordinator's
 * scripts/run_log.py EventLog so the two can be read together: {@code at_ms} (milliseconds since this
 * process started, as the coordinator's is since its run started), {@code wall} (UTC, to line the
 * two up) and {@code ev}, then the event's own fields. Secrets never reach a line: see {@link #scrub}.
 *
 * Logging must never fail a run. {@link #event} only builds a string and offers it to a bounded queue;
 * a daemon thread does the file I/O, so a slow or full disk cannot stall a heartbeat or a tunnel. Every
 * failure is a dropped line, counted and reported in the export. Memory is capped at RING lines in
 * memory plus QUEUE waiting to be written. Disk is capped at two files of FILE_LIMIT bytes.
 */
final class EventLog {
    private EventLog() {}
    private static final int RING = 4000, QUEUE = 2000, FIELD = 200, LINE = 2048;
    private static final long FILE_LIMIT = 512L * 1024;
    private static final Object LOCK = new Object();
    private static final ArrayDeque<String> ring = new ArrayDeque<>();
    private static final LinkedBlockingQueue<Object> queue = new LinkedBlockingQueue<>(QUEUE);
    private static final AtomicLong logged = new AtomicLong(), droppedQueue = new AtomicLong(), droppedWrite = new AtomicLong(), overwritten = new AtomicLong();
    private static final Set<String> secrets = ConcurrentHashMap.newKeySet();
    private static final long started = SystemClock.elapsedRealtime();
    private static volatile File dir;
    private static boolean initialised;

    /** Idempotent. Records app start once per process. Safe to call from every entry point. */
    static void init(Context context) {
        try {
            synchronized (LOCK) { if (initialised) return; initialised = true; }
            Context app = context.getApplicationContext();
            File d = new File(app.getFilesDir(), "logs"); d.mkdirs(); dir = d;
            Thread writer = new Thread(EventLog::writeLoop, "event-log"); writer.setDaemon(true); writer.start();
            appStart(app);
        } catch (Throwable ignored) {}
    }

    /** One line. Keys and values alternate: {@code event("connect", "host", h, "port", 5000)}. Never throws. */
    static void event(String ev, Object... kv) {
        try {
            JSONObject o = new JSONObject();
            o.put("at_ms", SystemClock.elapsedRealtime() - started); o.put("wall", wall()); o.put("ev", ev);
            for (int i = 0; i + 1 < kv.length; i += 2) {
                try {
                    Object v = kv[i + 1]; if (v == null) continue;
                    if (v instanceof CharSequence) v = scrub(v.toString());
                    else if (v instanceof Double && (((Double) v).isNaN() || ((Double) v).isInfinite())) continue;
                    o.put(String.valueOf(kv[i]), v);
                } catch (Throwable ignored) {}
            }
            String line = o.toString();
            if (line.length() > LINE) line = new JSONObject().put("at_ms", o.get("at_ms")).put("wall", o.get("wall")).put("ev", ev).put("truncated", true).toString();
            synchronized (LOCK) {
                ring.addLast(line); logged.incrementAndGet();
                if (ring.size() > RING) { ring.removeFirst(); overwritten.incrementAndGet(); }
            }
            if (dir != null && !queue.offer(line)) droppedQueue.incrementAndGet();
        } catch (Throwable ignored) {}
    }

    private static String wall() {
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.ROOT); f.setTimeZone(TimeZone.getTimeZone("UTC"));
        return f.format(new Date());
    }

    /** Register a value that must never be written, such as the token or pin. Ignored when short enough to be noise. */
    static void protect(String secret) { if (secret != null && secret.length() >= 8) secrets.add(secret); }

    /**
     * Makes text safe to write: registered secrets, any 64-or-more hex run (a token or pin) and any
     * sc1. pairing blob become placeholders; the rest is cut to FIELD characters. A 40-character
     * commit hash is shorter than the hex threshold on purpose, so provenance survives.
     */
    static String scrub(String s) {
        if (s == null) return null;
        try {
            for (String secret : secrets) s = s.replace(secret, "<redacted>");
            s = s.replaceAll("[0-9a-fA-F]{64,}", "<redacted>").replaceAll("sc1\\.[A-Za-z0-9_=-]{8,}", "sc1.<redacted>");
        } catch (Throwable ignored) { return "<unprintable>"; }
        return s.length() > FIELD ? s.substring(0, FIELD) + "…" : s;
    }

    /**
     * A cause for a log line. org.json puts the whole input into some of its messages, and the input
     * can be the pairing payload, so only the "No value for key" form is kept from a JSONException.
     */
    static String cause(Throwable t) {
        if (t == null) return null;
        String m = t.getMessage();
        if (t instanceof JSONException) m = m != null && m.startsWith("No value for ") ? m : "malformed JSON";
        return t.getClass().getSimpleName() + (m == null || m.isEmpty() ? "" : ": " + scrub(m));
    }

    // ---- file side -------------------------------------------------------------------------------

    private static void writeLoop() {
        File cur = new File(dir, "events.jsonl"), prev = new File(dir, "events.1.jsonl");
        FileOutputStream out = null; long size = cur.length(); long retryAt = 0;
        while (true) {
            Object item;
            try { item = queue.take(); } catch (InterruptedException e) { return; }
            try {
                if (item instanceof CountDownLatch) { ((CountDownLatch) item).countDown(); continue; }
                if (out == null) {
                    if (SystemClock.elapsedRealtime() < retryAt) { droppedWrite.incrementAndGet(); continue; }
                    out = new FileOutputStream(cur, true); size = cur.length();
                }
                byte[] bytes = ((String) item + "\n").getBytes(StandardCharsets.UTF_8);
                out.write(bytes); size += bytes.length;
                if (size >= FILE_LIMIT) { // Keep one older file, so history is bounded at about 2 x FILE_LIMIT.
                    out.close(); out = null; prev.delete(); cur.renameTo(prev);
                }
            } catch (Throwable t) { // An unwritable log is a dropped line. Retry the open later, not on every line.
                droppedWrite.incrementAndGet();
                try { if (out != null) out.close(); } catch (Throwable ignored) {}
                out = null; retryAt = SystemClock.elapsedRealtime() + 10000;
            }
        }
    }

    /** Waits (at most 2 s) for lines already queued to reach the file. */
    private static void drain() {
        try { CountDownLatch barrier = new CountDownLatch(1); if (queue.offer(barrier)) barrier.await(2, TimeUnit.SECONDS); } catch (Throwable ignored) {}
    }

    /**
     * A snapshot for sharing: the older file, the current file, then one {@code export} line carrying
     * the loss counters. Falls back to the in-memory ring when the files cannot be read. Always a new
     * file under logs/export, so a share in progress is never overwritten. Throws only so the caller
     * can tell the operator; it never affects a running session.
     */
    static File export(Context context) throws IOException {
        event("export_requested");
        File d = dir; if (d == null) { init(context); d = dir; }
        if (d == null) throw new IOException("Log folder is unavailable");
        drain();
        File folder = new File(d, "export"); folder.mkdirs();
        File[] old = folder.listFiles(); if (old != null) for (File f : old) f.delete();
        SimpleDateFormat stamp = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.ROOT); stamp.setTimeZone(TimeZone.getTimeZone("UTC"));
        File target = new File(folder, "sharecompute-android-" + stamp.format(new Date()) + ".jsonl");
        long fileLines = 0; String source = "files";
        try (OutputStream out = new BufferedOutputStream(new FileOutputStream(target))) {
            for (String name : new String[]{"events.1.jsonl", "events.jsonl"}) {
                File f = new File(d, name); if (!f.isFile()) continue;
                try (InputStream in = new FileInputStream(f)) {
                    byte[] b = new byte[16384]; int n;
                    while ((n = in.read(b)) != -1) { out.write(b, 0, n); for (int i = 0; i < n; i++) if (b[i] == '\n') fileLines++; }
                } catch (IOException e) { source = "files-partial"; }
            }
            if (fileLines == 0) { // Nothing on disk: the ring is all there is.
                source = "ring";
                List<String> copy; synchronized (LOCK) { copy = new ArrayList<>(ring); }
                for (String line : copy) out.write((line + "\n").getBytes(StandardCharsets.UTF_8));
            }
            JSONObject o = new JSONObject();
            try {
                o.put("at_ms", SystemClock.elapsedRealtime() - started).put("wall", wall()).put("ev", "export").put("source", source)
                 .put("lines_logged", logged.get()).put("dropped_queue_full", droppedQueue.get()).put("dropped_write_failed", droppedWrite.get())
                 .put("ring_overwritten", overwritten.get()).put("app_build", WorkerSession.appBuild());
            } catch (JSONException ignored) {}
            out.write((o.toString() + "\n").getBytes(StandardCharsets.UTF_8));
        }
        return target;
    }

    // ---- app start ---------------------------------------------------------------------------------

    private static void appStart(Context app) {
        String nativeBuild = "unavailable", runtime = "unavailable";
        try { nativeBuild = WorkerSession.nativeBuild(); runtime = NativeWorker.revision(); } catch (Throwable t) { nativeBuild = runtime = "unavailable: " + t.getClass().getSimpleName(); }
        event("app_start", "app_build", WorkerSession.appBuild(), "native_build", nativeBuild, "runtime", runtime, "pid", Process.myPid(),
              "version", versionName(app), "android", Build.VERSION.RELEASE, "sdk", Build.VERSION.SDK_INT,
              "device", Build.MANUFACTURER + " " + Build.MODEL, "abi", Build.SUPPORTED_ABIS.length > 0 ? Build.SUPPORTED_ABIS[0] : "unknown");
        memory(app, "start");
        priorExits(app);
    }
    private static String versionName(Context app) {
        try { return app.getPackageManager().getPackageInfo(app.getPackageName(), 0).versionName; } catch (Throwable t) { return "unknown"; }
    }

    /** What ended the previous process, when Android will say (API 30+). Often the only trace of a silent kill. */
    private static void priorExits(Context app) {
        if (Build.VERSION.SDK_INT < 30) return;
        try {
            List<android.app.ApplicationExitInfo> exits = app.getSystemService(ActivityManager.class).getHistoricalProcessExitReasons(null, 0, 3);
            for (android.app.ApplicationExitInfo e : exits)
                event("prior_exit", "reason", exitReason(e.getReason()), "status", e.getStatus(), "importance", e.getImportance(),
                      "rss_mib", e.getRss() / 1024, "at", e.getTimestamp(), "description", e.getDescription());
        } catch (Throwable ignored) {}
    }
    private static String exitReason(int r) {
        switch (r) {
            case 1: return "EXIT_SELF"; case 2: return "SIGNALED"; case 3: return "LOW_MEMORY"; case 4: return "CRASH";
            case 5: return "CRASH_NATIVE"; case 6: return "ANR"; case 7: return "INITIALIZATION_FAILURE"; case 8: return "PERMISSION_CHANGE";
            case 9: return "EXCESSIVE_RESOURCE_USAGE"; case 10: return "USER_REQUESTED"; case 11: return "USER_STOPPED";
            case 12: return "DEPENDENCY_DIED"; case 13: return "OTHER"; case 14: return "FREEZER"; case 15: return "PERMISSION_REVOKED";
            default: return "UNKNOWN_" + r;
        }
    }

    // ---- memory ------------------------------------------------------------------------------------

    private static long lastAvailMib = -1; private static boolean lastLow; private static long lastMemEvent;

    /** Logs phone memory unconditionally. Returns available MiB, or -1 when unreadable. */
    static long memory(Context context, String when) {
        try {
            ActivityManager.MemoryInfo m = new ActivityManager.MemoryInfo();
            context.getSystemService(ActivityManager.class).getMemoryInfo(m);
            long avail = m.availMem >> 20;
            event("mem", "when", when, "avail_mib", avail, "total_mib", m.totalMem >> 20, "low", m.lowMemory, "threshold_mib", m.threshold >> 20, "rss_mib", rssMib());
            lastAvailMib = avail; lastLow = m.lowMemory; lastMemEvent = SystemClock.elapsedRealtime();
            return avail;
        } catch (Throwable t) { return -1; }
    }

    /**
     * Called every couple of seconds while a session runs, but writes only when memory moved
     * materially: 256 MiB since the last memory figure written, or the low-memory flag flipped. Events are at
     * least 4 s apart, so a phone swinging around the threshold still produces a line every few seconds, not
     * one per call. Returns current available MiB (or -1) so the caller can put it in its own line.
     */
    static long memoryWatch(Context context) {
        try {
            ActivityManager.MemoryInfo m = new ActivityManager.MemoryInfo();
            context.getSystemService(ActivityManager.class).getMemoryInfo(m);
            long avail = m.availMem >> 20; long now = SystemClock.elapsedRealtime();
            boolean flip = m.lowMemory != lastLow, moved = lastAvailMib < 0 || Math.abs(avail - lastAvailMib) >= 256;
            if (flip || (moved && now - lastMemEvent >= 4000)) memory(context, flip ? "low-memory-flag" : "changed");
            return avail;
        } catch (Throwable t) { return -1; }
    }

    /** Resident set of this process (native model buffers included), from /proc/self/status; -1 if unreadable. */
    static long rssMib() {
        try (BufferedReader r = new BufferedReader(new FileReader("/proc/self/status"))) {
            String l; while ((l = r.readLine()) != null) if (l.startsWith("VmRSS:")) return Long.parseLong(l.replaceAll("[^0-9]", "")) / 1024;
        } catch (Throwable ignored) {}
        return -1;
    }

    static String trimLevel(int level) {
        switch (level) {
            case 5: return "RUNNING_MODERATE"; case 10: return "RUNNING_LOW"; case 15: return "RUNNING_CRITICAL";
            case 20: return "UI_HIDDEN"; case 40: return "BACKGROUND"; case 60: return "MODERATE"; case 80: return "COMPLETE";
            default: return "LEVEL_" + level;
        }
    }
    static String thermalName(int status) {
        switch (status) {
            case 0: return "NONE"; case 1: return "LIGHT"; case 2: return "MODERATE"; case 3: return "SEVERE";
            case 4: return "CRITICAL"; case 5: return "EMERGENCY"; case 6: return "SHUTDOWN"; default: return "UNKNOWN_" + status;
        }
    }
}
