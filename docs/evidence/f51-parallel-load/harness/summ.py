import json, sys
S = '/tmp/claude-0/-home-user-ShareCompute/e401796b-35ef-50f7-9cbc-9a4f7927fdf6/scratchpad'
for n in sys.argv[1:]:
    try: r = json.load(open(f'{S}/gate-{n}/report.json'))
    except Exception as e: print(n, 'NO REPORT', e); continue
    ph = {p['name']: p for p in r['phases']['phases']}
    up = {k[7:]: (round(ph[k]['at_ms'] / 1000, 1), round(ph[k]['duration_ms'] / 1000, 1)) for k in ph if k.startswith('upload:')}
    ok = 'Madrid. The capital of Italy is Rome' in r['generation']['text']
    print(f"{n:12s} {r['status']} split_generation={ph['split_generation']['duration_ms']/1000:.1f}s uploads(start,dur)={up} stalls={r['loop_stalls']['count']} text_ok={ok}")
