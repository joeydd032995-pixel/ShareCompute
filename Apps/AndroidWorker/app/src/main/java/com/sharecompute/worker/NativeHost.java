package com.sharecompute.worker;

import java.net.InetAddress;
import java.net.ServerSocket;

/** The native RPC listener lives for the whole process: sc_worker_run never returns, and its budget is fixed at first start. */
final class NativeHost {
    interface Exit { void exited(int code); }
    private static int port;
    private static long budget;
    private NativeHost() {}

    static synchronized int ensure(long requested, String cacheDir, Exit onExit) throws Exception {
        if (port != 0) {
            if (budget != requested) {
                EventLog.event("native_budget_conflict", "running_mib", budget >> 20, "requested_mib", requested >> 20);
                throw new Exception("Restart the app to change its memory budget");
            }
            EventLog.event("native_listener_reused", "port", port, "budget_mib", budget >> 20);
            return port;
        }
        try (ServerSocket probe = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))) { port = probe.getLocalPort(); }
        budget = requested;
        final int chosen = port;
        // Threads is the literal passed to sc_worker_run in worker-jni.cpp; keep the two in step.
        EventLog.event("native_listener_start", "port", chosen, "budget_mib", requested >> 20, "threads", 2, "cache", cacheDir != null && !cacheDir.isEmpty() ? "on" : "off");
        new Thread(() -> { int code = NativeWorker.run(chosen, requested, cacheDir); EventLog.event("native_exit", "code", code); onExit.exited(code); }, "native-worker").start();
        return port;
    }
}
