# Portable dual-topology simulation (stub and same-host RPC gate)

Pre-device product gate: prove **both** role directions on one portable control and
data plane with `ios` and `windows` seats only. The default backend is a
fake-but-sized stub; the opt-in `llamacpp-rpc` backend is a same-host gate for
the probed `llama.cpp` RPC binaries. Neither mode claims LAN iPhone↔Windows
inference.

## What it proves

| Layer | Status in this demo |
|---|---|
| UDP multicast discovery (`239.255.77.77:37777`, service `sharecompute-portable`) | **Real** |
| TCP JOIN with platform, role (`frontend` / `worker`), usable GB, and data port | **Real** |
| Product seats gated to `ios` + `windows` (Mac/Android rejected) | **Real** |
| `iphone-frontend` topology (`ios` FE + `windows` worker) | **Real** |
| `windows-frontend` topology (`windows` FE + `ios` worker) | **Real** |
| Default `--topology both` runs both rows and exits 0 only when both succeed | **Real** |
| StagePlanner-equivalent shard plan, epoch, and RAM-fit rejection | **Real** |
| TCP activation framing with magic `SCPT` along shard ranks | **Real** |
| Missing seat, discovery failure, role mismatch, and killed worker exit non-zero | **Real** |
| Same-host `llamacpp-rpc` client + one worker endpoint (opt-in) | **Real when the probe passes** |
| LAN iPhone↔Windows inference | **Not claimed** |

With `--backend stub` (the default), the demo model is a 32-layer, approximately
12 GB fake model. The planner reserves 0.5 GB overhead per peer and fits the
default advertised capacities: `ios=6.0 GB` and `windows=16.0 GB`. Stub compute
is a checksum fold plus a small sleep proportional to assigned layers; no model
weights or tokenizer are loaded. The RPC backend instead requires the BIN/model
environment described below and runs one loopback RPC worker endpoint.

## Backends and same-host RPC boundary

The CLI accepts `--backend stub` (the default) or `--backend llamacpp-rpc`.
`stub` keeps the deterministic SCPT fake activation path and needs no llama.cpp
installation. `llamacpp-rpc` replaces that data-plane step with one
`ggml-rpc-server` worker endpoint and a llama.cpp client, while retaining the same
seat, topology, plan, and bounded-wait checks.

The RPC gate is **same-host only**: the orchestrator, client, and RPC worker run on
the same host and the worker endpoint is bound to loopback. It has **no
authentication or TLS**; do not expose it beyond the host. Each topology run uses
one worker RPC endpoint, not a multi-worker or LAN deployment.

Before selecting `llamacpp-rpc`:

- Set `SHARECOMPUTE_LLAMA_BIN` (preferred) or `BIN` to a llama.cpp build directory
  containing an executable `ggml-rpc-server` and a client (`llama-server` or
  `llama-cli`).
- Set `SHARECOMPUTE_LLAMA_MODEL` (preferred) or `MODEL` to the GGUF model path.
- The build must include the peer-death handling from `ggml-org/llama.cpp#26724`
  (or an equivalent landed patch). The capability probe requires the marker
  `the device is now unusable`; a missing marker or missing executable fails the
  RPC run. There is no silent fallback to `stub`.

Kill and restart policy differs by backend. For `stub`, `--kill-worker start|mid`
kills the simulated worker; the run must exit non-zero and does not restart. For
`llamacpp-rpc`, the same options SIGKILL `ggml-rpc-server` and must emit
`SIGKILL-ok:rpc-server:` evidence. The failed attempt is torn down and the full
same-host topology is restarted once with fresh processes and ports. A successful
retry completes that topology; a second failure exits non-zero. This is a full
restart, not in-process RPC peer re-attachment.

## How to run

Run from the repository root. The orchestrator starts one hub and two peer
processes per topology, then cleans them up on success or failure. The explicit
`--backend stub` command below is equivalent to the default when the flag is
omitted.

```bash
# Stub backend — both topologies (default backend; exit 0 only if both pass)
python3 scripts/portable_dual_topology_demo.py --backend stub
python3 scripts/portable_dual_topology_demo.py --backend stub --topology both

# Same-host RPC backend — requires a probe-passing #26724 build and GGUF
SHARECOMPUTE_LLAMA_BIN=/path/to/bin \
SHARECOMPUTE_LLAMA_MODEL=/path/to/qwen2.5-0.5b-instruct-q4_k_m.gguf \
  python3 scripts/portable_dual_topology_demo.py --backend llamacpp-rpc --topology both

# Success — single topology (exit 0)
python3 scripts/portable_dual_topology_demo.py --topology iphone-frontend
python3 scripts/portable_dual_topology_demo.py --topology windows-frontend

# Negative — hub does not announce; peers fail sharecompute-portable discovery
python3 scripts/portable_dual_topology_demo.py --fail-discovery

# Negative — omit a required product seat; exit != 0, without hanging
python3 scripts/portable_dual_topology_demo.py --fail-platform ios
python3 scripts/portable_dual_topology_demo.py --fail-platform windows

# Negative — SIGKILL the worker at startup or during the activation pipeline
# Each run must exit != 0 and include SIGKILL-ok:<platform>:pid=...:exit=...
python3 scripts/portable_dual_topology_demo.py --kill-worker mid
python3 scripts/portable_dual_topology_demo.py --kill-worker start

# Negative — advertised RAM cannot fit the model; shard plan is rejected
python3 scripts/portable_dual_topology_demo.py --usable-gb 0.1

# Negative — peers JOIN with frontend/worker roles swapped; hub rejects the run
python3 scripts/portable_dual_topology_demo.py --fail-role-mismatch

# Argument error — token count must be positive; exit != 0 before spawning peers
python3 scripts/portable_dual_topology_demo.py --token-count -1

# Stub unit, protocol, happy-path, and failure-matrix gate
python3 scripts/portable_dual_topology_selftest.py

# RPC unit checks always run; RPC subprocess matrix runs when BIN + MODEL are set
python3 scripts/portable_rpc_selftest.py
```

