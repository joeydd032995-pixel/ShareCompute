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

## F9 — The app depends on **forks** of MLX, not upstream

`Apps/InferRing/Infer Ring.xcodeproj/project.pbxproj` pins:

- `https://github.com/N1k1tung/mlx-swift` branch `ios-distrib-0.3.0`
- `https://github.com/N1k1tung/mlx-swift-lm` branch `ios-distrib-0.3.0`

Neither appears in `Package.resolved` (Xcode stores branch-pinned remote packages in the pbxproj).
The fork pins upstream `ml-explore/mlx` at submodule commit `38ad257088fb2193ad47e527cf6534a689f30943`.

This matters for both spikes: they must be answered against *these* sources, not upstream MLX.
It also means patching MLX is already within the project's idiom — a fork is being maintained.

---

## Spike A — Can an MLX `DistributedGroup` be re-initialized in-process?

**Status: ANSWERED — NO. Determined by source inspection; no hardware needed.**

Two independent blockers, either of which is fatal on its own.

**1. There is no way to free a group.** `mlx-swift` fork,
`Source/MLX/DistributedGroup.swift:15-17`:

```swift
deinit { // this requires slight update for cmlx which I rather avoid, commented out for now
//        mlx_distributed_group_free(group)
}
```

The only free call is commented out, with the fork author noting it needs a cmlx change they
chose not to make. `grep` over `mlx/distributed/distributed.h` and `distributed_impl.h` finds no
`finalize`, `shutdown`, `destroy`, `reset` or `cleanup` entry point at all.

**2. Even if it could be freed, re-initialising returns the stale group.** Upstream
`mlx/distributed/distributed.cpp:141-148`:

```cpp
Group init(bool strict, const std::string& bk) {
  static std::unordered_map<std::string, std::shared_ptr<detail::GroupImpl>> backends;

  // Already initialized so return the group.
  if (auto g = backends.find(bk); g != backends.end()) {
    return Group(g->second);
  }
  ...
```

`backends` is a **function-local static**, cached for the lifetime of the process. The second and
every subsequent `mlx_distributed_init` short-circuits and hands back the first group — with the
original world size and the original hostfile. `MLX_HOSTFILE` and `MLX_RANK`, which
`Ring/Manager.swift` rewrites per ring formation, are read only on the first call.

**Consequence: `MLXManager.teardown()` as planned cannot be written.** Tearing down and
re-initialising the group in-process is not a hard problem here, it is an unavailable operation.
The approved plan gated on this and said to stop and re-plan — see "Re-plan" below.

---

## Spike B — Can a survivor escape a blocked collective?

**Status: ANSWERED — NO. Determined by source inspection; no hardware needed.**

`mlx/distributed/ring/ring.cpp`. Each socket has a worker thread draining queues of `SocketTask`,
each of which carries a `std::promise<void>` that the collective awaits.

Promises are fulfilled on the success path only (`ring.cpp:208-217`):

```cpp
if (delete_recv) { recvs_.front().promise.set_value(); recvs_.pop_front(); ... }
if (delete_send) { sends_.front().promise.set_value(); sends_.pop_front(); ... }
```

The failure path (`ring.cpp:260-262`) abandons them:

```cpp
if (error_count >= 10) {
  log_info(true, "Too many send/recv errors. Aborting...");
  return;
}
```

The worker returns, leaving the outstanding tasks — and their unfulfilled promises — sitting in
`recvs_`/`sends_`. The promises are not destroyed, so `future::get()` does not even receive
`broken_promise`. It waits forever. No C++ exception is thrown, so nothing propagates to Swift and
the existing `MLX.withError` wrapper in `Manager.swift:35` has nothing to catch.

There is a second, probably more common variant: `error_count` is incremented only when
`errno != EAGAIN` (`ring.cpp:241`, `:256`), and an orderly close returning `r == 0` is not
distinguished from "no data yet". A peer that is suspended rather than reset may therefore never
push the counter to 10 at all — the loop spins indefinitely instead. Both variants end the same
way: **the survivor hangs permanently.**

Sockets are also explicitly non-blocking with no `SO_RCVTIMEO`/`SO_SNDTIMEO`
(`ring.cpp:133`, `:140`), so there is no timeout to lean on.

**Consequence:** a Swift-level watchdog can *detect* the hang but cannot clear it. The blocked
thread is unrecoverable inside the process.

---

## Re-plan (both spikes failed)

What survives untouched: **`ShareComputeCore`**. It deliberately imports neither MLX nor UIKit, so
none of the above invalidates it. That boundary is the reason the failure is contained.

What changes: an epoch cannot be *enacted* by re-initialising in-process. Three options, in
preference order:

1. **Patch the MLX fork** — the only route that works on iOS. Needs (a) `mlx_distributed_group_free`
   exposed through mlx-c, which is precisely the "slight update for cmlx" the fork author deferred;
   (b) a `distributed::reset()` in `distributed.cpp` that clears the `backends` static and tears
   down ring sockets; and (c) fulfilling abandoned promises with an exception on the abort path in
   `ring.cpp` so survivors fail instead of hanging. (c) alone would make hard-failure recovery
   possible and is the smallest high-value change.
2. **Process restart** — relaunch with a new hostfile. Viable on macOS, impossible on iOS, so it
   cannot deliver the milestone's headline scenario.
