package com.sharecompute.worker;

import android.content.Context;
import java.io.File;

/**
 * Model weights the laptop sent before, kept so a repeat run with the same model skips most of the
 * Wi-Fi upload. The native worker checks every file against its hash before using it, so a damaged
 * file is re-sent, never computed with. It lives in the app cache, which Android may clear when
 * storage runs low; that only costs a re-upload.
 */
final class WeightCache {
    private WeightCache() {}
    // The native worker owns the decisions and exposes only running totals, so hits, stores and rejections are
    // seen here as changes in those totals. The totals span the process, so a session's baseline is taken when it begins.
    private static long hit, stored, rejected, reportedAt;
    private static long pendingHit, pendingStored;
    /** A session is starting: note what is on disk and take the counters' current values as the baseline. */
    static synchronized void begin(Context context, long[] totals) {
        File[] files = dir(context).listFiles(); int count = 0;
        if (files != null) for (File f : files) if (f.isFile()) count++;
        EventLog.event("cache_state", "files", count, "mib", bytes(context) >> 20);
        if (totals != null && totals.length >= 6) { hit = totals[3]; stored = totals[4]; rejected = totals[5]; }
        pendingHit = pendingStored = 0; reportedAt = android.os.SystemClock.elapsedRealtime();
    }
    /**
     * Called a few times a second-ish with the native totals. A rejection is logged the moment it is seen: it
     * means a stored file failed its hash check, which is rare and the thing worth knowing. Hits and stores
     * arrive as a stream during an upload, so they are summed and written at most every 10 s, and only when they changed.
     */
    static synchronized void observe(long[] totals) {
        if (totals == null || totals.length < 6) return;
        if (totals[5] > rejected) { EventLog.event("cache_rejected", "new", totals[5] - rejected, "total", totals[5]); rejected = totals[5]; }
        if (totals[3] > hit) { pendingHit += totals[3] - hit; hit = totals[3]; }
        if (totals[4] > stored) { pendingStored += totals[4] - stored; stored = totals[4]; }
        long now = android.os.SystemClock.elapsedRealtime();
        if ((pendingHit > 0 || pendingStored > 0) && now - reportedAt >= 10000) {
            EventLog.event("cache", "hit_bytes", pendingHit, "stored_bytes", pendingStored, "hit_total", hit, "stored_total", stored);
            pendingHit = pendingStored = 0; reportedAt = now;
        }
    }
    static File dir(Context context) { return new File(context.getCacheDir(), "rpc-weights"); }
    static long bytes(Context context) {
        File[] files = dir(context).listFiles(); long total = 0;
        if (files != null) for (File f : files) if (f.isFile()) total += f.length();
        return total;
    }
    /** The session ended: write whatever hits and stores have not been written yet. */
    static synchronized void end(long[] totals) {
        observe(totals);
        if (pendingHit > 0 || pendingStored > 0) { EventLog.event("cache", "hit_bytes", pendingHit, "stored_bytes", pendingStored, "hit_total", hit, "stored_total", stored, "final", true); pendingHit = pendingStored = 0; }
    }
    /** Safe while connected: a file deleted mid-run reads as a miss and is sent again. */
    static long clear(Context context) {
        File[] files = dir(context).listFiles(); long freed = 0;
        int count = 0;
        if (files != null) for (File f : files) { long size = f.length(); if (f.isFile() && f.delete()) { freed += size; count++; } }
        EventLog.event("cache_cleared", "files", count, "freed_mib", freed >> 20);
        return freed;
    }
}
