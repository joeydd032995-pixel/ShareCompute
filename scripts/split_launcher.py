#!/usr/bin/env python3
"""Self-contained ShareCompute test launcher. No user Python installation required."""
import argparse
import asyncio
import base64
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from types import SimpleNamespace
import urllib.request
import webbrowser
import zipfile
import qrcode
import qrcode.image.svg
import split_cluster as cluster
import download_split_model as model

BUNDLE = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))

def default_data_dir():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share'))
    return base / 'ShareCompute'

# The active route is normally the Wi-Fi the phones share. Among the rest, home Wi-Fi is usually
# 192.168/16, and 172.16/12 is where Hyper-V, WSL and Docker put adapters phones cannot reach.
# A subnet alone cannot identify a VPN, so a VPN holding a private route is not demoted; the
# alternatives are always shown instead.
LAN_RANGES = [ipaddress.ip_network(n) for n in ('192.168.0.0/16', '10.0.0.0/8', '172.16.0.0/12')]

def rank_addresses(route, candidates):
    """Private LAN IPv4 addresses, the likeliest Wi-Fi address first."""
    usable = []
    for text in [route, *candidates]:
        try: address = ipaddress.ip_address(text)
        except ValueError: continue
        if address.version == 4 and any(address in n for n in LAN_RANGES) and text not in usable: usable.append(text)
    return sorted(usable, key=lambda x: (x != route, next(i for i, n in enumerate(LAN_RANGES) if ipaddress.ip_address(x) in n)))

def lan_addresses():
    route = ''
    # UDP connect selects a route; it sends no packet to this address.
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(('1.1.1.1', 80)); route = s.getsockname()[0]
    candidates = []
    with contextlib.suppress(OSError):
        candidates += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    return rank_addresses(route, candidates)

def startup_banner(hosts, url):
    lines = ['ShareCompute is running. Keep this window open until the test finishes.', '']
    if hosts:
        lines.append(f'Laptop Wi-Fi address: {hosts[0]}')
        if len(hosts) > 1: lines.append(f'  Other addresses on this laptop: {", ".join(hosts[1:])}')
        lines.append('  Use the address on the same Wi-Fi as both phones (not a VPN or virtual adapter).')
    else:
        lines.append('Laptop Wi-Fi address: not detected. Connect to Wi-Fi, then type its IPv4 address in the dashboard.')
    return '\n'.join(lines + ['', f'Dashboard: {url}'])

def pairing_code(pair):
    return 'sc1.' + base64.urlsafe_b64encode(json.dumps(pair, separators=(',', ':')).encode()).decode().rstrip('=')

@contextlib.contextmanager
def instance_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'launcher.lock').open('a+b') as handle:
        handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RuntimeError('ShareCompute is already open. Use its browser tab or close the other launcher first.') from e
        try: yield
        finally:
            if os.name == 'nt':
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

