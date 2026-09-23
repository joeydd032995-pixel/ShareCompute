# Same-Host llama.cpp RPC Gate — Design

**Date:** 2026-09-22  
**Status:** Approved in section-by-section design review; awaiting written-spec sign-off, then implementation plan  
**Repo:** `joeydd032995-pixel/ShareCompute`  
**Parent:** Phase B stub (`docs/superpowers/specs/2026-09-22-portable-dual-topology-design.md`, merged as PR #21 / `3786a3e`)

## Problem

Phase B proved both product topologies with a **stub** data plane (`SCPT` fake activation):

1. **iPhone frontend** + **Windows worker** (`iphone-frontend`)
2. **Windows frontend** + **iPhone worker** (`windows-frontend`)

The next hard gate is the same product contract with a **real** llama.cpp RPC data plane — still same-host first — before any Windows↔iPhone LAN bring-up.

Spike evidence (`Spikes/llamacpp-rpc/`, findings F15 / F25 / F27 / F33–F35) shows:

- Without `#26724`, peer kill aborts the client process (`RPC_STATUS_ASSERT` → `GGML_ABORT`).
- With `#26724`, failure is catchable (`llama_decode` → `-3` / HTTP 500), but **one corrupted token** can be emitted, `llama-cli` can still exit 0, and `/health` can stay `ok` while compute is dead.
- Latch is insert-only: a restarted peer on the same address is **not** reattached (F35). Recovery = **restart the topology** (new processes, new ports).
- Prefer at most **two** RPC endpoints for this gate (`#28487` deadlock risk with 3+).
- No auth/TLS on `ggml-rpc-server` (F15) — PoC / loopback only for this slice.

## Goals

### This design (same-host RPC gate)

- Extend the portable dual-topology harness with a **backend adapter**: `stub` | `llamacpp-rpc`.
- Keep seats, topology matrix, hub role rules, and `--topology both` success criterion.
- Replace the SCPT fake compute path with real `ggml-rpc-server` + client when backend is `llamacpp-rpc`.
- Require a `#26724`-capable BIN; judge success from decode/compute status, never process exit alone.
- On peer loss: tear down and **restart topology** (one automatic retry, then fail).
- Default backend remains `stub` so CI stays green without llama binaries.

### Explicit non-goals (this gate)

- Real Windows PC ↔ iPhone LAN.
- Swift Core / iOS-on-device `GGML_RPC=ON`.
- Auth/TLS on the RPC wire.
- Latch reattach / hot peer swap without full topology restart.
- Three or more RPC backends in one run.
- Thin `run.sh` wrapper as the product path; Swift Core rewrite as the first path.

### Later plan (after this gate is green)

Windows↔iPhone LAN with firewall/bind rules from the spike README; then Core adapter work.

## Approach chosen

**Approach 2: Portable orch + RPC backend adapter**

Rejected alternatives:

1. **Thin wrapper around `Spikes/llamacpp-rpc/run.sh`** — fastest demo, but does not keep seats/topology/hub as the control plane or teach the product CLI.
2. **Swift Core + RPC first** — correct long-term ownership, but blocks the same-host gate on unproven iOS `GGML_RPC` and Core wiring.

## Architecture

### Planes

| Plane | Owner | Behavior |
|---|---|---|
| Control | Existing portable hub/peers (`sharecompute-portable`) | Discovery, JOIN, role enforcement, shard plan, epoch — **unchanged contract** |
| Data (stub) | SCPT activation path | Default; CI / no-BIN |
| Data (RPC) | Backend adapter | Starts `ggml-rpc-server` + client; maps plan layer ranges to RPC; reports compute status to orch |

Control plane still assigns frontend vs worker seats. The adapter swaps **data plane only**.

### Backend flag

```bash
python3 scripts/portable_dual_topology_demo.py --backend stub          # default
python3 scripts/portable_dual_topology_demo.py --backend llamacpp-rpc   # requires BIN
```

### Process topology (one `llamacpp-rpc` topology run, same host)

```text
  orch (+ RPC adapter)
   ├── hub                          # control plane
   ├── peer (frontend seat)         # role/metadata; client is owned by adapter
   ├── peer (worker seat)           # role/metadata
   ├── ggml-rpc-server              # worker compute (loopback ephemeral port)
   └── llama client                 # frontend compute (holds GGUF; talks RPC)
```

Exact process parenting (adapter as child of orch vs peer) is an implementation detail; the **role contract** is fixed below.

### Roles / topology → RPC mapping

Seats remain **`ios` + `windows` only**. CLI keeps product language `iphone-frontend`; wire enum stays `ios`.

| `--topology` | Frontend seat | Worker seat | Client (holds GGUF) | RPC backend |
|---|---|---|---|---|
| `iphone-frontend` | ios | windows | client bound to ios / frontend role | `ggml-rpc-server` as windows worker |
| `windows-frontend` | windows | ios | client bound to windows / frontend role | `ggml-rpc-server` as ios worker |
| `both` (default) | run both rows | | exit 0 only if both succeed | |

**Who starts what**

- Adapter starts **one** `ggml-rpc-server` per worker seat on an ephemeral loopback port, then **one** client pointed at that endpoint with model + layer split from the plan.
- Frontend seat = client owner (prompt / decode / status).
- Worker seat = RPC server only (no local GGUF required on worker for this gate).
- At most **two** RPC endpoints per topology run (one worker). Spike T1 used two servers for a 50-layer split; this gate uses **one worker endpoint** aligned to the two-seat product matrix unless the plan explicitly documents a second local device — prefer one remote RPC device + local layers on the client if the client supports it, or a single RPC server holding the assigned worker layers. Implementation plan will pin the exact `--rpc` / device string from a spike-compatible recipe that still maps 1:1 to frontend/worker seats.

### Deliverables (expected)

| Path | Role |
|---|---|
| Extend `scripts/portable_dual_topology_{demo,orch,lib}.py` (and peers as needed) | `--backend`, adapter hook |
| New adapter module (name TBD in plan, e.g. `portable_dual_topology_rpc.py`) | Spawn/probe BIN, ports, client, status, restart |
| `scripts/portable_rpc_selftest.py` or gated section in existing selftest | Opt-in when BIN set |
| `docs/PORTABLE-DUAL-TOPOLOGY-SIM.md` (or sibling RPC doc) | How to run stub vs RPC gates |
| This spec | Design authority for the gate |

Phase A scripts remain unmodified.

## Build and #26724 requirements

### Binary contract

- Env `SHARECOMPUTE_LLAMA_BIN` (or `BIN`) must point at a llama.cpp build with `-DGGML_RPC=ON` **and** peer-death handling from **`ggml-org/llama.cpp#26724`** (or equivalent landed patch).
- Required tools under that prefix / on `PATH`: `ggml-rpc-server`, plus client (`llama-server` preferred; `llama-cli` allowed only if status is read from decode/API, **not** exit code).
- If `--backend llamacpp-rpc` and BIN is missing or fails a version/capability probe → exit ≠ 0 with a clear message. **No silent fallback to stub or local-only.**

### Model

- Default: `qwen2.5-0.5b-instruct-q4_k_m.gguf` (same as spike; overridable via env/flag).
- Client holds the GGUF; workers are RPC backends for assigned layer ranges.

### Status / health rules

- Success = compute completed with **no** `llama_decode == -3` (or HTTP 500 / equivalent) and tokens consumed **only after** status check (F33 corrupted-token rule).
- Never treat `llama-cli` exit 0 as success after a kill.
- Do not trust `/health == ok` alone while latch can lie (F33–F35). Prefer decode/compute error channel. If latch query becomes available upstream, prefer it; until then, kill path = detect failure + restart topology.

### Same-host ports

- Ephemeral loopback ports (spike-style: below host ephemeral range or OS-assigned with bind retry).
- Must not collide with Phase A / portable discovery ports by accident; document the chosen range in the run doc.

### CI / local without BIN

- Default `--backend stub` keeps current portable selftest green.
- RPC selftest / gate commands are **opt-in** when BIN is set.

## Fail / recovery

### Hard fails (exit ≠ 0, loud — no hang)

- Control-plane discovery / seat / platform / role mismatch (unchanged from stub).
- `--backend llamacpp-rpc` with missing or non-#26724 BIN / probe fail.
- Client start fail, RPC server start fail, or port bind fail.
- Compute path reports `llama_decode == -3` (or HTTP 500 equivalent) after retries exhausted.
- Mid-run worker kill: must surface as a **caught** failure under #26724 — never silent success.

### Peer loss = restart topology (F35)

- No reattach / latch reuse for this gate.
- On peer death or decode/-3: tear down client + RPC server for that attempt, then **restart the full topology** (new ports, new processes, fresh latch).
- **One** automatic retry per topology; second failure → exit ≠ 0.

### Kill matrix (BIN set; both topologies)

- Kill worker mid-decode → detect fail + one restart; complete after restart counts as success for that topology; second fail → exit ≠ 0.
- Kill worker before first token → same policy.
- Stub backend keeps today’s kill / fail-mode matrix without llama processes.

### Out of scope for recovery

- Hot reattach into an existing latch.
- Treating `/health == ok` alone as recovered.
- Soft-degraded “continue with fewer layers” without full restart.

## Acceptance criteria

### Always (CI / no BIN)

1. Default `--backend stub`: existing portable selftest stays green (Phase B matrix untouched).
2. Phase A scripts unchanged and still pass their own checks.

### When `SHARECOMPUTE_LLAMA_BIN` / `BIN` is set (`--backend llamacpp-rpc`)

1. **Happy both** — `--topology both` exit 0: both topologies complete a short generate with clean status.
2. **Single topology** — each of `iphone-frontend` and `windows-frontend` alone can pass the happy path.
3. **Missing BIN** — `--backend llamacpp-rpc` without usable BIN → exit ≠ 0, clear error (no silent stub fallback).
4. **Kill mid-decode** — SIGKILL worker during generate → detect fail + one restart; never silent success / corrupted-token-as-OK.
5. **Kill before first token** — same restart policy as (4).
6. **Role / seat fails** — control-plane mismatches still exit ≠ 0 (stub or RPC).
7. **Endpoint cap** — each topology run uses at most two RPC endpoints (one worker for this gate).

### Done when

- This design spec is reviewed and accepted.
- Implementation plan is written and reviewed.
- Gate commands are documented and runnable with BIN set; stub remains CI default.

## Relationship to existing code

| Existing | Relationship |
|---|---|
| `scripts/portable_dual_topology_*` | Extend; keep stub path as default |
| `docs/PORTABLE-DUAL-TOPOLOGY-SIM.md` | Update or add sibling RPC run section |
| `scripts/inference_pipeline_*` | Untouched Phase A lab |
| `Spikes/llamacpp-rpc/` | Recipe + hazard source of truth; not the product CLI |
| Spike README (F15, F25, F27, F33–F35) | Constraints this design must not violate |
| `Sources/ShareComputeCore/*` | No Swift change required for this gate |

## Open implementation notes

- Prefer `llama-server` for status channel when available; fall back to `llama-cli` only with explicit decode/status parsing.
- Probe for #26724 capability (e.g. string/`device is now unusable` or documented probe) before claiming RPC backend ready.
- Port selection must avoid the false one-peer bind failure class from the spike (ports in ephemeral range).
- Implementation plan should use TDD: stub matrix first (regression), then BIN-gated RPC cases.
- Exact layer-split CLI flags (`--rpc`, `-ngl`, device mapping) to be copied from a verified spike recipe in the plan’s first task — do not invent new splits in the design.

## Success criteria (this design doc)

1. Spec checked into `docs/superpowers/specs/` and reviewed.
2. Section decisions above match the locked review (architecture, build/#26724, roles mapping, fail/recovery, acceptance).
3. Implementation plan follows only after written-spec approval.
