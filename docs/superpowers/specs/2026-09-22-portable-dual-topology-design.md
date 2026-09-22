# Phase B Portable Dual-Topology Design

**Date:** 2026-09-22  
**Status:** Approved in design review; awaiting implementation plan  
**Repo:** `joeydd032995-pixel/ShareCompute`

## Problem

ShareCompute must pool RAM/compute so one device is the **frontend/receiver** and peer seats are **workers**. The product hard requirements are:

1. **iPhone frontend** + **Windows worker**
2. **Windows frontend** + **iPhone worker**

If either topology fails, the project fails. Roles must be swappable on one portable control and data plane.

InferRing/MLX cannot satisfy either topology that includes Windows. The Mac remains a test lab only, not a product frontend.

Phase A (`scripts/inference_pipeline_*`, `docs/INFERENCE-PIPELINE-SIM.md`) proved hub/frontend/worker activation, discovery, planning, and loud fails — but defaults to macOS frontend and seats all four platforms. It does not gate the two product topologies.

## Goals

### First deliverable (this design — stub sim)

- Multi-process portable-protocol **stub** that proves both role directions with hard fails.
- Product seats exactly **ios** and **windows** (Mac/Android out of this harness).
- Default CLI runs **both** topologies and exits 0 only if both succeed.
- Document how to run success and forced-failure cases.

### Explicit non-goals (first deliverable)

- Real ggml-rpc / model weights / llama.cpp binaries.
- Mid-run role flip without restarting the topology.
- InferRing / MLX data plane.
- Auth/TLS on the wire.
- Real Windows PC ↔ iPhone LAN bring-up (comes after real-RPC slice).

### Later slice (handoff — not this PR’s implementation)

Replace the stub data plane with `Spikes/llamacpp-rpc`, keep the same seats, topology matrix, and membership/plan ownership.

## Approach chosen

**Approach 2: New portable dual-topology stub** (not a patch on Phase A).

Rejected alternatives:

1. **Extend Phase A only** — least code, but keeps macOS-default DNA and four-seat semantics; easy to confuse with the lab path.
2. **Live mid-run role swap** — overkill for the first proof; conflicts with known llama.cpp latch/no-reattach limits (F35).

## Architecture

### Deliverables

| Path | Role |
|---|---|
| `scripts/portable_dual_topology_demo.py` | Entry point / CLI |
| `scripts/portable_dual_topology_orch.py` | Spawns hub + peers; aggregates exit codes |
| `scripts/portable_dual_topology_hub.py` | Membership JOIN, role enforcement, plan |
| `scripts/portable_dual_topology_peer.py` | Frontend or worker process |
| `scripts/portable_dual_topology_lib.py` | Shared constants, framing, helpers |
| `docs/PORTABLE-DUAL-TOPOLOGY-SIM.md` | How to run success + fail matrix |

Fork Phase A patterns (`scripts/inference_pipeline_*`, discovery helpers from `scripts/four_platform_pool_lib.py`). Do **not** modify Phase A scripts as the product gate.

### Discovery

- UDP multicast discovery (same transport family as Phase A / four-platform pool).
- Service id: **`sharecompute-portable`** (distinct from `sharecompute-infer` and `sharecompute-pool`).

### Seats

- Exactly two platforms may JOIN: **`ios`** and **`windows`**.
- Hub rejects any other `PlatformKind`.
- Missing either seat fails loud before activation.

### Planes

- **Control:** UDP discovery + hub JOIN/heartbeat/role reservation (Phase A shape).
- **Data:** Portable activation stub over TCP — fake-but-sized compute, framing magic **`SCPT`** (distinct from Phase A `SCIN`). Not MLX. Not real llama.cpp.
- **Core ownership (conceptual):** ShareCompute membership + StagePlanner-style shard assignment remain in charge; this sim mirrors that ownership in Python until Swift Core is wired to a portable runtime adapter.

### Process topology (one topology run)

```
  orch
   ├── hub
   ├── peer (frontend platform, role=frontend, rank 0)
   └── peer (worker platform, role=worker, rank 1)
```

For `--topology both`, orch runs topology A to completion, tears down, then runs topology B with a fresh hub/peer set.

## Roles and CLI

### Roles (per topology run)

- Exactly one **frontend** and one **worker**.
- **Frontend:** JOIN as `frontend`; forced rank 0; seeds prompt; collects final tokens; sends `done`.
- **Worker:** JOIN as `worker`; receives from rank−1; fake-compute; sends to rank+1 (last hop returns to frontend).
- Hub rejects: wrong role for the chosen topology, two frontends, missing seat, or non-product platform.

### Topology mapping