3. **Ship detection without re-formation** — no MLX changes at all. Drain-on-background plus
   heartbeats cannot re-form the ring, but they convert *today's silent permanent hang* into a
   detected, reported failure with a clear "ring lost" state. Strictly better than current
   behaviour and independently shippable.

Option 3 is the honest immediate deliverable; option 1 is what actually completes the milestone.

---

## F10 — An exception may not escape a dispatched MLX task

`mlx/backend/cpu/encoder.h`, `CommandEncoder::dispatch` binds the callable and hands it to
`scheduler::enqueue`. There is **no `try`/`catch` in that path**; the only one in `scheduler.h` is
inside `~Scheduler()`. An exception thrown from a collective's execution path therefore unwinds a
scheduler thread with no handler and calls `std::terminate()`.

**Consequence:** the intuitive fix for Spike B — "make `ring.cpp` throw" — converts a hang into a
hard crash of a shipping App Store app. Failures must be *recorded* on the scheduler thread and
rethrown from the graph-building layer, which runs on the caller's own thread.

Note three throws are **already** reachable from inside those lambdas today (unsupported send/recv
neighbours, and an `all_reduce` too small to split), so this crash is latent in upstream MLX.

---

## F11 — `wait()` silently discards the failure

All ten future consumption sites in `ring.cpp` call `f.wait()`, never `f.get()`.
`std::future::wait()` returns **normally** on a promise carrying an exception — the state is ready,
and nothing is thrown.

**Consequence:** setting exceptions on the promises without also converting every `wait()` to
`get()` would replace the hang with *silent data corruption* — execution continuing over
incomplete buffers. That is strictly worse than the current behaviour, because a hang is at least
visible. The two changes are only safe together.

---

## F12 — `Cmlx` is a source target

`Package.swift` in the mlx-swift fork declares `Target.target(name: "Cmlx", path: "Source/Cmlx", …)`,
compiling the vendored `mlx` and `mlx-c` submodule sources directly through SwiftPM — no binary
framework, no CMake step. Patching MLX means editing submodule sources and rebuilding normally,
which is what makes milestone 2 tractable at all.

---

## F13 — `ios-distrib-0.3.0` is a tag, and the project asks for it as a branch

Confirming which sources Stage 2 had to be written against turned up a discrepancy in the pin
recorded in F9.

`project.pbxproj` declares both mlx-swift packages with `kind = branch`:

```text
repositoryURL = "https://github.com/N1k1tung/mlx-swift";
requirement = { branch = "ios-distrib-0.3.0"; kind = branch; };
```

On the remote, `ios-distrib-0.3.0` exists **only as a tag**, at `c53d302197489acbb6b3a81dc1635d0aae75b163`.
The branches are `ane`, `ios-distrib`, `main` and `update` — there is no branch of that name. This
also explains F9's aside that neither package appears in `Package.resolved`.

The good news is that the pins Stage 1 and Stage 2 were written against are **confirmed correct**:
tag `ios-distrib-0.3.0` is exactly the tree whose submodules are `mlx@38ad2570` and `mlx-c@0726ca9`.

The consequence is for **Stage 3**, not for the patches. Repointing the project at the forks means
writing that reference by hand, and it cannot be copied as-is: a `kind = branch` requirement naming
a tag is what is there now. Use `kind = revision` with the commit, or a real branch on the fork.

Not verified: whether Xcode currently resolves this, fails, or silently falls back — that needs
macOS. It is recorded because Stage 3 has to write this reference, not because the app is known
broken today.

---

## F14 — The loaded model holds the ring, which constrains Stage 3's teardown order

Restoring mlx-swift's commented-out `deinit` (Stage 2) meant auditing whether anything in the app
relies on a C `Group` outliving its Swift wrapper — the leak has been the behaviour for the life of
the fork, so such a call site would become a use-after-free.

**No such call site exists.** Every holder retains the Swift wrapper class, not the raw
`mlx_distributed_group`:

| Holder | What it holds |
|---|---|
| `Ring/Manager.swift` — `MLXManager.group` | `DistributedGroup?` |
| `Ring/AutoParallel.swift` — `AllToShardedLinear`, `ShardedToAllLinear` | `public let group: DistributedGroup` |
| `Ring/TensorParallel.swift` — sharded MoE layers | `public let group: DistributedGroup` |

ARC therefore guarantees the wrapper outlives every user, and freeing in `deinit` is safe.

**But the same audit found the real constraint on Stage 3.** `MLXManager.loadModel` passes the group
into `tensorAutoParallel` / `pipelineAutoParallel`, which store it on the sharded layers, giving:

```text
ModelManager → ModelContext → model → sharded layers → DistributedGroup → C handle → shared_ptr<GroupImpl>
```

So `MLXManager.teardown()` **cannot** be `group = nil; DistributedGroup.finalize()`. Whenever a
sharded model is loaded — the normal state of the app — the layers still hold the group,
`finalize()` returns `false`, and by design changes nothing. **The loaded `ModelContext` must be
released before finalize.**

This is a sequencing requirement, not a defect: Stage 2's check-before-clear is what makes the
mistake visible rather than silent. Without the `bool`, this would have presented as a ring that
re-formed with stale membership and no error anywhere.

Not verified: this is a reading of the Swift sources, not a run. Whether releasing `ModelContext`
actually drops every layer reference — MLX modules can retain children in ways not obvious from the
declaration site — has to be checked on hardware.
