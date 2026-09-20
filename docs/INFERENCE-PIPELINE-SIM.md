# Inference pipeline simulation (Phase A)

Pre-device thesis demo: wire a **shard plan** into an **inference-like data path**
without claiming MLX/Metal works. Fake-but-sized compute proves control-plane +
activation plumbing; Phase B (InferRing / MLX) is where real collectives land.

## What it proves

| Layer | Status in this demo |
|---|---|
| UDP multicast discovery (`239.255.77.77:37777`, `sharecompute-pool`) | **Real** (reused from four-platform demo) |
| TCP JOIN with platform + **role** (`frontend` / `worker`) + usable GB | **Real** multi-process |
| StagePlanner-equivalent layer apportionment + epoch | **Real** (Python mirror of `plan_shards`) |
| Peers **refuse data-plane** until plan epoch matches join ack | **Real** |
| Separate TCP **activation pipeline** along shard ranks | **Real** sockets + framing |
| Frontend (rank 0) owns prompt seed + final token results | **Real** ownership split |
| Buffer budget derived from shard `estimated_gb` | **Real** (demo-scale caps) |
| Missing seat / discovery fail / kill worker mid-run → **exit ≠ 0, no hang** | **Real** hard-fail paths |
| MLX / Metal / InferRing collectives | **Not claimed** — checksum + sleep only |

Honest summary: **discovery, join, plan, and activation plumbing are networked and
fallible**; **compute is fake**. That is enough to validate the Phase A thesis on
one Linux box before Apple hardware / MLX wiring.

## What it does *not* prove

- Real model weights, tokenizer, or sampling
- MLX distributed groups, Metal kernels, or InferRing adapter behavior
- Cross-machine LAN reliability, auth, or reconnect
- Production Bonjour/mDNS discovery (still the Python multicast shape)

## How to run

```bash
# Success — discovery → join → plan → N token steps on frontend → exit 0
python3 scripts/inference_pipeline_demo.py

# Negative — hub does not announce; peers fail discovery → exit 1
python3 scripts/inference_pipeline_demo.py --fail-discovery

# Negative — missing seat (omit Android peer) → exit 1
python3 scripts/inference_pipeline_demo.py --fail-platform android
# equivalent missing-worker helper:
python3 scripts/inference_pipeline_demo.py --omit-worker

# Negative — SIGKILL a worker mid-run → exit 1 within timeout, no hang
python3 scripts/inference_pipeline_demo.py --kill-worker mid
```

Optional knobs: `--timeout`, `--token-count`, `--frontend-platform`,
`--discovery-port`.

### Process roles (normally spawned by the orchestrator)

```bash
python3 scripts/inference_pipeline_demo.py --role hub --port 9876 --timeout 8
python3 scripts/inference_pipeline_demo.py --role peer --platform macos \
  --peer-role frontend --timeout 8
python3 scripts/inference_pipeline_demo.py --role peer --platform android \
  --peer-role worker --timeout 8
```

## Architecture

```
                    UDP beacon 239.255.77.77:37777
                 ┌────────────────────────────────┐
                 │  Hub (control plane TCP)        │
                 │  JOIN(role, usableGB, dataPort) │
                 │  → plan_shards / epoch          │
                 │  → broadcast PLAN               │
                 └────────────────────────────────┘
                        │ PLAN + epoch
        ┌───────────────┼───────────────┬──────────────┐
        ▼               ▼               ▼              ▼
   rank0 frontend   rank1 worker   rank2 worker   rank3 worker
   (prompt/out)     (fake compute) (fake compute) (fake compute)
        │               │               │              │
        └─ TCP act ────►└─ TCP act ────►└─ TCP act ───►│
        ◄──────────── final return (last → frontend) ──┘
```

### Control plane

- Same discovery helpers as `scripts/four_platform_pool_lib.py`
- JOIN carries `role`, `data_host`, `data_port` in addition to platform / usable GB
- Hub forces the configured frontend platform to **rank 0**, workers follow
  sorted platform order for remaining ranks
- PLAN message includes epoch, token count, per-rank layer ranges, estimated GB,
  and data-plane addresses

### Data plane

- **Separate TCP ports** from the control join socket
- Binary frame: magic `SCIN` + epoch hash + `token_id` + `rank_from` / `rank_to` +
  nbytes + epoch string + payload
- Rank `r`: recv from `r-1` → `fake_compute` (SHA-256 fold + sleep × layers) →
  send to `r+1`; last rank returns to frontend
- Peers abort with `pipeline-error` on epoch mismatch, broken pipe, or timeout

## Map to InferRing / MLX (Phase B)

| Phase A (this demo) | Phase B target |
|---|---|
| Python `plan_infer_shards` | `StagePlanner` + `CrossPlatformPool` roster |
| Fake activation TCP pipe | InferRing / MLX collective or host `RingTransport` data path |
| Frontend rank 0 prompt/output | App-side generation owner (Mac) |
| Worker seats (sim profiles) | Real peers with measured `MemoryProfile` |
| `--kill-worker` hard fail | Membership loss → drain / reform (not hang) |
| Unauthenticated localhost | Wire auth + production discovery |

Phase A success criterion: **the shard plan drives a multi-process activation
pipeline that completes on the frontend, and the documented failure modes exit
non-zero without hanging.** Phase B replaces fake compute with InferRing/MLX —
it should not reinvent discovery/join/plan hard-fail semantics.

## Files

| Path | Role |
|---|---|
| `scripts/inference_pipeline_demo.py` | CLI entry (orchestrator default) |
| `scripts/inference_pipeline_orch.py` | Multi-process orchestrator + argparse |
| `scripts/inference_pipeline_hub.py` | Control-plane hub + plan broadcast |
| `scripts/inference_pipeline_peer.py` | Frontend / worker data-plane peers |
| `scripts/inference_pipeline_lib.py` | Framing, planning, fake compute |
| `scripts/four_platform_pool_lib.py` | Shared UDP discovery + TCP line framing |

Related: [`FOUR-PLATFORM-CONNECT.md`](FOUR-PLATFORM-CONNECT.md) (membership seats
without the activation path).
