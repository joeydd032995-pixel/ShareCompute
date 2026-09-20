# Four-platform pool connect

ShareCompute’s membership core is platform-neutral. InferRing today only runs on
Apple devices (MLX). This document describes the **smallest path** that gets
**Windows, macOS, iOS, and Android** into the same pool — including simulated
stand-ins — and how far that goes toward real RAM pooling.

## What shipped

| Piece | Role |
|---|---|
| `PlatformKind` | Explicit `windows` / `macos` / `ios` / `android` seat labels |
| `RingTransport` + `InProcessRingTransport` | Host-provided control-plane transport (no sockets in core) |
| `CrossPlatformPool` | Membership + platform roster + RAM shard planning |
| `SimulatedPlatformPeer` | Stock capability profiles per platform |
| `FourPlatformDemo` | `swift run` executable |
| `scripts/four_platform_pool_demo.py` | Zero-toolchain Python demo |
| `FourPlatformPoolTests` | Asserts all four seats connect and plan |

## How to run the demo

### Option A — Python (no Swift required)

```bash
python3 scripts/four_platform_pool_demo.py
```

Expected: four `✓` lines (Windows / macOS / iOS / Android), then a shard plan
covering 48 layers across the pooled RAM.

### Option B — Swift executable

Requires Swift 6.0+.

```bash
swift run FourPlatformDemo
# or
swift test --filter FourPlatformPoolTests
```

Both demos use **in-process simulated peers**. They exercise the same membership
and largest-remainder planner the Apple app will call; they do not open LAN
sockets or talk to phones/PCs.

## Real vs simulated

| Layer | Status |
|---|---|
| Membership epoch / lease / drain | **Real** (`MembershipService`) |
| RAM shard apportionment | **Real** (`StagePlanner`) |
| Platform capability profiles | **Real types**, simulated values in the demo |
| Windows / Android / iOS / macOS *processes* in the demo | **Simulated** (`SimulatedPlatformPeer` / Python stand-ins) |
| InferRing on a Mac + iPhone | **Real app** (MLX), not wired to `CrossPlatformPool` yet |
| Windows / Android native clients | **Not built** (roles remain gated until a non-MLX runtime exists) |
| Cross-machine wire protocol over TCP/WebSocket | **Stub only** (`RingTransport`); in-process impl for demos |

## What’s still incomplete for real-device RAM pooling

1. **Runtime adapters** for Windows (WinML / llama.cpp) and Android (LiteRT /
   llama.cpp). MLX remains Apple-only; the gated `windows-*` / `android-*`
   agent roles stay correct until those land.
2. **Network transport** — a real `RingTransport` over TCP or WebSocket, plus
   discovery (Bonjour today on Apple; mDNS / explicit host lists elsewhere).
3. **Wire auth** — the control plane is still unauthenticated (see root README).
4. **InferRing adapter** — `RingCoordinator` should drive `CrossPlatformPool`
   (or share its platform roster) instead of only Bonjour Apple peers.
5. **On-device memory accounting** — demo GB figures are stock defaults; real
   clients must publish measured `MemoryProfile.usableBytes`.
6. **Collective / inference path** — pooling layers across heterogeneous
   backends (Metal vs DirectML vs LiteRT) needs the spec’s portable IR / RPC
   path (see `Spikes/llamacpp-rpc`).

## Success criterion for this milestone

> All four platforms successfully **connect** to the pool (simulated stand-ins
> acceptable), and the planner produces a contiguous shard plan over their
> combined usable RAM.

That criterion is what the demos and `FourPlatformPoolTests` assert.
