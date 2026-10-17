#!/usr/bin/env python3
"""Loopback measurement of the 3B model load: three workers at the real budgets, phones with a hash cache.

usage: loadtime.py BIN_DIR MODEL CACHE_ROOT LABEL   (CACHE_ROOT is wiped by the caller for a cold run)
"""
import json, os, socket, subprocess, sys, tempfile, threading, time
from pathlib import Path

bins, model, cache_root, label = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
NODES = [('laptop', 768, False), ('android', 2048, True), ('iphone', 1536, True)]

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]

workers, readers, ports = [], [], []
for name, budget, cached in NODES:
    port = free_port(); ports.append(port)
    args = [str(bins / 'sc-rpc-worker'), str(port), str(budget), '2']
    if cached:
        d = cache_root / name; d.mkdir(parents=True, exist_ok=True); args.append(str(d))
    w = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []; t = threading.Thread(target=lambda w=w, l=lines: l.extend(w.stdout), daemon=True); t.start()
    workers.append(w); readers.append((name, lines, t))
try:
    for port in ports:
        for _ in range(100):
            try: socket.create_connection(('127.0.0.1', port), 1).close(); break
            except OSError: time.sleep(.1)
        else: raise SystemExit('worker did not start')
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / 'probe.json'
        cfg.write_text(json.dumps({'model': str(model.resolve()), 'prompt': 'The capital of France is', 'tokens': 4,
                                   'endpoints': [f'127.0.0.1:{p}' for p in ports], 'shares': [b for _, b, _ in NODES]}))
        t0 = time.time()
        probe = subprocess.run([str(bins / 'sc-split-probe'), str(cfg)], capture_output=True, text=True, timeout=1800)
        wall = time.time() - t0
finally:
    for w in workers: w.terminate()
    for w in workers:
        try: w.wait(timeout=10)
        except subprocess.TimeoutExpired: w.kill()
result = next((json.loads(l[10:]) for l in probe.stdout.splitlines() if l.startswith('SC_RESULT ')), None)
sc_load = [l for l in probe.stderr.splitlines() if l.startswith('SC_LOAD')]
out = {'label': label, 'probe_exit': probe.returncode, 'wall_s': round(wall, 1), 'result': result and {**{k: round(result[k]) for k in ('prefill_ms', 'decode_ms', 'wall_ms')}, 'tokens': result['token_ids']},
       'sc_load': sc_load}
print(json.dumps(out, indent=1))