For a negative command, a non-zero exit is the expected result. A kill test is
only valid when the combined output contains `SIGKILL-ok:` evidence; a timeout or
silent process disappearance is a failure of the test itself. Use `--timeout` to
bound discovery and pipeline waits, `--token-count N` for a positive token count,
`--discovery-port PORT` to choose the multicast port, and `--usable-gb GB` to
override every peer's advertised capacity. The peer-role `--skip-discovery` mode is available when invoking the `hub` and
`peer` roles directly with `--host` and `--port`. The RPC selftest prints a
`SKIP` for its BIN-gated matrix when the required environment is absent; that
does not skip its always-on unit checks.

## Architecture

```text
                         UDP multicast beacon
                      239.255.77.77:37777
                    service=sharecompute-portable
                                  |
             +--------------------v--------------------+
             | orchestrator (A, then B for --topology both) |
             | starts hub + frontend peer + worker peer     |
             +--------------------+--------------------+
                                  |
                 TCP control: JOIN -> PLAN(epoch) -> done
                                  |
             +--------------------v--------------------+
             | hub: role validation + shard planning       |
             | frontend is rank 0; worker is rank 1         |
             +--------------------+--------------------+
                                  |
                    SCPT activation data plane
                     frontend -> worker -> frontend
```

| `--topology` | Frontend seat / rank 0 | Worker seat / rank 1 |
|---|---|---|
| `iphone-frontend` | wire `ios` (display: iPhone) | wire `windows` |
| `windows-frontend` | wire `windows` | wire `ios` |
| `both` (default) | runs both rows sequentially | runs both rows sequentially |

The CLI uses product language `iphone-frontend`; the wire/platform enum remains
`ios`. Every run has exactly one frontend and one worker. The hub emits a fresh
epoch in its beacon and sends a PLAN containing token count, layer ranges, RAM
estimates, and each peer's data-plane address. Peers accept data only when the
PLAN epoch equals their JOIN epoch.

### Control plane

1. The hub binds a TCP control socket and periodically announces a JSON beacon on
   `239.255.77.77:37777`. Peers ignore beacons for `sharecompute-infer` and other
   service IDs; only `sharecompute-portable` is accepted.
2. Each peer opens a separate data listener, discovers the hub, and sends a TCP
   JOIN containing `platform`, `role`, `usable_gb`, `node_id`, and data address.
3. The hub admits only `ios` and `windows`, reserves the configured frontend seat,
   requires one frontend plus one worker, then plans 32 layers across the two
   advertised capacities. A plan that cannot fit approximately 12 GB after
   overhead is rejected.
4. The hub broadcasts the epoch-tagged PLAN. The frontend owns the prompt seed
   and final result; the worker supplies the second shard.

### Data plane

The activation path uses a separate TCP connection from control and is framed
with `SCPT`, not Phase A's `SCIN`. Each frame carries an epoch hash plus epoch
string, token ID, `rank_from`, `rank_to`, payload size, and payload. Receivers
reject bad magic, epoch mismatches, unexpected rank edges, oversized payloads, and
broken or timed-out channels. The frontend sends a fake activation to rank 1;
the worker computes and returns the final activation to rank 0. The hub reports
`done` only from the configured frontend and converts peer errors or disconnects
to a non-zero run result.

## Files

| Path | Role |
|---|---|
| `scripts/portable_dual_topology_demo.py` | Public CLI entry point |
| `scripts/portable_dual_topology_orch.py` | Multi-process orchestration, topology selection, failure injection, exit aggregation |
| `scripts/portable_dual_topology_hub.py` | UDP beacon, TCP JOIN validation, shard plan, PLAN broadcast, completion/error handling |
| `scripts/portable_dual_topology_peer.py` | Frontend and worker peers; SCPT activation path |
| `scripts/portable_dual_topology_lib.py` | Constants, discovery, planning, framing, fake compute |
| `scripts/portable_dual_topology_selftest.py` | Stub unit checks plus subprocess success/failure matrix |
| `scripts/portable_rpc_selftest.py` | RPC unit checks plus BIN-gated same-host matrix |
| `scripts/four_platform_pool_lib.py` | Shared multicast transport and TCP line framing |

The Phase A sibling remains unchanged: [`INFERENCE-PIPELINE-SIM.md`](INFERENCE-PIPELINE-SIM.md).
Do not use this stub as evidence that MLX/Metal, real model weights, authentication,
production discovery, or cross-device reliability work.

## Next: LAN Windows↔iPhone after this gate

This same-host RPC gate keeps the existing two-seat control-plane contract while
checking the selected llama.cpp RPC path, its `#26724` probe, and kill/restart
handling. The next topology is LAN Windows↔iPhone after this gate. LAN transport,
remote discovery, and device-to-device security are not covered here.
