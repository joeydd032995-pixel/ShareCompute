# Portable dual-topology simulation (Phase B stub)

Pre-device product gate: prove **both** role directions on one portable control and
data plane with `ios` and `windows` seats only. Compute is fake-but-sized: this
simulation is not `llama.cpp`, ggml-rpc, InferRing, or MLX.

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
| Real ggml-rpc / `llama.cpp` / LAN iPhone↔Windows inference | **Not claimed** |

The default demo model is a 32-layer, approximately 12 GB fake model. The planner
reserves 0.5 GB overhead per peer and fits the default advertised capacities:
`ios=6.0 GB` and `windows=16.0 GB`. Fake compute is a checksum fold plus a small
sleep proportional to assigned layers; no model weights or tokenizer are loaded.

## How to run

Run from the repository root. The orchestrator starts one hub and two peer
processes per topology, then cleans them up on success or failure.

```bash
# Success — both topologies (the default; exit 0 only if both pass)
python3 scripts/portable_dual_topology_demo.py
python3 scripts/portable_dual_topology_demo.py --topology both

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

# Automated unit, protocol, happy-path, and failure-matrix gate
python3 scripts/portable_dual_topology_selftest.py
```

For a negative command, a non-zero exit is the expected result. A kill test is
only valid when the combined output contains `SIGKILL-ok:` evidence; a timeout or
silent process disappearance is a failure of the test itself. Use `--timeout` to
bound discovery and pipeline waits, `--token-count N` for a positive token count,
`--discovery-port PORT` to choose the multicast port, and `--usable-gb GB` to
override every peer's advertised capacity. The peer-role `--skip-discovery` mode
is available when invoking the `hub` and `peer` roles directly with `--host` and
`--port`.

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
| `scripts/portable_dual_topology_selftest.py` | Unit checks plus subprocess success/failure matrix |
| `scripts/four_platform_pool_lib.py` | Shared multicast transport and TCP line framing |

The Phase A sibling remains unchanged: [`INFERENCE-PIPELINE-SIM.md`](INFERENCE-PIPELINE-SIM.md).
Do not use this stub as evidence that MLX/Metal, real model weights, authentication,
production discovery, or cross-device reliability work.

## Next: RPC handoff (real `llama.cpp` — not this stub)

Keep the product boundary and test contract: the two seats, both topology rows,
hub role rules, epoch validation, RAM-fit planning, bounded waits, and the
requirement that `--topology both` succeeds only when both directions succeed.

Replace only the fake `SCPT` activation/compute path with the
[`Spikes/llamacpp-rpc`](../Spikes/llamacpp-rpc) implementation. The RPC adapter
must translate a planned shard and activation into the selected `llama.cpp`
server/client request, then report explicit compute and transport status back to
this control plane. A process exiting successfully is not sufficient evidence:
judge the handoff by `llama_decode` / compute status and returned data, with
bounded RPC timeouts and a surfaced worker loss.

The RPC spike documents hazards around request framing, partial writes,
concurrent requests, server readiness, and exit-status ambiguity. Resolve those
hazards before replacing the deterministic stub. This document intentionally does
not claim that RPC, ggml-rpc, `llama.cpp`, InferRing, or MLX is already wired.
See the spike README and `findings.md` (F15, F25–F27, F33–F35) for the next
implementation boundary.
