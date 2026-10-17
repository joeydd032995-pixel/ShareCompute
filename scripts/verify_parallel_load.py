#!/usr/bin/env python3
"""Parallel loading check: the endpoints' weights load at the same time, and the tokens do not change.

Runs the same three-worker loopback split twice, with SC_PARALLEL_LOAD=0 and with SC_PARALLEL_LOAD=1
(llama-parallel-load.patch), and requires:

1. both runs succeed and produce the same token ids;
2. the parallel run says it loaded the remote contexts in parallel, and the serial run does not, so a build that
   lost the patch cannot pass by running serially twice.

It prints both load times. It does not assert that the parallel run is faster: three workers on one shared
machine are a poor timing instrument, and F51 records the measured numbers.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

BUDGETS = (768, 2048, 1536)  # laptop, android, iphone
PROMPT = 'The capital of France is'
MARKER = 'remote contexts in parallel'

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]

def run_split(bins, suffix, model, parallel):
    """One probe run against three fresh workers. Returns the SC_RESULT, the probe's stderr and the wall seconds."""
    workers, ports = [], []
    for budget in BUDGETS:
        port = free_port(); ports.append(port)
        w = subprocess.Popen([str(bins / ('sc-rpc-worker' + suffix)), str(port), str(budget), '2'],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        # Four stats lines a second would fill the pipe and stall the worker.
        threading.Thread(target=lambda w=w: [None for _ in w.stdout], daemon=True).start()
        workers.append(w)
    try:
        for port in ports:
            for _ in range(100):
                try: socket.create_connection(('127.0.0.1', port), 1).close(); break
                except OSError: time.sleep(.1)
            else: raise RuntimeError('worker did not start')
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'probe.json'
            cfg.write_text(json.dumps({'model': str(model.resolve()), 'prompt': PROMPT, 'tokens': 8,
                                       'endpoints': [f'127.0.0.1:{p}' for p in ports], 'shares': list(BUDGETS)}))
            started = time.time()
            probe = subprocess.run([str(bins / ('sc-split-probe' + suffix)), str(cfg)], capture_output=True, text=True, timeout=1800,
                                   env=dict(os.environ, SC_PARALLEL_LOAD='1' if parallel else '0'))
            wall = time.time() - started
    finally:
        for w in workers: w.terminate()
        for w in workers:
            try: w.wait(timeout=10)
            except subprocess.TimeoutExpired: w.kill(); w.wait()
    result = next((json.loads(l[10:]) for l in probe.stdout.splitlines() if l.startswith('SC_RESULT ')), None)
    return result, probe.stderr, probe.returncode, wall

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--bin-dir', type=Path, required=True); p.add_argument('--model', type=Path, required=True)
    p.add_argument('--out', type=Path, default=None, help='write the summary here as JSON')
    a = p.parse_args(); suffix = '.exe' if os.name == 'nt' else ''
    summary = {}
    for name, parallel in (('serial', False), ('parallel', True)):
        result, stderr, code, wall = run_split(a.bin_dir, suffix, a.model, parallel)
        assert code == 0 and result, (name, code, stderr[-400:])
        said = MARKER in stderr
        assert said == parallel, f'{name}: the "{MARKER}" line should be {"present" if parallel else "absent"}'
        summary[name] = {'tokens': result['token_ids'], 'text': result['text'], 'wall_s': round(wall, 1), 'said_parallel': said}
        print(f'{name}: {wall:.1f} s, tokens {result["token_ids"]}', flush=True)
    assert summary['serial']['tokens'] == summary['parallel']['tokens'], summary
    print('VERIFIED parallel loading gives the same tokens as serial loading, and the switch is live', flush=True)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True); a.out.write_text(json.dumps(summary, indent=2) + '\n')

if __name__ == '__main__': main()
