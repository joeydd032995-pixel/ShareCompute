#!/usr/bin/env python3
"""Real-model pass, post-compute worker death, insufficient-buffer-budget and weight-cache tests."""
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
            logs = '\n'.join(p.read_text(encoding='utf-8') for p in out.glob('*-worker.log'))
            assert 'buffer budget exceeded' in logs, logs[-2000:]
        print(f'VERIFIED {name}', flush=True)
    verify_cache(base, a.out)

def run_split(base, out, *args):
    result = subprocess.run(base + ['--out', str(out), '--tokens', '16', *args], timeout=720)
    report = json.loads((out / 'report.json').read_text())
    assert result.returncode == 0 and report['status'] == 'PASS', report
    return report['workers']

def verify_cache(base, root):
    """Every run must still match the laptop-only baseline; that is what proves a hit loaded the right bytes."""
    cache = root / 'cache'
    cold = run_split(base, root / 'cache-cold', '--cache-dir', str(cache))
    for node, w in cold.items():
        assert w['cache']['cache_stored_bytes'] > 0 and w['cache']['cache_hit_bytes'] == 0, (node, w)
    print('VERIFIED cache-cold', flush=True)

    warm = run_split(base, root / 'cache-warm', '--cache-dir', str(cache))
    for node, w in warm.items():
        assert w['cache']['cache_hit_bytes'] > 0 and w['cache']['cache_rejected'] == 0, (node, w)
        # Weights dominate the upload; a warm worker should receive well under half of a cold one.
        assert w['bytes_to_worker'] < cold[node]['bytes_to_worker'] / 2, (node, w['bytes_to_worker'], cold[node]['bytes_to_worker'])
    print('VERIFIED cache-warm ' + ', '.join(f"{n} {cold[n]['bytes_to_worker']}->{w['bytes_to_worker']} bytes"
                                            for n, w in warm.items()), flush=True)

    # A flipped byte and a file cut short, as a phone killed mid-write could leave, plus a file
    # with junk appended, which must not spill into the next tensor. All three must be rejected
    # and re-sent; none may reach the model.
    files = sorted((f for f in (cache / 'android').iterdir() if f.is_file()), key=lambda f: f.stat().st_size, reverse=True)
    flipped, truncated, extended = files[0], files[1], files[2]
    data = bytearray(flipped.read_bytes()); data[len(data) // 2] ^= 0xFF; flipped.write_bytes(data)
    with truncated.open('r+b') as f: f.truncate(truncated.stat().st_size // 2)
    with extended.open('ab') as f: f.write(b'\xff' * (1 << 20))
    damaged = run_split(base, root / 'cache-damaged', '--cache-dir', str(cache))
    assert damaged['android']['cache']['cache_rejected'] == 3, damaged['android']
    assert damaged['android']['cache']['cache_stored_bytes'] > 0, damaged['android']
    assert all(damaged[n]['cache']['cache_rejected'] == 0 for n in ('laptop', 'iphone')), damaged
    print('VERIFIED cache-damaged', flush=True)

    healed = run_split(base, root / 'cache-healed', '--cache-dir', str(cache))
    assert all(w['cache']['cache_rejected'] == 0 and w['cache']['cache_stored_bytes'] == 0 for w in healed.values()), healed
    print('VERIFIED cache-healed', flush=True)

if __name__ == '__main__': main()
