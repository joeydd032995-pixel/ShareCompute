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
    static File dir(Context context) { return new File(context.getCacheDir(), "rpc-weights"); }
    static long bytes(Context context) {
        File[] files = dir(context).listFiles(); long total = 0;
        if (files != null) for (File f : files) if (f.isFile()) total += f.length();
        return total;
    }
    /** Safe while connected: a file deleted mid-run reads as a miss and is sent again. */
    static long clear(Context context) {
        File[] files = dir(context).listFiles(); long freed = 0;
        if (files != null) for (File f : files) { long size = f.length(); if (f.isFile() && f.delete()) freed += size; }
        return freed;
    }
}
