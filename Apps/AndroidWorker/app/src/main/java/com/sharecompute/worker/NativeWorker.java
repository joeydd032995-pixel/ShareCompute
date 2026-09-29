package com.sharecompute.worker;
final class NativeWorker {
    static { System.loadLibrary("sc-android"); }
    static native int run(int port, long budget);
    static native long[] stats();
    static native String revision();
}
