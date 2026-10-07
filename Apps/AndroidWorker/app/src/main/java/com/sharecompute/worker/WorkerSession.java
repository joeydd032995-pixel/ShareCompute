package com.sharecompute.worker;

import org.json.JSONObject;
import javax.net.ssl.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.security.cert.X509Certificate;
import java.util.*;
import java.util.concurrent.*;

/** Outbound pinned TLS only. The native RPC listener never leaves loopback. */
final class WorkerSession {
    interface Listener { void update(String message); void ended(String message); }
    private final JSONObject pair;
    private final Listener listener;
    private final int port;
    private final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
    private final ExecutorService pool = Executors.newCachedThreadPool();
    private final ScheduledExecutorService clock = Executors.newSingleThreadScheduledExecutor();
    private final Semaphore channels = new Semaphore(4);
    private volatile boolean closed;
    WorkerSession(JSONObject pair, int port, Listener listener) { this.pair=pair; this.port=port; this.listener=listener; }
    static JSONObject parse(String text) throws Exception {
        text=text.trim();
        if (text.startsWith("sc1.")) text=new String(android.util.Base64.decode(text.substring(4), android.util.Base64.URL_SAFE|android.util.Base64.NO_WRAP), StandardCharsets.UTF_8);
        if (text.length()>8192) throw new IOException("Pairing code is too long");
        JSONObject p=new JSONObject(text);
        if (!p.getString("node").equals("android") || !p.getString("runtime").equals(NativeWorker.revision()) ||
            !p.getString("pin").matches("[0-9a-f]{64}") || !p.getString("token").matches("[0-9a-f]{64}") ||
            p.getInt("budget_mib")<64 || p.getInt("budget_mib")>4096 || p.getInt("port")<1 || p.getInt("port")>65535 ||
            p.getString("host").length()>253 || p.getString("host").isEmpty()) throw new IOException("Wrong phone or incompatible pairing code");
        return p;
    }
    private void track(Socket s) throws IOException { sockets.add(s); if(closed) { sockets.remove(s); s.close(); throw new IOException("Stopped"); } }
    private SSLSocket connect() throws Exception {
        final String pin=pair.getString("pin");
        X509TrustManager trust=new X509TrustManager() {
            public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            public void checkClientTrusted(X509Certificate[] c,String a) throws java.security.cert.CertificateException { throw new java.security.cert.CertificateException("Client certificate not supported"); }
            public void checkServerTrusted(X509Certificate[] certs,String auth) throws java.security.cert.CertificateException {
                try {
                    if(certs.length==0) throw new IOException("Missing certificate");
                    byte[] hash=MessageDigest.getInstance("SHA-256").digest(certs[0].getEncoded());
                    StringBuilder actual=new StringBuilder(); for(byte b:hash) actual.append(String.format(Locale.ROOT,"%02x",b&255));
                    if(!actual.toString().equals(pin)) throw new IOException("Laptop certificate changed; scan again");
                } catch(Exception e) { throw new java.security.cert.CertificateException(e); }
            }
        };
        SSLContext ctx=SSLContext.getInstance("TLS"); ctx.init(null,new TrustManager[]{trust},new SecureRandom());
        SSLSocket s=(SSLSocket)ctx.getSocketFactory().createSocket(); track(s);
        try { s.setEnabledProtocols(new String[]{"TLSv1.2"}); s.setTcpNoDelay(true); s.connect(new InetSocketAddress(pair.getString("host"),pair.getInt("port")),10000); s.setSoTimeout(10000); s.startHandshake(); return s; }
        catch(Exception e) { sockets.remove(s); s.close(); throw e; }
    }
    private JSONObject hello(String kind) throws Exception { return new JSONObject().put("kind",kind).put("node","android").put("token",pair.getString("token")); }
    private static void send(OutputStream out,JSONObject message) throws Exception { synchronized(out) { out.write((message.toString()+"\n").getBytes(StandardCharsets.UTF_8)); out.flush(); } }
    private static JSONObject line(InputStream in) throws Exception {
        ByteArrayOutputStream b=new ByteArrayOutputStream(); int c;
        while((c=in.read())!=-1) { if(c==10) return new JSONObject(b.toString("UTF-8")); b.write(c); if(b.size()>8192) throw new IOException("Invalid laptop response"); }
        throw new EOFException("Laptop disconnected");
    }
    void start() { pool.execute(()->{
        try {
            SSLSocket control=connect(); BufferedInputStream in=new BufferedInputStream(control.getInputStream()); OutputStream out=control.getOutputStream();
            send(out,hello("control").put("runtime",NativeWorker.revision()).put("platform","android").put("simulator",android.os.Build.FINGERPRINT.startsWith("generic") || android.os.Build.MODEL.contains("sdk") || android.os.Build.MODEL.contains("Emulator")).put("budget_mib",pair.getInt("budget_mib")).put("session",UUID.randomUUID().toString()));
            if(!line(in).optBoolean("ok")) throw new IOException("Laptop rejected pairing");
            control.setSoTimeout(0); listener.update("Connected. The test keeps running if you lock this phone.");
            clock.scheduleAtFixedRate(()->{
                try { long[] s=NativeWorker.stats(); send(out,new JSONObject().put("op","stats").put("allocated_bytes",s[0]).put("peak_bytes",s[1]).put("graph_calls",s[2])
                        .put("cache_hit_bytes",s[3]).put("cache_stored_bytes",s[4]).put("cache_rejected",s[5]));
                    listener.update("Connected • "+(s[0]/1048576)+" MiB allocated • "+(s[3]/1048576)+" MiB from cache • "+s[2]+" completed graphs");
                } catch(Exception e) { fail(e); }
            },0,500,TimeUnit.MILLISECONDS);
            while(!closed) {
                JSONObject msg=line(in); String channel=msg.optString("channel");
                if(!msg.optString("op").equals("open") || !channel.matches("[0-9a-f]{32}") || !channels.tryAcquire()) throw new IOException("Invalid tunnel request");
                pool.execute(()->tunnel(channel));
            }
        } catch(Exception e) { fail(e); }
    }); }
    private void tunnel(String id) {
        Socket local=null; SSLSocket remote=null;
        try {
            remote=connect(); BufferedInputStream in=new BufferedInputStream(remote.getInputStream());
            send(remote.getOutputStream(),hello("data").put("channel",id));
            if(!line(in).optBoolean("ok")) throw new IOException("Tunnel rejected"); remote.setSoTimeout(0);
            local=new Socket(); track(local); local.setTcpNoDelay(true); local.connect(new InetSocketAddress("127.0.0.1",port),5000);
            final Socket a=local,b=remote;
            pool.execute(()->{ try { copy(a.getInputStream(),b.getOutputStream()); } catch(IOException ignored) {} finally { close(a); close(b); } });
            try { copy(in,local.getOutputStream()); } finally { close(local); close(remote); }
        } catch(Exception e) { if(!closed) fail(e); }
        finally { if(local!=null) {close(local);sockets.remove(local);} if(remote!=null) {close(remote);sockets.remove(remote);} channels.release(); }
    }
    private static void copy(InputStream in,OutputStream out) throws IOException { byte[] b=new byte[65536]; int n; while((n=in.read(b))!=-1) {out.write(b,0,n);out.flush();} }
    private static void close(Socket s) { try {s.close();} catch(IOException ignored) {} }
    private synchronized void fail(Exception e) { if(!closed) { stop(); listener.ended("Disconnected: "+e.getMessage()+". Tap Connect for the next test."); } }
    synchronized void stop() { closed=true; for(Socket s:sockets) close(s); sockets.clear(); clock.shutdownNow(); pool.shutdownNow(); }
}
