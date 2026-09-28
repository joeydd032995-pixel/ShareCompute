#!/usr/bin/env python3
"""Execute the actual iOS app against two native desktop workers on a Mac CI runner."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time

def run(*args): return subprocess.run(args, check=True)
def main():
    p=argparse.ArgumentParser(); p.add_argument('--app', type=Path, required=True)
    p.add_argument('--bin-dir', type=Path, required=True); p.add_argument('--model', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True); a=p.parse_args()
    devices = json.loads(subprocess.check_output(['xcrun','simctl','list','devices','available','--json']))
    phone = next(d for group in devices['devices'].values() for d in group if 'iPhone' in d['name'] and d.get('isAvailable'))
    udid = phone['udid']
    if phone['state'] != 'Booted': run('xcrun','simctl','boot',udid)
    run('xcrun','simctl','bootstatus',udid,'-b')
    run('xcrun','simctl','install',udid,str(a.app.resolve()))
    command = [sys.executable,str(Path(__file__).with_name('split_cluster.py')),'simulator',
               '--worker-binary',str(a.bin_dir/'sc-rpc-worker'),'--probe-binary',str(a.bin_dir/'sc-split-probe'),
               '--model',str(a.model),'--out',str(a.out),'--join-timeout','120','--timeout','300']
    proc = subprocess.Popen(command)
    try:
        deadline = time.monotonic() + 30
        while not (a.out/'pairing-ready').exists():
            if proc.poll() is not None or time.monotonic() > deadline: raise RuntimeError('Coordinator did not start')
            time.sleep(.1)
        pair = (a.out/'pairing/iphone.json').read_bytes()
        env = dict(os.environ, SIMCTL_CHILD_SC_PAIRING_B64=base64.b64encode(pair).decode())
        subprocess.run(['xcrun','simctl','launch','--terminate-running-process',udid,'com.sharecompute.compute-worker'], env=env, check=True)
        if proc.wait(timeout=720) != 0: raise RuntimeError('Native iOS simulator split failed')
        report = json.loads((a.out/'report.json').read_text())
        assert report['status'] == 'PASS' and not report['physical_devices']
        assert report['workers']['iphone']['platform'] == 'ios' and report['workers']['iphone']['simulator']
    finally:
        if proc.poll() is None: proc.terminate(); proc.wait(timeout=10)
        subprocess.run(['xcrun','simctl','terminate',udid,'com.sharecompute.compute-worker'], check=False)

if __name__ == '__main__': main()
