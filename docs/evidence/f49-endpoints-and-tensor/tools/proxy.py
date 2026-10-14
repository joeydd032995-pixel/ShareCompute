#!/usr/bin/env python3
"""Counting + link-emulating TCP proxy (scratch). usage: proxy.py LISTEN TARGET STATS [owd_ms] [mbit_per_s]
Per direction: a chunk read at time t is delivered at max(t, link_free) + len/bw + owd (pipelined, in order).
Stats: bytes each way, direction switches ('turns', per connection; 'turns_global' is the older all-connections count)."""
import asyncio, json, sys, time, signal, os
listen, target, stats_path = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
owd = float(sys.argv[4]) / 1000 if len(sys.argv) > 4 else 0.0
bw = float(sys.argv[5]) * 1e6 / 8 if len(sys.argv) > 5 and float(sys.argv[5]) > 0 else 0.0
st = {"c2s": 0, "s2c": 0, "turns": 0, "turns_global": 0, "conns": 0, "last_dir": None, "msgs_c2s": 0, "msgs_s2c": 0}
def dump():
    json.dump(st, open(stats_path, "w"))
async def pipe(r, w, direction, conn):
    q = asyncio.Queue()
    free = 0.0
    async def writer():
        while True:
            item = await q.get()
            if item is None:
                break
            t, data = item
            d = t - time.monotonic()
            if d > 0:
                await asyncio.sleep(d)
            w.write(data); await w.drain()
        try: w.close()
        except Exception: pass
    wt = asyncio.create_task(writer())
    try:
        while True:
            data = await r.read(1 << 20)
            if not data:
                break
            now = time.monotonic()
            st[direction] += len(data); st["msgs_" + direction] += 1
            # turns: direction switches counted per TCP connection (a request and its response are two).
            # turns_global: the first version's count, one last-direction for every connection together.
            if conn["last"] is not None and conn["last"] != direction:
                st["turns"] += 1
            conn["last"] = direction
            if st["last_dir"] != direction:
                st["turns_global"] += 1; st["last_dir"] = direction
            if os.path.exists(stats_path + '.arm'):
                start = max(now, free)
                free = start + (len(data) / bw if bw else 0.0)
                await q.put((free + owd, data))
            else:
                await q.put((now, data))
    finally:
        await q.put(None)
        await wt
        dump()
async def handle(cr, cw):
    st["conns"] += 1
    sr, sw = await asyncio.open_connection("127.0.0.1", target)
    conn = {"last": None}
    await asyncio.gather(pipe(cr, sw, "c2s", conn), pipe(sr, cw, "s2c", conn))
async def main():
    srv = await asyncio.start_server(handle, "127.0.0.1", listen)
    hist = open(stats_path + ".ts", "w")
    async def periodic():
        while True:
            await asyncio.sleep(0.1); dump()
            hist.write("%.3f %d %d %d %d\n" % (time.time(), st["c2s"], st["s2c"], st["turns"], st["turns_global"])); hist.flush()
    asyncio.create_task(periodic())
    async with srv:
        await srv.serve_forever()
asyncio.run(main())
