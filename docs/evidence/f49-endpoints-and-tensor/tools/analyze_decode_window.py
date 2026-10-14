"""Per-token bytes and direction switches from a counting-proxy run (F49).

usage: python3 -I analyze_decode_window.py RUN_DIR [LINKS]
RUN_DIR holds probe.err or probe.err.markers (with the SC_T dec_start/dec_end lines written by probe-tp)
and proxyN.json.ts (time, bytes client->server, bytes server->client, turns; 10 Hz).
The window skips the first two decode steps. 'turns' counts direction switches on a
link, so one request/response pair is two turns. Resolution is the proxy's 0.1 s sampling.
"""
import re, sys
D = sys.argv[1]; links = int(sys.argv[2]) if len(sys.argv) > 2 else 3
import os
err = open(D + ("/probe.err" if os.path.exists(D + "/probe.err") else "/probe.err.markers")).read()
st = {int(m.group(1)): float(m.group(2)) for m in re.finditer(r"SC_T dec_start (\d+) ([\d.]+)", err)}
en = {int(m.group(1)): float(m.group(2)) for m in re.finditer(r"SC_T dec_end (\d+) ([\d.]+)", err)}
ks = sorted(set(st) & set(en)); k0, k1 = ks[2], ks[-1]
t0, t1 = st[k0], en[k1]; n = k1 - k0 + 1
print(D, "steps", n, "window s", round(t1 - t0, 2), "s/token", round((t1 - t0) / n, 2))
for i in range(1, links + 1):
    rows = [tuple(map(float, l.split())) for l in open(D + f"/proxy{i}.json.ts")]
    def at(t):
        best = rows[0]
        for r in rows:
            if r[0] <= t: best = r
        return best
    a, b = at(t0), at(t1)
    print(f"  link{i}: c2s {(b[1]-a[1])/n/1e6:.3f} MB/token  s2c {(b[2]-a[2])/n/1e6:.3f} MB/token  turns {(b[3]-a[3])/n:.1f}/token")
