#!/usr/bin/env python3
"""Real-model pass, post-compute worker death, and insufficient-buffer-budget tests."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

def main():
    p=argparse.ArgumentParser(); p.add_argument('--bin-dir', type=Path, required=True)
    p.add_argument('--model', type=Path, required=True); p.add_argument('--out', type=Path, required=True); a=p.parse_args()
    suffix = '.exe' if os.name == 'nt' else ''
    base = [sys.executable, str(Path(__file__).with_name('split_cluster.py')), 'loopback',
            '--worker-binary', str(a.bin_dir / ('sc-rpc-worker' + suffix)),
            '--probe-binary', str(a.bin_dir / ('sc-split-probe' + suffix)), '--model', str(a.model)]
    for name, args, expected in [('success', ['--tokens','24'], 0),
                                 ('worker-death', ['--tokens','128','--kill-worker','android'], 1),
                                 ('memory-refusal', ['--tokens','8','--budgets','64','64','64'], 1)]:
        out = a.out / name
        result = subprocess.run(base + ['--out', str(out)] + args, timeout=720)
        report = json.loads((out/'report.json').read_text())
        assert result.returncode == expected, (name, result.returncode)
        assert report['status'] == ('PASS' if expected == 0 else 'FAIL'), report
        assert not report['physical_devices']
        if name == 'worker-death': assert report.get('fault_trigger', {}).get('graph_calls_before_kill', 0) > 0, report
        if name == 'memory-refusal':
            logs = '\n'.join(p.read_text() for p in out.glob('*-worker.log'))
            assert 'buffer budget exceeded' in logs, logs[-2000:]
        print(f'VERIFIED {name}', flush=True)

if __name__ == '__main__': main()