class TestKit:
    def __init__(self, directory, binaries=None):
        self.directory = directory.resolve(); self.directory.mkdir(parents=True, exist_ok=True)
        self.binaries = binaries or BUNDLE / 'bin'
        self.pair_dir = self.directory / 'pairing'; self.model_path = self.directory / 'model.gguf'
        self.lock = threading.RLock(); self.loop = None; self.task = None; self.thread = None
        self.current_out = None; self.preparing = False
        hosts = lan_addresses()
        self.state = {'phase': 'setup', 'message': 'Check your laptop Wi-Fi address, then press Start.',
                      'host': hosts[0] if hosts else '', 'hosts': hosts,
                      'progress': 0, 'active': False, 'workers': [], 'report': None}
        if (self.pair_dir / 'cluster.json').exists():
            old = json.loads((self.pair_dir / 'cluster.json').read_text())
            # Prefer the current route over yesterday's DHCP address.
            self.state['host'] = self.state['host'] or old['host']

    def snapshot(self):
        with self.lock:
            state = dict(self.state)
            log_path = self.directory/'active.log' if state['active'] else (self.current_out/'coordinator.log' if self.current_out else None)
            if self.current_out and log_path and log_path.exists():
                log = log_path.read_text(encoding='utf-8', errors='replace')
                state['workers'] = [n for n in cluster.NODES if f'Joined {n}:' in log]
                if state['phase'] == 'running':
                    state['message'] = 'Computing on all three devices…' if len(state['workers']) == 3 else 'Scan the QR codes in the phone apps. Keep both apps open.'
            return state

    def start(self, host):
        address = ipaddress.ip_address(host)
        if address.version != 4 or address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError('Choose the laptop IPv4 address used by your Wi-Fi network.')
        with self.lock:
            if self.state['active']: raise ValueError('A test is already active.')
            self.state.update(host=host, active=True, phase='preparing', message='Preparing model and pairing…', report=None, progress=0, workers=[])
            self.current_out = None; self.preparing = True
            self.thread = threading.Thread(target=self.prepare_and_run, args=(host,), daemon=True); self.thread.start()

    def prepare_and_run(self, host):
        try:
            suffix = '.exe' if os.name == 'nt' else ''
            for name in ('sc-rpc-worker', 'sc-split-probe'): cluster.check_binary(self.binaries / (name+suffix))
            if not self.model_path.exists() or model.digest(self.model_path) != model.SHA256:
                partial = self.directory / 'model.partial'; partial.unlink(missing_ok=True)
                digest = hashlib.sha256(); received = 0
                with urllib.request.urlopen(model.URL, timeout=60) as response, partial.open('xb') as f:
                    while chunk := response.read(1024*1024):
                        if not self.preparing: raise InterruptedError('Stopped')
                        f.write(chunk); digest.update(chunk); received += len(chunk)
                        with self.lock:
                            self.state.update(progress=min(100, int(received * 100 / 491400032)),
                                              message=f'Downloading the test model once: {received//1048576} / 469 MiB')
                if digest.hexdigest() != model.SHA256: raise RuntimeError('Model checksum mismatch; start again to retry.')
                partial.replace(self.model_path)
            if not self.preparing: raise InterruptedError('Stopped')
            if not self.pair_dir.exists(): cluster.init_cluster(self.pair_dir, host)
            config = json.loads((self.pair_dir / 'cluster.json').read_text())
            if config['runtime'] != cluster.REV: raise RuntimeError('Pairing uses an older runtime. Remove the pairing folder and scan again.')
            config['host'] = host
            for node, pair in config['nodes'].items():
                pair['host'] = host
                (self.pair_dir / f'{node}.json').write_text(json.dumps(pair), encoding='utf-8')
            (self.pair_dir / 'cluster.json').write_text(json.dumps(config), encoding='utf-8')
            self.current_out = self.directory / 'runs' / (time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2))
            args = SimpleNamespace(mode='coordinator', config=self.pair_dir/'cluster.json', model=self.model_path,
                worker_binary=self.binaries/('sc-rpc-worker'+suffix), probe_binary=self.binaries/('sc-split-probe'+suffix),
                out=self.current_out, tokens=16, prompt=cluster.PROMPT, budgets=[768,2048,1536],
                join_timeout=900, timeout=300, kill_worker=None)
            async def run():
                self.loop = asyncio.get_running_loop(); self.task = asyncio.current_task()
                if not self.preparing: raise asyncio.CancelledError()
                with self.lock: self.state.update(phase='running', progress=100)
                return await cluster.run_coordinator(args)
            self.current_out.parent.mkdir(parents=True, exist_ok=True)
            # Keep the coordinator's terminal output private; expose only a short status.
            log_path = self.directory / 'active.log'
            with log_path.open('w', encoding='utf-8', buffering=1) as log, contextlib.redirect_stdout(log): asyncio.run(run())
            report = json.loads((self.current_out/'report.json').read_text())
            with self.lock:
                self.state.update(phase='done', report=report, message='PASS — all three devices computed their assigned layers.' if report['status']=='PASS' else 'Test failed: '+report.get('error','See report.'))
        except (asyncio.CancelledError, InterruptedError):
            with self.lock: self.state.update(phase='stopped', message='Stopped. Press Start when you are ready.')
        except Exception as e:
            with self.lock: self.state.update(phase='error', message=str(e))
        finally:
            with self.lock:
                if self.current_out and self.current_out.exists() and (self.directory/'active.log').exists():
                    (self.current_out/'coordinator.log').write_bytes((self.directory/'active.log').read_bytes())
                self.state['active'] = False; self.loop = None; self.task = None; self.preparing = False

    def stop(self):
        self.preparing = False
        with self.lock:
            if self.loop and self.task:
                with contextlib.suppress(RuntimeError): self.loop.call_soon_threadsafe(self.task.cancel)

    def pair(self, node):
        if node not in ('android', 'iphone'): raise ValueError('Unknown phone')
        with self.lock:
            if self.state['phase'] != 'running': raise ValueError('Start the test before scanning.')
            return json.loads((self.pair_dir/f'{node}.json').read_text())

    def report_zip(self):
        with self.lock:
            if self.state['active'] or not self.current_out or not self.current_out.exists(): raise ValueError('Finish or stop the test first.')
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
                for path in self.current_out.iterdir():
                    if path.name == 'report.json' or path.suffix == '.log': z.write(path, path.name)
            return buf.getvalue()

