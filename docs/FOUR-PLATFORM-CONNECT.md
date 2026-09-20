# Four-platform pool connect

ShareCompute’s membership core is platform-neutral. InferRing today only runs on
Apple devices (MLX). This document describes the **smallest path** that gets
**Windows, macOS, iOS, and Android** into the same pool — including simulated
runtime stand-ins — and how far that goes toward real RAM pooling.

## What shipped

| Piece | Role |
|---|---|
| `PlatformKind` | Explicit `windows` / `macos` / `ios` / `android` seat labels |
| `RingTransport` + `InProcessRingTransport` | Host-provided control-plane transport (no sockets in core I/O path) |
| `TcpRingProtocol` | Shared framing helpers for the localhost TCP hub protocol |
| `CrossPlatformPool` | Membership + platform roster + RAM shard planning |
| `SimulatedPlatformPeer` | Stock capability profiles per platform |
| `FourPlatformDemo` | `swift run` executable (in-process + `--fail-platform`) |
| `scripts/four_platform_pool_demo.py` | **Networked multi-process** Python demo (primary device-like path) |
| `FourPlatformPoolTests` | Asserts all four seats connect and plan; missing seat blocks plan |

## How to run the demo

### Option A — Python networked multi-process (recommended, no Swift)

This is the path that behaves like devices: **separate OS processes** join a hub
over **localhost TCP**. If any required platform misses the join deadline, the
process exits **non-zero** and prints which seat(s) failed.

```bash
# Success — hub + four peer processes; all must JOIN within the timeout
python3 scripts/four_platform_pool_demo.py

# Negative test — omit Android peer; hub times out → exit 1
python3 scripts/four_platform_pool_demo.py --fail-platform android

# Shorter deadline
python3 scripts/four_platform_pool_demo.py --timeout 3 --fail-platform ios
```

Expected success: four `✓ … joined via TCP` lines, then a 48-layer shard plan,
exit code `0`.

Expected failure (`--fail-platform android`): three joins, then
`FAIL: platform(s) did not join… ✗ Android`, exit code `1`.

Child roles (normally spawned by the orchestrator):

```bash
python3 scripts/four_platform_pool_demo.py --role hub --port 9876 --timeout 5
python3 scripts/four_platform_pool_demo.py --role peer --platform windows --port 9876
```

### Option B — Swift executable

Requires Swift 6.0+. Uses in-process simulated peers (no TCP). Still fails the
run when a required platform is omitted via `--fail-platform`.

```bash
swift run FourPlatformDemo
swift run FourPlatformDemo -- --fail-platform android
swift test --filter FourPlatformPoolTests
```

## Real vs simulated

| Layer | Status |
|---|---|
| Membership epoch / lease / drain | **Real** (`MembershipService`) |
| RAM shard apportionment | **Real** (`StagePlanner`) |
| Platform capability profiles | **Real types**, simulated GB values in demos |
| Multi-process localhost TCP join | **Real** (Python demo: hub + 4 peer processes) |
| Join timeout → non-zero exit | **Real** (Python demo; Swift `--fail-platform`) |
| Windows / Android / iOS / macOS *runtimes* | **Simulated** (`SimulatedPlatformPeer` / Python stock profiles) |
| InferRing on a Mac + iPhone | **Real app** (MLX), not wired to `CrossPlatformPool` yet |
| Windows / Android native clients | **Not built** (roles remain gated until a non-MLX runtime exists) |
| Production cross-machine TCP/WebSocket + discovery | **Not done** — Python demo proves the connect *shape* on localhost only |

Honest summary: **platform runtimes are still simulated**, but the Python demo’s
**connect path is networked and multi-process** and **can fail** the way a
missing device would.

## What’s still incomplete for real-device RAM pooling

1. **Runtime adapters** for Windows (WinML / llama.cpp) and Android (LiteRT /
   llama.cpp). MLX remains Apple-only; the gated `windows-*` / `android-*`
   agent roles stay correct until those land.
2. **Production network transport** — discovery (Bonjour today on Apple; mDNS /
   explicit host lists elsewhere), reconnect, and a host-owned `RingTransport`
   wired into InferRing (not only the Python localhost hub).
3. **Wire auth** — the control plane is still unauthenticated (see root README).
4. **InferRing adapter** — `RingCoordinator` should drive `CrossPlatformPool`
   (or share its platform roster) instead of only Bonjour Apple peers.
5. **On-device memory accounting** — demo GB figures are stock defaults; real
   clients must publish measured `MemoryProfile.usableBytes`.
6. **Collective / inference path** — pooling layers across heterogeneous
   backends (Metal vs DirectML vs LiteRT) needs the spec’s portable IR / RPC
   path (see `Spikes/llamacpp-rpc`).

## Success criterion for this milestone

> All four platforms successfully **connect** to the pool (simulated runtimes
> acceptable), over a **device-like join path** that **fails the run** if a
> platform cannot join in time, and the planner produces a contiguous shard
> plan over their combined usable RAM.

The Python TCP demo and `FourPlatformPoolTests` assert that criterion.
