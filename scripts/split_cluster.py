#!/usr/bin/env python3
"""Native three-worker model split over pinned TLS reverse tunnels (Python 3.11+)."""
import argparse
import asyncio
import contextlib
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import uuid

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
REV = (ROOT / 'native/split/llama-revision.txt').read_text().strip()
NODES = ('laptop', 'android', 'iphone')
PROMPT = 'The capital of France is'
CORE_STATS = ('allocated_bytes', 'peak_bytes', 'graph_calls')
# Weight-cache counters. Optional on the wire so a phone app built before the cache still joins.
CACHE_STATS = ('cache_hit_bytes', 'cache_stored_bytes', 'cache_rejected')

def cache_delta(before, after):
    """Cache activity during one run. Phone workers outlive runs, so their counters are cumulative."""
    return {k: after.get(k, 0) - before.get(k, 0) for k in CACHE_STATS}

def private_json(path, obj):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f: json.dump(obj, f, indent=2)

def init_cluster(directory, host, port=9443, budgets=(768, 2048, 1536)):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    if not 0 <= port <= 65535 or any(not 64 <= b <= 4096 for b in budgets):
        raise ValueError('Port must be 0..65535 and each RPC buffer budget 64..4096 MiB')
    directory.mkdir(parents=True, exist_ok=False)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'ShareCompute paired laptop')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    for filename, data in [('cert.pem', cert.public_bytes(serialization.Encoding.PEM)),
                           ('key.pem', key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption()))]:
        fd = os.open(directory / filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as f: f.write(data)
    pin = cert.fingerprint(hashes.SHA256()).hex()
    config = {'host': host, 'port': port, 'pin': pin, 'runtime': REV, 'nodes': {}}
    for node, budget in zip(NODES, budgets):
        pair = {'host': host, 'port': port, 'pin': pin, 'runtime': REV, 'node': node,
                'token': secrets.token_hex(32), 'budget_mib': budget}
        config['nodes'][node] = pair
        private_json(directory / f'{node}.json', pair)
    private_json(directory / 'cluster.json', config)
    return config

async def send(writer, obj):
    writer.write(json.dumps(obj, separators=(',', ':')).encode() + b'\n')
    await writer.drain()

class PeerClosed(ConnectionError):
    pass

# A phone sends a heartbeat twice a second. If none is read for this long, the session is dropped.
RECEIVE_LIMIT = 10

def describe_silence(node, stalled):
    """Say whose fault a missed heartbeat was. They need opposite fixes: the laptop froze, or the phone went quiet."""
    if stalled > 3:
        return (f'{node} control disconnected: the laptop itself stopped responding for about {stalled:.0f} s, so it '
                f'could not read the {node} app\'s heartbeat. The laptop is probably short of memory: close other '
                f'programs on it and try again.')
    return (f'{node} control disconnected: no heartbeat from the {node} app for {RECEIVE_LIMIT} s while the laptop was '
            f'responding. The app was probably locked, in the background or off Wi-Fi: keep it on screen with the '
            f'phone unlocked and on the same Wi-Fi.')

async def receive(reader, timeout=None):
    line = await asyncio.wait_for(reader.readline(), RECEIVE_LIMIT if timeout is None else timeout)
    if not line: raise PeerClosed('The other side closed the connection')
    if len(line) > 8192: raise ConnectionError('Oversized protocol message')
    result = json.loads(line)
    if not isinstance(result, dict): raise ValueError('Protocol requires a JSON object')
    return result

async def close(writer):
    writer.close()
    with contextlib.suppress(Exception): await asyncio.wait_for(writer.wait_closed(), 2)

async def bridge(left, right, counts=None):
    async def copy(src, dst, index):
        while chunk := await src.read(65536):
            dst.write(chunk); await dst.drain()
            if counts is not None: counts[index] += len(chunk)
    tasks = [asyncio.create_task(copy(left[0], right[1], 0)), asyncio.create_task(copy(right[0], left[1], 1))]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done: task.result()
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(close(left[1]), close(right[1]))
        await asyncio.gather(*tasks, return_exceptions=True)

async def paired_connection(pair):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    reader, writer = await asyncio.wait_for(asyncio.open_connection(pair['host'], pair['port'], ssl=ctx,
                                                                   server_hostname='ShareCompute', limit=16384), 10)
    cert = writer.get_extra_info('ssl_object').getpeercert(binary_form=True)
    if not hmac.compare_digest(hashlib.sha256(cert).hexdigest(), pair['pin']):
        await close(writer); raise ConnectionError('Laptop certificate pin mismatch; no credential sent')
    return reader, writer

class Relay:
    def __init__(self, config, directory):
        self.config = config; self.directory = directory
        self.nodes = {}; self.pending = {}; self.tasks = set(); self.writers = set(); self.listeners = []
        self.failure = asyncio.Event(); self.reason = ''; self.closing = False
        self.last_tick = time.monotonic(); self.worst_stall = 0.0; self.stall_at = 0.0; self.monitor = None

    def fail(self, reason):
        if not self.closing:
            self.reason = self.reason or str(reason); self.failure.set()

    async def start(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.directory / 'cert.pem', self.directory / 'key.pem')
        self.server = await asyncio.start_server(self.accept, '0.0.0.0', self.config['port'], ssl=ctx,
                                                 ssl_handshake_timeout=10, limit=16384)
        self.port = self.server.sockets[0].getsockname()[1]
        self.monitor = asyncio.create_task(self.watch_loop())

    async def watch_loop(self):
        """Notice when this process stops running, which makes every device look silent at once."""
        while True:
            before = time.monotonic(); await asyncio.sleep(.5); now = time.monotonic()
            late = now - before - .5; self.last_tick = now
            if late > 2:
                self.worst_stall = max(self.worst_stall, late); self.stall_at = now
                print(f'Laptop stalled for {late:.1f} s: this program did not run (low memory or a busy disk).', flush=True)

    def stalled_for(self):
        """Seconds this loop has recently gone without running, whether or not the monitor has caught up yet."""
        now = time.monotonic()
        return max(now - self.last_tick - .5, self.worst_stall if now - self.stall_at < 30 else 0.0)

    async def accept(self, reader, writer):
        task = asyncio.current_task(); self.tasks.add(task); self.writers.add(writer)
        node = kind = None; control = False
        try:
            hello = await receive(reader)
            node = hello.get('node'); kind = hello.get('kind')
            if not isinstance(node, str) or node not in self.config['nodes']: raise ValueError('Unknown node')
            pair = self.config['nodes'][node]
            token = hello.get('token')
            if not isinstance(token, str) or not hmac.compare_digest(token, pair['token']): raise ValueError('Authentication failed')
            if kind == 'control':
                if node in self.nodes: raise ValueError('Duplicate worker')
                if hello.get('runtime') != REV or hello.get('budget_mib') != pair['budget_mib']:
                    raise ValueError('Runtime or memory budget mismatch')
                control = True
                record = {'writer': writer, 'hello': hello, 'peer': writer.get_extra_info('peername')[0],
                          'stats': dict.fromkeys(CORE_STATS + CACHE_STATS, 0), 'bytes': [0, 0]}
                self.nodes[node] = record
                listener = await asyncio.start_server(lambda r, w: self.forward(node, r, w), '127.0.0.1', 0)
                self.listeners.append(listener)
                record['endpoint'] = f"127.0.0.1:{listener.sockets[0].getsockname()[1]}"
                await send(writer, {'ok': True})
                print(f'Joined {node}: {hello.get("platform")} budget={pair["budget_mib"]} MiB', flush=True)
                while True:
                    message = await receive(reader)
                    if message.get('op') != 'stats': raise ValueError('Expected telemetry heartbeat')
                    stats = {k: message.get(k) for k in CORE_STATS} | {k: message.get(k, 0) for k in CACHE_STATS}
                    if any(type(v) is not int or v < 0 for v in stats.values()): raise ValueError('Invalid telemetry')
                    record['stats'] = stats
            elif kind == 'data':
                channel = hello.get('channel')
                if not isinstance(channel, str): raise ValueError('Invalid channel')
                pending = self.pending.get((node, channel))
                if pending is None or pending.done(): raise ValueError('Unsolicited data channel')
                await send(writer, {'ok': True})
                completed = asyncio.Event(); pending.set_result((reader, writer, completed))
                await completed.wait()
            else: raise ValueError('Unknown connection kind')
        except asyncio.CancelledError: raise
        except Exception as e:
            if control and isinstance(e, PeerClosed):
                self.fail(f'{node} control disconnected: the {node} app closed the connection '
                          f'(it left the screen, was disconnected, or stopped). Keep both phone apps open '
                          f'until the result appears, then press Start test and scan again.')
            elif control and isinstance(e, (asyncio.TimeoutError, TimeoutError)): self.fail(describe_silence(node, self.stalled_for()))
            elif control: self.fail(f'{node} control disconnected: {str(e) or type(e).__name__}')
            else:
                # Name only known values; the hello is unauthenticated and must not be echoed.
                who = node if isinstance(node, str) and node in self.config['nodes'] else 'unknown'
                what = kind if isinstance(kind, str) and kind in ('control', 'data') else 'unknown'
                peer = (writer.get_extra_info('peername') or ('unknown address',))[0]
                print(f'Rejected {who} {what} connection from {peer}: {str(e) or type(e).__name__}', flush=True)
        finally:
            await close(writer); self.writers.discard(writer); self.tasks.discard(task)

    async def forward(self, node, reader, writer):
        task = asyncio.current_task(); self.tasks.add(task); self.writers.add(writer)
        channel = uuid.uuid4().hex; key = (node, channel); completed = None
        try:
            if sum(n == node for n, _ in self.pending) >= 4: raise RuntimeError('Worker channel limit')
            future = asyncio.get_running_loop().create_future(); self.pending[key] = future
            await send(self.nodes[node]['writer'], {'op': 'open', 'channel': channel})
            remote_reader, remote_writer, completed = await asyncio.wait_for(future, 10)
            await bridge((reader, writer), (remote_reader, remote_writer), self.nodes[node]['bytes'])
        except asyncio.CancelledError: raise
        except Exception as e: self.fail(f'{node} tunnel failed: {e}')
        finally:
            self.pending.pop(key, None)
            if completed: completed.set()
            await close(writer); self.writers.discard(writer); self.tasks.discard(task)

    async def stop(self):
        self.closing = True
        if self.monitor: self.monitor.cancel(); await asyncio.gather(self.monitor, return_exceptions=True)
        listeners = [self.server, *self.listeners]
        for listener in listeners: listener.close()
        # Python 3.12+ waits for accepted connections in Server.wait_closed().
        # Disconnect external phone controls before waiting for the TLS server.
        for task in list(self.tasks): task.cancel()
        await asyncio.gather(*(close(w) for w in list(self.writers)), return_exceptions=True)
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await asyncio.gather(*(listener.wait_closed() for listener in listeners))

async def terminate(proc):
    if proc and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError): proc.terminate()
        try: await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError): proc.kill()
            await proc.wait()

