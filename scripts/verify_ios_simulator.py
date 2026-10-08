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

def run(*args, timeout=180):
    print(' '.join(args), flush=True)
    return subprocess.run(args, check=True, timeout=timeout)

def boot(udid, booted, attempts=2):
    """Boot the simulator, erasing it and trying again if it never finishes booting.

    Hosted macOS runners sometimes hang a first boot in a system migration (CoreLocationMigrator
    was seen on PR #28, and the main-branch run for PR #31 timed out the same way). Nothing of
    ours has run at that point, and a factory-reset device boots cleanly.
    """
    for attempt in range(1, attempts + 1):
        try:
            if not booted: run('xcrun','simctl','boot',udid)
            run('xcrun','simctl','bootstatus',udid,'-b', timeout=300)
            return
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            if attempt == attempts: raise
            print(f'Simulator boot attempt {attempt} failed ({type(e).__name__}); erasing and retrying', flush=True)
            subprocess.run(['xcrun','simctl','shutdown',udid], check=False, timeout=120)
            run('xcrun','simctl','erase',udid, timeout=300)
            booted = False
def main():
    p=argparse.ArgumentParser(); p.add_argument('--app', type=Path, required=True)
    p.add_argument('--bin-dir', type=Path, required=True); p.add_argument('--model', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True); a=p.parse_args()
    devices = json.loads(subprocess.check_output(['xcrun','simctl','list','devices','available','--json']))
    phone = next(d for group in devices['devices'].values() for d in group if 'iPhone' in d['name'] and d.get('isAvailable'))
    udid = phone['udid']
    boot(udid, phone['state'] == 'Booted')
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
        # Exercise exactly the QR payload decoder used on physical phones.
        code = b'sc1.' + base64.urlsafe_b64encode(pair).rstrip(b'=')
        env = dict(os.environ, SIMCTL_CHILD_SC_PAIRING_B64=base64.b64encode(code).decode())
        with (a.out/'ios-app.log').open('wb') as log:
            app = subprocess.Popen(['xcrun','simctl','launch','--console','--terminate-running-process',udid,
                                    'com.sharecompute.compute-worker'], env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                if proc.wait(timeout=720) != 0: raise RuntimeError('Native iOS simulator split failed')
                report = json.loads((a.out/'report.json').read_text())
                assert report['status'] == 'PASS' and not report['physical_devices']
                assert report['workers']['iphone']['platform'] == 'ios' and report['workers']['iphone']['simulator']
            finally:
                if app.poll() is None: app.terminate(); app.wait(timeout=10)
    finally:
        if proc.poll() is None: proc.terminate(); proc.wait(timeout=10)
        subprocess.run(['xcrun','simctl','terminate',udid,'com.sharecompute.compute-worker'], check=False)

if __name__ == '__main__': main()
