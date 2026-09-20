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
| `scripts/four_platform_pool_demo.py` + `_lib.py` | **UDP LAN discovery + TCP multi-process** Python demo |
| `FourPlatformPoolTests` | Asserts all four seats connect and plan; missing seat blocks plan |

## How to run the demo

### Option A — Python networked multi-process (recommended, no Swift)

This is the path that behaves like devices on a LAN:

1. **Hub** binds a TCP listen socket and periodically announces a small JSON
   beacon over **UDP multicast** (`239.255.77.77:37777`, service id
   `sharecompute-pool`).
2. Each **peer** process listens for that beacon (with timeout), extracts the
   hub host/TCP port, then **JOIN**s over TCP.
3. If any required platform misses the join deadline — or peers never hear a
   beacon — the process exits **non-zero** and prints which seat(s) / discovery
   step failed.

Peers are still simulated OS processes with stock RAM profiles; discovery and
connect are real enough that **missing announcement or wrong discovery port
fails the join** (same hard-fail semantics as a missing TCP peer).

```bash
# Success — hub announces; four peers discover + JOIN within the timeout
python3 scripts/four_platform_pool_demo.py

# Negative test — omit Android peer; hub times out → exit 1
python3 scripts/four_platform_pool_demo.py --fail-platform android

# Negative test — hub does not announce; peers fail discovery → exit 1
python3 scripts/four_platform_pool_demo.py --fail-discovery
# equivalent: --no-beacon

# Shorter deadline
python3 scripts/four_platform_pool_demo.py --timeout 3 --fail-platform ios
```

Expected success: peer `discovered hub …` lines, four `✓ … joined via TCP`
lines, then a 48-layer shard plan, exit code `0`.

Expected failure (`--fail-platform android`): three joins, then
`FAIL: platform(s) did not join… ✗ Android`, exit code `1`.

Expected failure (`--fail-discovery`): peer lines
`discovery failed — no sharecompute-pool beacon…`, orchestrator
`FAIL: discovery failed`, exit code `1`.

Child roles (normally spawned by the orchestrator):

```bash
python3 scripts/four_platform_pool_demo.py --role hub --port 9876 --timeout 5
python3 scripts/four_platform_pool_demo.py --role peer --platform windows --timeout 5
# Optional: skip discovery and connect to an explicit hub
python3 scripts/four_platform_pool_demo.py --role peer --platform windows \
  --skip-discovery --host 127.0.0.1 --port 9876
```

### Why multicast (not broadcast)

On a real LAN, either UDP broadcast or multicast can carry a discovery beacon.
For **one-machine simulation** on Linux, `255.255.255.255` / `127.255.255.255`
broadcast is flaky across processes on loopback. This demo therefore uses
**IPv4 multicast** `239.255.77.77:37777` with `IP_MULTICAST_LOOP=1` so peers on
the same host reliably hear the hub. Documented choice; swap the group/port via
`--discovery-port` (and the constants in `four_platform_pool_lib.py`) if needed.

Beacon payload (JSON): `service=sharecompute-pool`, `hub_host`, `hub_port`,
`epoch` (token), and the platform seats the hub expects.

### Option B — Swift executable

Requires Swift 6.0+. Uses in-process simulated peers (no UDP/TCP). Still fails
the run when a required platform is omitted via `--fail-platform`.

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
| UDP LAN-style discovery (multicast beacon) | **Real** (Python demo; same-host multicast) |
| Multi-process TCP join after discovery | **Real** (Python demo: hub + 4 peer processes) |
| Join / discovery timeout → non-zero exit | **Real** (Python demo; Swift `--fail-platform`) |
| Windows / Android / iOS / macOS *runtimes* | **Simulated** (`SimulatedPlatformPeer` / Python stock profiles) |
| InferRing on a Mac + iPhone | **Real app** (MLX), not wired to `CrossPlatformPool` yet |
| Windows / Android native clients | **Not built** (roles remain gated until a non-MLX runtime exists) |
| Production cross-machine Bonjour/mDNS + auth | **Not done** — Python demo proves discovery *shape* on one machine |

Honest summary: **platform runtimes are still simulated**, but the Python demo’s
**discovery path is LAN-style UDP** and the **connect path is networked
multi-process TCP**, and both **can fail** the way a missing announcement or
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

> All four platforms successfully **discover** and **connect** to the pool
> (simulated runtimes acceptable), over a **device-like join path** that
> **fails the run** if discovery misses or a platform cannot join in time, and
> the planner produces a contiguous shard plan over their combined usable RAM.

The Python UDP+TCP demo and `FourPlatformPoolTests` assert that criterion.