def check_binary(path):
    import subprocess
    if subprocess.check_output([str(Path(path).resolve()), '--version'], text=True, timeout=10).strip() != REV:
        raise RuntimeError('Native binary revision does not match this checkout')

class Worker:
    def __init__(self, pair, binary, log, cache_dir=None):
        self.pair = pair; self.binary = str(Path(binary).resolve()); self.log = log
        self.cache_dir = cache_dir  # None leaves the native weight cache off
        self.stats = dict.fromkeys(CORE_STATS + CACHE_STATS, 0)
        self.proc = None; self.channels = set(); self.data_failed = asyncio.Event()

    async def tunnel(self, channel):
        remote = await paired_connection(self.pair)
        try:
            await send(remote[1], {'kind': 'data', 'node': self.pair['node'], 'token': self.pair['token'], 'channel': channel})
            if not (await receive(remote[0])).get('ok'): raise ConnectionError('Data authentication rejected')
            local = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', self.port), 5)
            await bridge(remote, local)
        finally: await close(remote[1])

    def channel_done(self, task):
        self.channels.discard(task)
        if not task.cancelled() and task.exception(): self.data_failed.set()

    async def run(self):
        if self.pair['runtime'] != REV: raise RuntimeError('Pairing runtime mismatch')
        check_binary(self.binary)
        with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        tasks = []; writer = None
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open('w', encoding='utf-8') as log:
            try:
                cache = [str(Path(self.cache_dir).resolve())] if self.cache_dir else []
                self.proc = await asyncio.create_subprocess_exec(self.binary, str(self.port), str(self.pair['budget_mib']), '2', *cache,
                                                                 stdout=asyncio.subprocess.PIPE, stderr=log)
                async def telemetry():
                    while line := await self.proc.stdout.readline():
                        text = line.decode(errors='replace'); log.write(text); log.flush()
                        if text.startswith('SC_STATS '): self.stats = json.loads(text[9:])
                    raise ConnectionError('Native worker exited')
                tasks.append(asyncio.create_task(telemetry()))
                for attempt in range(100):
                    if self.proc.returncode is not None: raise RuntimeError('Native listener exited')
                    try:
                        _, probe_writer = await asyncio.open_connection('127.0.0.1', self.port)
                        await close(probe_writer); break
                    except OSError: await asyncio.sleep(.05)
                else: raise TimeoutError('Native RPC listener did not start')
                reader, writer = await paired_connection(self.pair)
                await send(writer, {'kind': 'control', 'node': self.pair['node'], 'token': self.pair['token'],
                                    'runtime': REV, 'platform': 'android' if os.environ.get('ANDROID_ROOT') else platform.system().lower(),
                                    'simulator': False, 'budget_mib': self.pair['budget_mib'], 'session': uuid.uuid4().hex})
                if not (await receive(reader)).get('ok'): raise ConnectionError('Control authentication rejected')
                async def heartbeat():
                    while True: await send(writer, {'op': 'stats', **self.stats}); await asyncio.sleep(.5)
                async def commands():
                    while True:
                        message = await receive(reader, 3600)
                        channel = message.get('channel')
                        if message.get('op') != 'open' or not isinstance(channel, str) or len(channel) != 32:
                            raise ValueError('Invalid open command')
                        if len(self.channels) >= 4: raise RuntimeError('Channel limit')
                        task = asyncio.create_task(self.tunnel(channel)); self.channels.add(task); task.add_done_callback(self.channel_done)
                tasks += [asyncio.create_task(heartbeat()), asyncio.create_task(commands()), asyncio.create_task(self.data_failed.wait())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done: task.result()
                raise ConnectionError('Worker connection ended')
            finally:
                for task in [*tasks, *self.channels]: task.cancel()
                if writer: await close(writer)
                await terminate(self.proc)
                await asyncio.gather(*tasks, *self.channels, return_exceptions=True)

async def probe(binary, config, out, name, timeout, relay=None):
    cfg = out / f'{name}-config.json'; cfg.write_text(json.dumps(config))
    proc = await asyncio.create_subprocess_exec(str(Path(binary).resolve()), str(cfg), stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.STDOUT)
    communicate = asyncio.create_task(proc.communicate()); failed = None
    try:
        tasks = [communicate]
        if relay: failed = asyncio.create_task(relay.failure.wait()); tasks.append(failed)
        done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if communicate not in done:
            await terminate(proc)
            output, _ = await communicate
            (out / f'{name}.log').write_bytes(output)
            raise RuntimeError(relay.reason if failed and failed in done else f'{name} timed out')
        output, _ = communicate.result(); text = output.decode(errors='replace')
        (out / f'{name}.log').write_text(text, encoding='utf-8')
        if proc.returncode != 0: raise RuntimeError(f'{name} inference exited {proc.returncode}')
        records = [json.loads(line[10:]) for line in text.splitlines() if line.startswith('SC_RESULT ')]
        if len(records) != 1 or not records[0].get('ok') or not records[0].get('token_ids'):
            raise RuntimeError('Native probe did not produce exactly one complete generation')
        return records[0], text
    finally:
        if failed: failed.cancel(); await asyncio.gather(failed, return_exceptions=True)
        await terminate(proc)

SMALL_MODEL_NOTE = 'Small-model splitting proof; does not prove a model larger than any one device fits.'
CAPACITY_NOTE = ('The workers\' combined allocations during this run exceed the largest single worker budget, so no one '
                 'worker budget could have held this model. This is a budget result: it does not show the model would not fit in a '
                 'device\'s physical memory, and platform flags are worker reports, not hardware attestation.')

async def file_sha256(path):
    """Checksum a file without stopping the event loop.

    The relay reads each device's heartbeat with a 10 second limit. A multi-gigabyte checksum run on the
    loop itself freezes that for as long as it takes, and a slow laptop takes well over 10 seconds, so every
    connected device was dropped with "control disconnected: TimeoutError" (F41).
    """
    stop = threading.Event()
    try: return await asyncio.to_thread(_sha256_file, path, stop)
    except asyncio.CancelledError:
        # Cancelling the await does not stop the thread, and asyncio.run() waits for it before the launcher
        # can exit, so Stop would hang for the rest of a multi-gigabyte checksum. The thread checks this.
        stop.set(); raise

HASH_CHUNK = 8 << 20

def _sha256_file(path, stop):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        while chunk := f.read(HASH_CHUNK):
            if stop.is_set(): raise InterruptedError('Stopped')
            digest.update(chunk)
    return digest.hexdigest()

def load_reference(path, model_sha256, prompt, tokens):
    """Tokens produced elsewhere by this runtime, for a model too large to run unsplit on the laptop."""
    ref = json.loads(Path(path).read_text(encoding='utf-8'))
    for key, want in (('runtime', REV), ('model_sha256', model_sha256), ('prompt', prompt), ('tokens', tokens)):
        if ref.get(key) != want: raise ValueError(f'Pinned reference does not match this run: {key}')
    ids = ref.get('token_ids')
    if not isinstance(ids, list) or not ids or len(ids) > tokens or not all(isinstance(t, int) for t in ids):
        raise ValueError('Pinned reference has no usable token_ids')
    return ref

def capacity_summary(workers, model_file_bytes):
    """Whether the workers' combined allocations are more than any one worker's budget allows.

    Uses what each worker held during this run. A phone's native worker lives as long as its app, and its
    reported peak is a high-water mark for that whole life, so an earlier larger run would otherwise be
    counted again in a later, smaller one.
    """
    combined = sum(w['run_allocated_bytes'] for w in workers.values())
    largest = max(w['budget_mib'] for w in workers.values()) * 1048576
    return {'model_file_bytes': model_file_bytes, 'combined_run_allocated_bytes': combined,
            'largest_worker_budget_bytes': largest, 'exceeds_largest_worker_budget': combined > largest}

def validate_proof(baseline, result, log, records, before, physical):
    if not baseline.get('token_ids') or result.get('token_ids') != baseline['token_ids']:
        raise ValueError('Split greedy tokens differ from the ' + ('pinned reference' if baseline.get('profile') else 'local baseline'))
    workers = {}; assigned = set()
    for index, node in enumerate(NODES):
        layers = sorted(set(map(int, re.findall(r'layer\s+(\d+)\s+assigned to device RPC'+str(index)+r'\b', log))))
        rec = records[node]; stats = rec['stats']; calls = stats['graph_calls'] - before[node]
        if not layers or assigned.intersection(layers): raise ValueError(f'{node} has missing or overlapping model layers')
        assigned.update(layers)
        if calls <= 0 or stats['peak_bytes'] <= 0 or min(rec['bytes']) <= 0:
            raise ValueError(f'{node} lacks native compute, allocation, or bidirectional data evidence')
        hello = rec['hello']
        workers[node] = {'layers': layers, 'graph_calls': calls, 'peak_bytes': stats['peak_bytes'],
                         'bytes_to_worker': rec['bytes'][0], 'bytes_from_worker': rec['bytes'][1],
                         'platform': hello.get('platform'), 'simulator': hello.get('simulator'),
                         'peer': rec['peer'], 'budget_mib': hello['budget_mib']}
    if physical:
        if workers['android']['platform'] != 'android' or workers['iphone']['platform'] != 'ios':
            raise ValueError('Physical proof requires Android and native iOS workers')
        for node in ('android', 'iphone'):
            if workers[node]['simulator'] or ipaddress.ip_address(workers[node]['peer']).is_loopback:
                raise ValueError(f'{node} is a simulator or loopback worker')
        if workers['android']['peer'] == workers['iphone']['peer']: raise ValueError('Phones must have distinct LAN addresses')
    return workers

async def run_coordinator(a):
    out = a.out.resolve(); out.mkdir(parents=True, exist_ok=False)
    local = a.mode != 'coordinator'; physical = not local
    directory = out / 'pairing' if local else a.config.resolve().parent
    config = init_cluster(directory, '127.0.0.1', 0, a.budgets) if local else json.loads(a.config.read_text())
    relay = Relay(config, directory); workers = {}; tasks = []; fault = None
    report = {'status': 'FAIL', 'physical_devices': physical, 'runtime': REV,
              'scope': 'physical-LAN' if physical else ('native-ios-simulator' if a.mode == 'simulator' else 'three-process-loopback'),
              'identity_note': 'Platform and simulator flags are worker reports, not hardware attestation.',
              'capacity_note': SMALL_MODEL_NOTE}
    try:
        check_binary(a.worker_binary); check_binary(a.probe_binary)
        await relay.start()
        if local:
            for node in NODES:
                config['nodes'][node]['port'] = relay.port
                (directory / f'{node}.json').write_text(json.dumps(config['nodes'][node]))
        (out / 'pairing-ready').write_text(str(directory))
        for node in (NODES if a.mode == 'loopback' else ('laptop', 'android') if a.mode == 'simulator' else ('laptop',)):
            pair = dict(config['nodes'][node]); pair['host'] = '127.0.0.1'; pair['port'] = relay.port
            cache_dir = getattr(a, 'cache_dir', None)
            worker = Worker(pair, a.worker_binary, out / f'{node}-worker.log', cache_dir and Path(cache_dir) / node)
            workers[node] = worker
            task = asyncio.create_task(worker.run()); tasks.append(task)
            def ended(t, n=node):
                if not t.cancelled() and t.exception(): relay.fail(f'{n}: {t.exception()}')
            task.add_done_callback(ended)
        deadline = time.monotonic() + a.join_timeout
        while len(relay.nodes) < 3:
            if relay.failure.is_set(): raise RuntimeError(relay.reason)
            if time.monotonic() > deadline: raise TimeoutError('Waiting for all three workers timed out')
            await asyncio.sleep(.1)
        cfg = {'model': str(a.model.resolve()), 'prompt': a.prompt, 'tokens': a.tokens, 'endpoints': [], 'shares': []}
        reference = getattr(a, 'reference', None)
        model_sha256 = await file_sha256(a.model)
        if reference:
            baseline = load_reference(reference, model_sha256, a.prompt, a.tokens); report['baseline_source'] = 'pinned-reference'
        else:
            baseline, _ = await probe(a.probe_binary, cfg, out, 'baseline', a.timeout, relay); report['baseline_source'] = 'local-run'
        before = {n: relay.nodes[n]['stats']['graph_calls'] for n in NODES}
        cache_before = {n: dict(relay.nodes[n]['stats']) for n in NODES}
        if a.kill_worker:
            if a.kill_worker not in workers: raise ValueError('Fault target is not a locally managed worker')
            async def inject():
                target = workers[a.kill_worker]
                while target.stats['graph_calls'] <= before[a.kill_worker]: await asyncio.sleep(.01)
                report['fault_trigger'] = {'node': a.kill_worker, 'graph_calls_before_kill': target.stats['graph_calls']}
                target.proc.kill()
            fault = asyncio.create_task(inject())
        cfg['endpoints'] = [relay.nodes[n]['endpoint'] for n in NODES]
        cfg['shares'] = [config['nodes'][n]['budget_mib'] for n in NODES]
        held = dict.fromkeys(NODES, 0)
        async def sample():
            while True:
                for n in NODES: held[n] = max(held[n], relay.nodes[n]['stats']['allocated_bytes'])
                await asyncio.sleep(.1)
        sampler = asyncio.create_task(sample())
        try: result, log = await probe(a.probe_binary, cfg, out, 'split', a.timeout, relay)
        finally:
            sampler.cancel(); await asyncio.gather(sampler, return_exceptions=True)
        await asyncio.sleep(.8) # Allow final counters to traverse the heartbeat connection.
        if relay.failure.is_set(): raise RuntimeError(relay.reason)
        report['workers'] = validate_proof(baseline, result, log, relay.nodes, before, physical)
        for n in NODES:
            report['workers'][n]['cache'] = cache_delta(cache_before[n], relay.nodes[n]['stats'])
            report['workers'][n]['run_allocated_bytes'] = held[n]
        report['generation'] = result; report['matches_baseline'] = True; report['model_sha256'] = model_sha256
        report['capacity'] = capacity_summary(report['workers'], a.model.stat().st_size)
        if report['capacity']['exceeds_largest_worker_budget']: report['capacity_note'] = CAPACITY_NOTE
        report['status'] = 'PASS'
    except Exception as e:
        report['error'] = str(e)
    finally:
        if fault: fault.cancel(); await asyncio.gather(fault, return_exceptions=True)
        relay.closing = True
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if hasattr(relay, 'server'): await relay.stop()
        (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2)); return 0 if report['status'] == 'PASS' else 1

