# Findings

Research log. Anything discovered goes here immediately, before it is lost.

---

## F1 — MLX `DistributedGroup` is static and never torn down

`Apps/InferRing/Ring/Manager.swift` writes a hostfile mapping rank → address, sets `MLX_RANK`,
and calls `DistributedGroup.initialize(strict: true)`. A grep across all 43 Swift sources shows
`DistributedGroup` appears only as a type annotation and in that single `initialize` call. There
is **no** `finalize`, teardown, or release anywhere.

**Consequence:** membership cannot be mutated. Topology changes have to be modelled as epochs —
destroy the group, re-plan, rebuild.

---

## F2 — Every generated token is an all-ranks barrier

`Apps/InferRing/Ring/AutoParallel.swift`:
- `PipelineFirstLayer.callAsFunction` → `group.recvLike(x, source: r-1)` for all ranks but 0.
- `PipelineLastLayer.callAsFunction` → `group.send(...)` then **`group.allGather(output)`**, on
  every forward pass.

**Consequence:** a lease cannot be attached to a rank that is inside a per-token collective. An
elastic node either belongs to the current epoch's group or it hosts no pipeline stage at all.

---

## F3 — There is no failure detection today

- `DataClient.ping()` is defined (`Apps/InferRing/InferRing/Services/DataClient.swift`) and
  called from **nowhere** in the codebase.
- `RingCoordinator.processElectionMessage` calls `bonjourClient?.stopSearching()` immediately
  after `mlxManager.initMLX(...)` succeeds — the app stops observing topology at exactly the
  moment inference begins.
- The only liveness signal is Bonjour add/remove, and it is switched off.

---

## F4 — Zero iOS lifecycle handling

Grep across all sources finds no `scenePhase`, no `willResignActive`, no
`UIApplicationDidEnterBackground`. `Infer-Ring-Info.plist` declares **no `UIBackgroundModes`**.
The only nod to staying alive is `UIApplication.shared.isIdleTimerDisabled = true` set during
load and generation.

**Consequence:** backgrounding the iOS app kills its sockets with no notice to any peer. This is
the single most common real-world failure and it currently deadlocks the whole ring.

---

## F5 — Three defects in shard assignment

`Apps/InferRing/InferRing/Services/ModelManager.swift`, `assignShardMetadata` /
`checkIfCanLoad`:

1. **Remainder concentration.** `nLayers * deviceMemory / totalMemory` is integer division, so
   every device truncates; `endLayer: rank < size-1 ? … : nLayers` then dumps all accumulated
   remainder on the final rank.
2. **nil-profile inconsistency.** `assignShardMetadata` substitutes `1GB` for a device with no
   `hardwareProfile`, but `RingCoordinator.usableRAM` `compactMap`s nil profiles away entirely.
   The device contributes to the numerator but not the denominator, so boundaries can exceed
   `nLayers` and are then silently clamped by `safeStart`/`safeEnd` in `pipelineAutoParallel`,
   producing an empty or truncated shard with no error surfaced. `requestMissingProfiles()`
   fetches profiles best-effort, so one failed HTTP call triggers this.
3. **Aggregate-only feasibility.** `checkIfCanLoad` compares model size against the *sum* of
   `usableRAM`. A plan that fits in aggregate but assigns some device more than its own RAM
   passes and then OOMs at load.

---

## F6 — Unused fault-injection hooks already exist

`Apps/InferRing/Ring/Models/Shards.swift` declares `ShardMetadata.immediateException: Bool` and
`ShardMetadata.shouldTimeout: Double?`. Both are encoded/decoded and travel inside
`ModelLoadRequest`, but grep shows **no reads anywhere** — they are dead scaffolding.

**Consequence:** the chaos-testing hooks the spec asks for in §3b are already plumbed end to end.
Wiring them up is cheap.

---

## F7 — The control plane is unauthenticated

`DataServer.FileServerHandler` serves `/elect`, `/loadModel`, `/startGeneration`, `/resetChat`,
`/getHardwareProfile`, `/download`, and the OpenAI-compatible routes over plain HTTP with no
authentication. Any LAN device advertising a Bonjour name prefixed `InferRing` is accepted as a
peer.

Separately, `ModelManager.streamResponseChunks` broadcasts `GenerationRequest` — which carries
the **full conversation history** — to every peer in cleartext.

**Consequence for this milestone:** adding `/epoch` and `/lease/revoke` unauthenticated would
hand any LAN device a one-request ring-teardown primitive that does not exist today. A shared
ring secret is therefore in scope for M1. `swift-crypto` 4.2.0 is already a resolved dependency
(via async-http-client), so no new package is needed.

---

## F8 — Environment limits on verification

This container is **x86_64 Linux (Ubuntu 24.04)**. It has no macOS, no Xcode, and no Apple
Silicon, so:

- `ShareComputeCore` **can** be built and tested here (Swift 6.1.2 toolchain installed to
  `/opt/swift`; the package intentionally has no external dependencies).
- The Xcode project, anything importing MLX/UIKit, and both spikes **cannot** run here. They
  require a Mac.

---

## Spike A — Can an MLX `DistributedGroup` be re-initialized in-process?

**Status: BLOCKED — requires Apple hardware. Not run.**

Harness: `Spikes/SpikeA_GroupTeardown/`. Run on a Mac per its README and paste results below.

The epoch model in this milestone depends on the answer. Fill this in before writing the
`MLXManager.teardown()` production path.

| Attempt | Setup | Result |
|---|---|---|
| | | |

---

## Spike B — Can a survivor escape a blocked collective?

**Status: BLOCKED — requires Apple hardware. Not run.**

Harness: `Spikes/SpikeB_BlockedCollective/`. Run on a Mac per its README and paste results below.

Determines whether hard-failure recovery is possible at all, or whether M1 is limited to
guaranteed-graceful / best-effort-hard.

| Attempt | Setup | Result |
|---|---|---|
| | | |
