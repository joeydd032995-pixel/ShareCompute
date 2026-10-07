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
            if (budget != requested) throw new Exception("Restart the app to change its memory budget");
            return port;
        }
        try (ServerSocket probe = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))) { port = probe.getLocalPort(); }
        budget = requested;
        final int chosen = port;
        new Thread(() -> onExit.exited(NativeWorker.run(chosen, requested, cacheDir)), "native-worker").start();
        return port;
    }
}