def main():
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('init'); p.add_argument('--dir', type=Path, default=Path('.sc-pairing'))
    p.add_argument('--host', required=True); p.add_argument('--port', type=int, default=9443)
    p.add_argument('--budgets', nargs=3, type=int, default=[768,2048,1536])
    p = sub.add_parser('worker'); p.add_argument('--pair', type=Path, required=True); p.add_argument('--binary', type=Path, required=True)
    for mode in ('loopback', 'coordinator', 'simulator'):
        p = sub.add_parser(mode); p.add_argument('--config', type=Path, default=Path('.sc-pairing/cluster.json'))
        p.add_argument('--worker-binary', type=Path, required=True); p.add_argument('--probe-binary', type=Path, required=True)
        p.add_argument('--model', type=Path, required=True); p.add_argument('--out', type=Path, required=True)
        p.add_argument('--tokens', type=int, default=24); p.add_argument('--prompt', default=PROMPT)
        p.add_argument('--reference', type=Path, default=None,
                       help='Compare against pinned tokens instead of running the model unsplit first (for a model too big for one machine).')
        p.add_argument('--join-timeout', type=float, default=180); p.add_argument('--timeout', type=float, default=300)
        p.add_argument('--budgets', nargs=3, type=int, default=[768,2048,1536])
        p.add_argument('--kill-worker', choices=NODES if mode == 'loopback' else ['android'] if mode == 'simulator' else [], default=None)
        p.add_argument('--cache-dir', type=Path, default=None,
                       help='Give each worker this process starts a weight cache under DIR/<node>. Phones manage their own.')
    a = parser.parse_args()
    if a.mode == 'init': init_cluster(a.dir, a.host, a.port, a.budgets); print(f'Pairing files created in {a.dir}'); return 0
    if a.mode == 'worker':
        asyncio.run(Worker(json.loads(a.pair.read_text()), a.binary, Path('split-runs/worker.log')).run()); return 0
    return asyncio.run(run_coordinator(a))

if __name__ == '__main__':
    try: raise SystemExit(main())
    except KeyboardInterrupt: raise SystemExit(130)