PAGE = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ShareCompute</title><style>body{font:17px system-ui;background:#f2f5fa;color:#182635;max-width:900px;margin:40px auto;padding:20px}h1{font-size:36px}section{background:white;border-radius:16px;padding:24px;margin:20px 0;box-shadow:0 4px 20px #1231}button,a.action{font:inherit;padding:12px 22px;background:#175ddc;color:white;border:0;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block;margin:6px}input{font:inherit;padding:10px;max-width:220px}small{display:block;color:#536578;margin:12px 0}.phones{display:flex;gap:24px;flex-wrap:wrap}.phone{flex:1;min-width:240px}img{width:100%;max-width:320px}#message{font-weight:600}progress{width:100%}button:disabled{opacity:.4}summary{cursor:pointer}textarea{width:95%;height:80px}</style>
<h1>ShareCompute</h1><p>One model. Your laptop and both phones.</p>
<section><h2>1. Start on this laptop</h2><p>Keep all three devices on the same Wi-Fi.</p><label>Laptop Wi-Fi address <input id="host" aria-label="Laptop Wi-Fi address" list="hosts" placeholder="e.g. 192.168.1.20" autocomplete="off"></label><datalist id="hosts"></datalist><small id="host-help"></small>
<button id="start" onclick="action('start',{host:document.getElementById('host').value})">Start test</button><button id="stop" onclick="action('stop',{})">Stop</button>
<p id="message"></p><progress id="progress" max="100" value="0"></progress><small>The 469 MiB model downloads once. If Windows asks, allow ShareCompute on your private network.</small></section>
<section><h2>2. Connect the phone apps</h2><p>Install the matching apps from the <a href="phones">phone downloads</a>. On each phone tap <b>Scan laptop QR</b>. Keep the apps open until the test ends.</p>
<div class="phones"><div class="phone"><h3>Android</h3><p id="android-state">Waiting</p><img id="android-qr" alt="Android pairing QR"><details><summary>Copy pairing code instead</summary><textarea id="android-code" readonly></textarea></details></div>
<div class="phone"><h3>iPhone</h3><p id="iphone-state">Waiting</p><img id="iphone-qr" alt="iPhone pairing QR"><details><summary>Copy pairing code instead</summary><textarea id="iphone-code" readonly></textarea></details></div></div>
<small>The iPhone IPA needs signing and Developer Mode before first use. The <a href="https://docs.sidestore.io/docs/installation/prerequisites" target="_blank" rel="noopener">SideStore setup</a> works from Windows or Linux; no owned Mac is required.</small></section>
<section><h2>3. Save the result</h2><p>A PASS requires native computation on all three physical devices and matching output for this 16-token test.</p><a class="action" href="report.zip">Download report</a><button onclick="action('quit',{})">Close launcher</button><small>All project data stays in <span id="data"></span>. No global Python packages or compiler setup.</small></section>
<script>let shown=false; async function action(name,data){try{let r=await fetch(name,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});let j=await r.json();if(!r.ok)alert(j.error);if(name==='quit')document.body.innerHTML='<h1>ShareCompute closed</h1><p>You can close this tab.</p>';}catch(e){alert(e.message)}}
async function poll(){try{let r=await fetch('status');let s=await r.json();if(!shown){document.getElementById('host').value=s.host;let list=document.getElementById('hosts');for(let h of s.hosts){let o=document.createElement('option');o.value=h;list.appendChild(o)}document.getElementById('host-help').textContent=s.hosts.length>1?'Detected automatically. If a phone cannot connect, clear the box to pick another: '+s.hosts.slice(1).join(', '):s.hosts.length?'Detected automatically.':'Not detected. Type the IPv4 address of this laptop on your Wi-Fi.';shown=true}document.getElementById('message').textContent=s.message;document.getElementById('data').textContent=s.data;document.getElementById('progress').value=s.progress;document.getElementById('start').disabled=s.active;document.getElementById('host').disabled=s.active;document.getElementById('stop').disabled=!s.active;
for(let n of ['android','iphone']){document.getElementById(n+'-state').textContent=s.workers.includes(n)?'Connected':'Waiting';let img=document.getElementById(n+'-qr');if(s.phase==='running'&&!img.getAttribute('src')){img.src='qr/'+n;let p=await fetch('pair/'+n);if(p.ok)document.getElementById(n+'-code').value=(await p.json()).code;}if(s.phase!=='running'){img.removeAttribute('src');document.getElementById(n+'-code').value=''}}}catch(e){}setTimeout(poll,1000)}poll();</script>'''

def make_handler(kit, secret):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def route(self):
            expected=f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != expected or not self.path.startswith('/'+secret+'/'): raise ValueError('Invalid launcher session')
            return self.path[len(secret)+2:]
        def reply(self, data, mime='application/json', status=200):
            if not isinstance(data, bytes): data=json.dumps(data).encode()
            self.send_response(status); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','no-referrer'); self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            try:
                route=self.route()
                if route=='': return self.reply(PAGE.encode(),'text/html; charset=utf-8')
                if route=='status': return self.reply(dict(kit.snapshot(),data=str(kit.directory)))
                if route.startswith('pair/'): return self.reply({'code':pairing_code(kit.pair(route[5:]))})
                if route.startswith('qr/'):
                    code=pairing_code(kit.pair(route[3:])); out=io.BytesIO()
                    qrcode.make(code,image_factory=qrcode.image.svg.SvgPathImage,box_size=6,border=4).save(out)
                    return self.reply(out.getvalue(),'image/svg+xml')
                if route=='report.zip': return self.reply(kit.report_zip(),'application/zip')
                if route=='phones':
                    links=json.loads((BUNDLE/'phone-downloads.json').read_text())
                    import html
                    body='<h1>Install the phone apps once</h1><p>Open these matching GitHub downloads on each phone. Sign in to GitHub if asked.</p>'
                    for n,u in links.items(): body+=f'<p><a href="{html.escape(u,quote=True)}">{html.escape(n)}</a></p>'
                    body+='<p>Android: extract the ZIP and install the APK. iPhone: extract the IPA, then sign and install it with SideStore.</p><p><a href="./">Back to test</a></p>'
                    return self.reply(body.encode(),'text/html; charset=utf-8')
                raise ValueError('Unknown page')
            except Exception as e: self.reply({'error':str(e)},status=400)
        def do_POST(self):
            try:
                route=self.route(); origin=self.headers.get('Origin')
                if origin != f'http://127.0.0.1:{self.server.server_port}': raise ValueError('Invalid request origin')
                length=int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 4096: raise ValueError('Invalid request size')
                data=json.loads(self.rfile.read(length))
                if route=='start': kit.start(data['host'])
                elif route in ('stop','quit'):
                    kit.stop()
                    if route=='quit': threading.Thread(target=self.server.shutdown,daemon=True).start()
                else: raise ValueError('Unknown action')
                self.reply({'ok':True})
            except Exception as e: self.reply({'error':str(e)},status=400)
    return Handler

def main():
    p=argparse.ArgumentParser(); p.add_argument('--data-dir',type=Path,default=default_data_dir())
    p.add_argument('--bin-dir',type=Path); p.add_argument('--self-test-model',type=Path); p.add_argument('--no-browser',action='store_true'); a=p.parse_args()
    if a.self_test_model:
        suffix='.exe' if os.name=='nt' else ''; bins=a.bin_dir or BUNDLE/'bin'
        args=SimpleNamespace(mode='loopback',out=a.data_dir/'self-test',model=a.self_test_model,
             worker_binary=bins/('sc-rpc-worker'+suffix),probe_binary=bins/('sc-split-probe'+suffix),
             tokens=16,prompt=cluster.PROMPT,budgets=[768,2048,1536],join_timeout=60,timeout=300,kill_worker=None)
        raise SystemExit(asyncio.run(cluster.run_coordinator(args)))
    with instance_lock(a.data_dir):
        kit=TestKit(a.data_dir,a.bin_dir); secret=secrets.token_urlsafe(24)
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(kit,secret))
        url=f'http://127.0.0.1:{server.server_port}/{secret}/'
        print(startup_banner(kit.state['hosts'],url),flush=True)
        if not a.no_browser: webbrowser.open(url)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally:
            kit.stop(); server.server_close()
            if kit.thread: kit.thread.join(timeout=15)

if __name__=='__main__': main()
