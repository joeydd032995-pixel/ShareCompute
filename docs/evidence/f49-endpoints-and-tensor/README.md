# Loopback evidence for F49 — more than three endpoints, and tensor split over RPC

**Linux x86-64, 4-core container, loopback only.** No Wi-Fi, no TLS relay, no `split_cluster.py`, no phone.
Nothing here ran on a physical device. The binaries are the repository's own `build/desktop/bin/sc-rpc-worker`
and `sc-split-probe` at the pinned llama.cpp revision `4da6337767f973e2b4d0797e5b323d77d8565e4a`. The scratch
probe is the repository probe plus `tools/probe-f49.diff`: the endpoint limit raised from 3 to 16 (so the 16th
endpoint can show the scheduler's own limit), `split_mode` from the config (`layer` or `tensor`, with flash
attention forced for tensor), `tensor_split` padded with zeros to `llama_max_devices()` entries
(`pad_shares`, default true; `false` reproduces the repository probe's unpadded read, see F49 section 2), and
timestamp marks around each decode step.

## Reproducing

```
export SCRATCH=/some/empty/dir
tools/build_probes.sh                                    # needs build/desktop; writes $SCRATCH/bin/probe-f49
tools/run_n.sh NAME $SCRATCH/bin/probe-f49 "512 512 512 512" 150   # N workers, budgets in MiB, no proxy
tools/runp.sh NAME MODEL MODE 3 "768,2048,1536" "768,2048,1536" 16 # layer or tensor, through counting proxies
python3 -I tools/analyze_decode_window.py RUN_DIR                  # per-token bytes and direction switches
```
`BASEPORT`, `BUILD`, `MODEL`, `PAD` and `TOKENS` are environment variables (see each script's header). Both
harnesses refuse to start if a port is already taken and fail if a worker dies at start; they take every path
from their own location and `$SCRATCH`, not from a particular machine. Model files are not committed;
`scripts/download_split_model.py` fetches them.

## What is here

| Directory | What it is |
|---|---|
| `endpoints/e4`, `e6`, `e15` | 0.5B, 4/6/15 workers, 512 MiB each. `probe.out` carries `SC_RESULT` with `token_ids`, which equal the single-process reference |
| `endpoints/e16` | the same with 16 workers: **aborts** (`rc.txt`: 134) at `ggml-backend.cpp:1856`, `GGML_ASSERT(n_backends <= GGML_SCHED_MAX_BACKENDS)`; `probe.err` is the whole log |
| `endpoints/e6_3b` | Qwen2.5-3B Q8_0 across six workers, budgets 768/2048/1536/768/2048/1536; tokens equal `native/split/reference-capacity.json`; `worker_last_stats.txt` has each worker's last `SC_STATS` |
| `endpoints/c3pad`, `c3unpad` | the control for the unpadded `tensor_split` read: three workers, 0.5B, padded and unpadded; byte-identical peaks (F37's figures) and equal tokens |
| `tensor/f_layer05`, `f_tensor05` | 0.5B, three workers, layer versus tensor split, through the counting proxy with no added delay |
| `tensor/f_layer3b`, `f_tensor3b`, `f_tensor3b_unpad` | 3B Q8_0, the same, tensor also with the unpadded read. `proxyN.json` is a proxy's totals for the whole run (loading included); `proxyN.json.ts` is its 10 Hz history (time, bytes to the server, bytes to the client, direction switches per connection, and the older all-connections count); `probe.err.markers` holds the `SC_T` timestamps |
| `unpadded_first_runs/` | the first runs, made with the first probe (unpadded `shares`) and the first proxy (one last-direction for all connections together), including the 3B six-worker run by the `linux-backend` worker. They agree with the runs above; the old proxy files have no `turns_global` column and `analyze_decode_window.py` reads both. `unpadded_first_runs/tools/` holds the two first scratch probes as diffs (`probe-n.diff`: limit raised; `probe-tp.diff`: tensor mode and timestamp marks), superseded by `tools/probe-f49.diff` |


## What changed after review

Codex's review of the first version found four problems in the tools, each real: the proxy kept one last-direction
for all connections (now per connection, with the old count kept as `turns_global`; in the decode window the two
are identical, so the first figures stand); the harness accepted a leaked worker already bound to its port
(now refused); `runp.sh` pointed at a scratch-only proxy path (now the committed `tools/proxy.py`); and the
scratch probes passed an N-float `shares` to a call that reads 16 (now padded, with the control above). The
endpoint, layer and tensor runs were then repeated with the fixed tools and are the ones recorded in F49.

## What is not here

The emulated-link comparison, the batched-sequence timing, the tensor-mode weight-cache and allocation figures and
the upstream-master claim in F49 were measured by a worker and are **not** reproduced in this directory; F49 marks
each `[worker]`. Neither is anything from the 57- and 212-token or the 12-repeat runs.

**Pitfalls the harness taught.** Piping `run_n.sh` through `head` kills it before its cleanup trap and leaks worker
processes. `pkill -f sc-rpc-worker` matches the invoking shell's own command line; use `pkill -x`.
