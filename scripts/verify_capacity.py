#!/usr/bin/env python3
"""Capacity check: the 3B model is refused by every single worker budget and runs when the three pool.

1. The whole model is handed to ONE worker at each real budget. Each must refuse it
   ("buffer budget exceeded"). A worker with a large enough budget must accept it, so the refusals are
   about the budget and not about the model or the worker. Its peak is the model's single-worker need.
2. The three-worker loopback split runs against the pinned reference tokens. Its combined peak
   allocations must exceed the largest single budget while every worker stays inside its own.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

BUDGETS = {'laptop': 768, 'android': 2048, 'iphone': 1536}
CONTROL_MIB = 4096  # the largest budget init_cluster accepts
MIB = 1048576
PROMPT = 'The capital of France is'

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]

def one_worker(bins, suffix, model, budget_mib):
    """Run the whole model on a single worker with this budget."""
    port = free_port()
    worker = subprocess.Popen([str(bins / ('sc-rpc-worker' + suffix)), str(port), str(budget_mib), '2'],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(100):
            try: socket.create_connection(('127.0.0.1', port), 1).close(); break
            except OSError: time.sleep(.1)
        else: raise RuntimeError('worker did not start')
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'probe.json'
            cfg.write_text(json.dumps({'model': str(model.resolve()), 'prompt': PROMPT, 'tokens': 4,
                                       'endpoints': [f'127.0.0.1:{port}'], 'shares': [1]}))
            probe = subprocess.run([str(bins / ('sc-split-probe' + suffix)), str(cfg)], capture_output=True, text=True, timeout=900)
    finally:
        worker.terminate()
        try: output, _ = worker.communicate(timeout=10)
        except subprocess.TimeoutExpired: worker.kill(); output, _ = worker.communicate()
    stats = [json.loads(l[9:]) for l in output.splitlines() if l.startswith('SC_STATS ')]
    return {'budget_mib': budget_mib, 'probe_exit': probe.returncode, 'refused': 'buffer budget exceeded' in output,
            'peak_bytes': max((s['peak_bytes'] for s in stats), default=0)}

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--bin-dir', type=Path, required=True); p.add_argument('--model', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--reference', type=Path, default=Path(__file__).resolve().parents[1] / 'native/split/reference-capacity.json')
    a = p.parse_args(); suffix = '.exe' if os.name == 'nt' else ''
    summary = {'single_worker': {}, 'pooled': {}}

    for node, budget in BUDGETS.items():
        r = one_worker(a.bin_dir, suffix, a.model, budget)
        assert r['probe_exit'] != 0 and r['refused'], (node, r)
        summary['single_worker'][node] = r
        print(f'VERIFIED {node} alone ({budget} MiB) refuses the model', flush=True)

    control = one_worker(a.bin_dir, suffix, a.model, CONTROL_MIB)
    assert control['probe_exit'] == 0 and not control['refused'], control
    need = control['peak_bytes']
    assert need > max(BUDGETS.values()) * MIB, (need, BUDGETS)
    summary['single_worker']['control'] = control
    print(f'VERIFIED one {CONTROL_MIB} MiB worker accepts it; needs {need / MIB:.0f} MiB, more than every real budget', flush=True)

    out = a.out / 'pooled'
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('split_cluster.py')), 'loopback',
                             '--worker-binary', str(a.bin_dir / ('sc-rpc-worker' + suffix)),
                             '--probe-binary', str(a.bin_dir / ('sc-split-probe' + suffix)), '--model', str(a.model),
                             '--out', str(out), '--tokens', '16', '--timeout', '1800', '--reference', str(a.reference)], timeout=3600)
    report = json.loads((out / 'report.json').read_text())
    assert result.returncode == 0 and report['status'] == 'PASS' and report['baseline_source'] == 'pinned-reference', report
    assert report['capacity']['exceeds_largest_worker_budget'], report['capacity']
    for node, w in report['workers'].items():
        assert w['peak_bytes'] <= w['budget_mib'] * MIB, (node, w)
    summary['pooled'] = {'capacity': report['capacity'], 'text': report['generation']['text'],
                         'workers': {n: {'layers': len(w['layers']), 'peak_bytes': w['peak_bytes'], 'budget_mib': w['budget_mib'],
                                         'bytes_to_worker': w['bytes_to_worker']} for n, w in report['workers'].items()}}
    (a.out / 'capacity-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(f"VERIFIED the three budgets pool: combined peak {report['capacity']['combined_peak_bytes'] / MIB:.0f} MiB "
          f"against a {report['capacity']['largest_worker_budget_bytes'] / MIB:.0f} MiB largest budget, tokens equal the pinned reference", flush=True)

if __name__ == '__main__': main()