| `--topology` value | Frontend | Worker |
|---|---|---|
| `iphone-frontend` | ios | windows |
| `windows-frontend` | windows | ios |
| `both` (default) | run both rows in sequence | |

CLI uses product language `iphone-frontend`; wire/platform enum stays **`ios`** to match `PlatformKind`.

### Entry point

```bash
python3 scripts/portable_dual_topology_demo.py
# equivalent to --topology both

python3 scripts/portable_dual_topology_demo.py --topology iphone-frontend
python3 scripts/portable_dual_topology_demo.py --topology windows-frontend
```

### Orchestrator behavior

- Spawns hub + two peers per topology run.
- Sets each peer’s `--platform` and `--peer-role` from the table above.
- Aggregates exit codes: for `both`, overall exit **0** only if **both** topology runs exit 0.
- Useful knobs (Phase A-compatible where practical): `--token-count`, `--discovery-port`, `--skip-discovery`, `--usable-gb`.

## Fail modes

Loud fails: exit ≠ 0, no hang. Forced failures must work on both topologies when `--topology both` (or on the single topology under test).

### Required negative cases

1. `--fail-discovery` — peer never joins; orch times out; exit 1.
2. `--fail-platform` / omit one seat — missing ios or windows; refuse to plan; exit 1.
3. `--kill-worker mid` — SIGKILL worker mid-pipeline; detect disconnect; exit 1 (no silent success).
4. `--kill-worker start` — worker dies before activation completes; exit 1.
5. `--usable-gb` too small — plan unfit / over-memory reject; exit 1.
6. **Role mismatch** — peer JOINs with wrong role for the topology; hub rejects; exit 1.

### Positive cases

- Happy path each topology: exit 0; logs show frontend platform, worker platform, shard ranks, token completion.
- `--topology both`: both happy paths back-to-back; exit 0 only if both succeeded.

### Evidence rules

- Kill cases must leave evidence in logs (signal / disconnect), not rely on peer process exit alone.
- Fake compute remains sized from `estimated_gb` / buffer budget so plan rejects are meaningful.

### Out of scope for this stub

llama.cpp latch, corrupted-token-on-peer-death, and false `/health` (F33–F35) belong to the real-RPC slice, not this sim.

## Handoff to real llama.cpp RPC

After the stub gate is green:

**Keep:** two seats, topology matrix, hub role rules, ShareCompute membership/plan ownership, both-topologies success criterion.

**Replace:** `SCPT` fake activation with `Spikes/llamacpp-rpc` (ggml-rpc servers + client).

**Rules for the real-RPC slice:**

- Frontend process = llama client (decode / session owner); worker = rpc server holding assigned layers.
- Judge success from `llama_decode` / compute status — **never** trust process exit alone (F33).
- Adopt peer-death handling aligned with upstream `#26724`; treat latch/no-reattach (F35) as a known limit — **restart the topology run** rather than claiming in-process re-attach until clear/reset exists.
- No auth/TLS until after a controlled LAN proof (F15); PoC network only.

**Acceptance for real-RPC (not this stub PR):**

1. Same-host two-process: both topologies succeed and kill-mid fails loudly.
2. Then real Windows PC ↔ iPhone on LAN when both devices are available.
3. Mac remains optional lab only.

**Doc boundary:** stub PR owns `docs/PORTABLE-DUAL-TOPOLOGY-SIM.md`. Spike README + `findings.md` remain source of truth for RPC hazards; the sim doc’s “Next” section links them.

## Relationship to existing code

| Existing | Relationship |
|---|---|
| `scripts/inference_pipeline_*` | Pattern source only; remains four-platform / Phase A lab |
| `docs/INFERENCE-PIPELINE-SIM.md` | Sibling doc; not replaced |
| `scripts/four_platform_pool_*` | Discovery helpers may be reused |
| `Sources/ShareComputeCore/*` | Conceptual control plane; no Swift change required for stub sim |
| `Spikes/llamacpp-rpc/` | Next data plane; not invoked by stub |
| `findings.md` (F15, F25–F27, F33–F35) | Constraints on real-RPC handoff |

## Success criteria (stub deliverable)

1. `python3 scripts/portable_dual_topology_demo.py` (default `both`) exits 0 with both topologies logged.
2. Each forced-failure flag above exits ≠ 0 with no hang.
3. `docs/PORTABLE-DUAL-TOPOLOGY-SIM.md` documents success + fail commands.
4. Phase A demos remain unchanged and still pass.

## Open implementation notes

- Prefer copying Phase A’s timeout and kill-evidence patterns rather than inventing new ones.
- Keep service id and TCP magic distinct so Phase A and Phase B can run on the same LAN without cross-talk.
- Implementation plan should use TDD against the exit-code matrix above.
