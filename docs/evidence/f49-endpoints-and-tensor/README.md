# Loopback evidence for F49 — more than three endpoints, and tensor split over RPC

**Linux x86-64, 4-core container, loopback only.** No Wi-Fi, no TLS relay, no `split_cluster.py`, no phone.
Nothing here ran on a physical device. The binaries are the repository's own `build/desktop/bin/sc-rpc-worker`
and `sc-split-probe` at the pinned llama.cpp revision `4da6337767f973e2b4d0797e5b323d77d8565e4a`; the two
scratch probes are the repository probe with the small changes in `tools/*.diff`.

| Directory | What it is | Who ran it |
|---|---|---|
| `endpoints/w4`, `w6`, `w15` | 0.5B model, 4/6/15 `sc-rpc-worker` processes, budget 512 MiB each, scratch probe with the endpoint limit raised to 16. `probe.out` carries `SC_RESULT` with `token_ids` | re-run for F49, clean (no output pipe, zero workers left) |
| `endpoints/v16` | the same with 16 workers: **aborts** (`rc.txt`: 134) at `ggml-backend.cpp:1856`, `GGML_ASSERT(n_backends <= GGML_SCHED_MAX_BACKENDS)`; `probe.err` is the whole log | re-run for F49 |
| `endpoints/n6_3B_reference_model` | Qwen2.5-3B Q8_0 across six workers, budgets 768/2048/1536/768/2048/1536. `n6_3B.summary` has the layers per endpoint and each worker's last `SC_STATS`. Tokens equal `native/split/reference-capacity.json` | run by the `linux-backend` worker; the saved output compared by me, the 5-minute run not repeated |
| `tensor/v_layer`, `v_tensor` | 0.5B, three workers, layer versus tensor split, through the counting proxy with no added delay | re-run for F49 |
| `tensor/v3b_layer`, `v3b_tensor` | 3B Q8_0, the same. `proxyN.json` is the proxy's totals for the whole run (loading included); `proxyN.json.ts` is its 10 Hz history (time, bytes to the server, bytes to the client, direction switches); `probe.err.markers` holds the `SC_T` timestamp lines and the device lines | re-run for F49 |

**Reproducing the per-token table in F49:** `python3 -I tools/analyze_decode_window.py tensor/v3b_layer` and
`... tensor/v3b_tensor`. The window skips the first two decode steps. Direction switches count both
directions, so one request and its response are two.

**What is not here.** The emulated-link comparison, the batched-sequence timing, the tensor-mode weight-cache and
allocation figures and the upstream-master claim in F49 were measured by a worker and are **not** reproduced
in this directory; F49 marks each `[worker]`. Neither is anything from the 57- and 212-token or the
12-repeat runs.

**Pitfalls the harness taught.** Piping `run_n.sh` through `head` kills it before its cleanup trap and leaks
worker processes; later runs then silently reuse them. `pkill -f sc-rpc-worker` matches the invoking shell's own
command line; use `pkill -x`. `tools/*` refer to a `$SCRATCH` directory that was this session's scratchpad; set it
to any empty directory and run `tools/run_n.sh NAME PROBE "budget budget ..."` (endpoint runs) or `tools/runp.sh`
(proxy runs) from the repository root.

The model files are not committed; `scripts/download_split_model.py` fetches them.
