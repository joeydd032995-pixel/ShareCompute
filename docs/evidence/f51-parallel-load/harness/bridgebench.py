#!/usr/bin/env python3
"""Throughput of the relay's byte path: probe -> plain socket -> bridge -> TLS -> bridge -> plain sink, N streams at once.

Mirrors split_cluster: Relay.forward bridges plain<->TLS-server-side, Worker.tunnel bridges TLS-client-side<->plain.
usage: bridgebench.py MODE STREAMS MIB_PER_STREAM [CHUNK_KIB]    MODE = asyncio | threads
"""
import asyncio, os, socket, ssl, subprocess, sys, tempfile, threading, time
from pathlib import Path

mode, streams, mib = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
chunk = int(sys.argv[4]) * 1024 if len(sys.argv) > 4 else 65536
tmp = Path(tempfile.mkdtemp())
subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes', '-keyout', str(tmp/'k.pem'),
                '-out', str(tmp/'c.pem'), '-days', '1', '-subj', '/CN=ShareCompute'], check=True, capture_output=True)
sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sctx.load_cert_chain(tmp/'c.pem', tmp/'k.pem'); sctx.minimum_version = ssl.TLSVersion.TLSv1_2
cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); cctx.check_hostname = False; cctx.verify_mode = ssl.CERT_NONE; cctx.minimum_version = ssl.TLSVersion.TLSv1_2
total = mib * 1048576

def sink_server():
    """The worker: reads everything, counts it."""
    srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(16); got = {'n': 0}; lock = threading.Lock()
    def serve():
        def one(c):
            n = 0
            while True:
                b = c.recv(1 << 20)
                if not b: break
                n += len(b)
            with lock: got['n'] += n
            c.close()
        while True:
            try: c, _ = srv.accept()
            except OSError: return
            threading.Thread(target=one, args=(c,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return srv, got

def sender(port):
    s = socket.create_connection(('127.0.0.1', port)); buf = os.urandom(1 << 20); sent = 0
    while sent < total:
        s.sendall(buf[:min(len(buf), total - sent)]); sent += min(len(buf), total - sent)
    s.shutdown(socket.SHUT_WR); s.recv(1); s.close()

# ------------------------------------------------------------------ asyncio path (as in split_cluster.bridge)
async def abridge(left, right):
    async def copy(src, dst):
        try:
            while data := await src.read(chunk):
                dst.write(data); await dst.drain()
        finally:
            try: dst.close()
            except Exception: pass
    await asyncio.gather(copy(left[0], right[1]), copy(right[0], left[1]), return_exceptions=True)

async def asyncio_main(sink_port):
    tls_ready = {}
    async def tls_side(reader, writer):           # Worker.tunnel: TLS reader -> plain worker socket
        local = await asyncio.open_connection('127.0.0.1', sink_port)
        await abridge((reader, writer), local)
    tls_srv = await asyncio.start_server(tls_side, '127.0.0.1', 0, ssl=sctx, limit=16384)
    tls_port = tls_srv.sockets[0].getsockname()[1]
    async def plain_side(reader, writer):          # Relay.forward: plain probe socket -> TLS
        remote = await asyncio.open_connection('127.0.0.1', tls_port, ssl=cctx, server_hostname='ShareCompute', limit=16384)
        await abridge((reader, writer), remote)
    plain_srv = await asyncio.start_server(plain_side, '127.0.0.1', 0)
    plain_port = plain_srv.sockets[0].getsockname()[1]
    return plain_port

# ------------------------------------------------------------------ threaded path
def pump(src, dst):
    buf = bytearray(chunk); view = memoryview(buf)
    try:
        while True:
            n = src.recv_into(buf)
            if not n: break
            dst.sendall(view[:n])
    except OSError: pass
    finally:
        try: dst.shutdown(socket.SHUT_WR)
        except OSError: pass

def tbridge(a, b):
    t = threading.Thread(target=pump, args=(a, b), daemon=True); t.start(); pump(b, a); t.join(); a.close(); b.close()

def threads_main(sink_port):
    tls_srv = socket.socket(); tls_srv.bind(('127.0.0.1', 0)); tls_srv.listen(16); tls_port = tls_srv.getsockname()[1]
    def tls_accept():
        while True:
            c, _ = tls_srv.accept()
            def one(c=c):
                s = sctx.wrap_socket(c, server_side=True); local = socket.create_connection(('127.0.0.1', sink_port)); tbridge(s, local)
            threading.Thread(target=one, daemon=True).start()
    threading.Thread(target=tls_accept, daemon=True).start()
    plain = socket.socket(); plain.bind(('127.0.0.1', 0)); plain.listen(16); plain_port = plain.getsockname()[1]
    def plain_accept():
        while True:
            c, _ = plain.accept()
            def one(c=c):
                r = cctx.wrap_socket(socket.create_connection(('127.0.0.1', tls_port)), server_hostname='ShareCompute'); tbridge(c, r)
            threading.Thread(target=one, daemon=True).start()
    threading.Thread(target=plain_accept, daemon=True).start()
    return plain_port

srv, got = sink_server(); sink_port = srv.getsockname()[1]
if mode == 'asyncio':
    loop = asyncio.new_event_loop()
    ready = threading.Event(); box = {}
    def run():
        asyncio.set_event_loop(loop)
        box['port'] = loop.run_until_complete(asyncio_main(sink_port)); ready.set(); loop.run_forever()
    threading.Thread(target=run, daemon=True).start(); ready.wait(); port = box['port']
else:
    port = threads_main(sink_port)
t0 = time.time()
ts = [threading.Thread(target=sender, args=(port,)) for _ in range(streams)]
[t.start() for t in ts]; [t.join() for t in ts]
dt = time.time() - t0
time.sleep(0.2)
print(f'{mode:8s} streams={streams} chunk={chunk//1024}KiB  {streams*mib} MiB in {dt:.1f}s = {streams*mib/dt:.0f} MiB/s total, {mib/dt:.0f} MiB/s per stream; sink received {got["n"]/1048576:.0f} MiB')
