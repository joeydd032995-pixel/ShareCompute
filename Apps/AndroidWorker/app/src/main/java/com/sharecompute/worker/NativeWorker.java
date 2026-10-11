package com.sharecompute.worker;
final class NativeWorker {
    static { System.loadLibrary("sc-android"); }
    static native int run(int port, long budget, String cacheDir);
    static native long[] stats();
    static native String revision();
    /** The ShareCompute commit the native runtime was built from, or "unknown". */
    static native String buildCommit();
}
