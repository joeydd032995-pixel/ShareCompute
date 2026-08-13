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

---

## F15 — llama.cpp RPC as the portable execution engine: the only realistic candidate, and further from ready than it first looks

Read against `ggml-org/llama.cpp` at `9afff1b`. The question was whether its RPC backend could replace
MLX as the execution layer, since MLX is Apple-only and the specification targets five OSes.

### What it supplies

| Capability | Evidence |
|---|---|
| Multi-machine distribution | `llama-cli --rpc host:port,host:port` — "one or several instances", `tools/rpc/README.md` |
| Heterogeneous backends in one cluster — *unnegotiated* | README topology shows CUDA hosts and a Metal host together. But `ggml_backend_rpc_device_supports_op` (`ggml-rpc.cpp:1904-1909`) is `//TODO: call the remote backend and cache the results` and **returns `true` for every op** — see gap 6 |
| Portable *by construction* to all five | `transport.cpp:4-13` splits `winsock2` / `sys/socket`; llama.cpp's general CI covers iOS (`CMAKE_SYSTEM_NAME=iOS`), Android NDK arm64, macOS arm64+Intel, Windows, Linux. **Not the same as an RPC build** — see below |
| Memory-proportional splitting | "distributes model weights and the KV cache ... in proportion to each device's available memory" |
| Protocol version negotiation | `RPC_PROTO_MAJOR/MINOR/PATCH`, HELLO handshake. `ggml-rpc.cpp:347` rejects a *differing major* or a server minor *newer than* the client; patch is never compared and an older server minor is accepted. Asymmetric, but MLX has no equivalent at all |
| Non-owning connection cache | `ggml-rpc.cpp:357-367` — function-local static map of **`weak_ptr`**, mutex-guarded |

That last row needs care, because the cache design and the runtime behaviour do not agree.

The *socket* cache is the same shape as MLX's group cache but holds `weak_ptr` rather than
`shared_ptr`, so an entry expires once the last owner drops it. Structurally that is what
`Patches/mlx/0002` had to add to MLX.

**But MLX's defect does occur here — one level up.** `ggml_backend_rpc_add_server`
(`ggml-rpc.cpp:2013-2053`) keeps its endpoint registrations in a function-local static
`std::unordered_map<std::string, ggml_backend_reg_t> reg_map`, returns the existing entry on a
repeat call (`:2018-2020`), and **offers no removal path**. That is precisely the shape of
`distributed::init` that this project spent Stage 2 fixing: an owning, process-lifetime,
function-local static cache that nothing can clear.

The consequence is specific to epoch re-formation, which is the whole reason this assessment exists.
If a peer leaves and rejoins — restarted, or exposing a different set of devices — the socket may
reconnect, but `add_server` hands back the **stale registration and its enumerated devices**. A
llama.cpp path therefore needs its own `finalize()`-equivalent, for the same reason and with the
same shape as `Patches/mlx/0002`.

**And even the socket cache does not buy a reconnect in practice.** `ggml_backend_rpc_buffer_context` holds a
`std::shared_ptr<socket_t>` (`ggml-rpc.cpp:232-236`), and every buffer operation calls through
`ctx->sock` and then `RPC_STATUS_ASSERT` — e.g. `ggml_backend_rpc_buffer_set_tensor` at
`ggml-rpc.cpp:486-496`. So while a model is loaded the buffers pin the socket, and an I/O failure
**aborts the process before any replacement could be created**. A new socket appears only if every
strong owner releases the failed one *and* a later `get_socket()` runs. This is not transparent
reconnect and it is not health-aware; it is a cache that would permit reconnect if something else
were handling the failure.

### The gaps — which are precisely this project's subject matter

1. **A departing peer aborts the whole process.** `RPC_STATUS_ASSERT` is
   `if (!(x)) GGML_ABORT(...)` (`ggml-rpc.cpp:30`), used at **19 sites**. `ggml_abort` runs the
   optional callback and then calls `abort()` **unconditionally** (`ggml/src/ggml.c:252-272`) — so it
   cannot be intercepted and turned into a recoverable error.
2. **A silently-dead peer hangs — on both directions.** `recv_data` (`transport.cpp:482-502`) and
   `send_data` (`transport.cpp:462-480`) are both blocking loops, and **neither `SO_RCVTIMEO` nor
   `SO_SNDTIMEO` appears anywhere in `ggml/src/ggml-rpc/`**; the only options set are `TCP_NODELAY`
   and `SO_REUSEADDR` (`transport.cpp:578`, `:584`). A suspended iPhone or a pulled cable produces
   no FIN, so a receive blocks forever — and so does a **send**, once the kernel socket buffer
   fills. The send path matters in its own right because model load and tensor upload push large
   volumes, so it is the likelier place to wedge during a join.

   This project already knows the send and receive routes fail differently: the Stage 1 harness
   tests them separately for exactly that reason (`Patches/mlx/README.md`, 0001 verification). Any
   llama.cpp-side fix needs bounded waits on **both**.
3. **No membership management of any kind.** No heartbeats, no leases, no failure detection, no
   re-planning. `ggml_backend_rpc_add_server()` exists; there is **no** `remove_server`.
4. **No authentication.** No auth/token/TLS anywhere in the RPC sources, and upstream says so:
   *"the functionality is fragile and insecure. Never run the RPC server on an open network or in a
   sensitive environment!"* Upstream self-describes RPC as "proof-of-concept development stage".
5. **A hard 16-device ceiling, from two places — and not the one the header advertises.**
   `GGML_RPC_MAX_SERVERS 16` in the public header is **never referenced** anywhere in the tree and
   enforces nothing. The real limits are elsewhere and they bite:
   - `llama_max_devices()` returns 16 (`src/llama.cpp:85-87`), bounding `--tensor-split` parsing
     (`common/arg.cpp:2737`, `:2803`) and sizing the fitting config (`common/common.h:473`);
   - the scheduler asserts `GGML_ASSERT(n_backends <= GGML_SCHED_MAX_BACKENDS)`
     (`ggml-backend.cpp:1788`, macro at `:753` = 16). `GGML_ASSERT` aborts, so this is a **hard
     ceiling, not a soft cap**.

   The ceiling counts **devices, not servers**, so a node exposing several accelerators consumes
   several slots. A ring of 16+ heterogeneous nodes is not a tuning exercise; it aborts.
6. **Heterogeneous placement is asserted, never negotiated.**
   `ggml_backend_rpc_device_supports_op` (`ggml-rpc.cpp:1904-1909`) is a stub —
   `//TODO: call the remote backend and cache the results` — that returns `true` unconditionally.
   The local scheduler therefore believes every remote device can run every op. In a genuinely
   heterogeneous ring (an iPhone's Metal beside a Windows CPU or a CUDA box) an op the remote
   backend cannot execute is still assigned to it, failing or aborting at run time rather than
   being planned around. This is the single biggest hole under the "heterogeneous" headline.

### Why this is still the right direction

Failure modes 1 and 2 are the *same two* this project already characterised and fixed in MLX, and the
split is instructive:

| | MLX before | MLX after Stages 1–2 | llama.cpp RPC today |
|---|---|---|---|
| Orderly peer close | hang (`r == 0` misread as stale `errno`) | catchable error | **process `abort()`** |
| Silent peer death | hang | catchable error | **hang** (blocking recv, no timeout) |
| Teardown / rebuild | impossible | `finalize()` | **the same defect**: `add_server`'s `reg_map` is an owning static with no removal path |
| Membership, leases, re-planning | none | `ShareComputeCore` | **none** |
| Authentication | none (F7) | none (F7) | none, and documented as unsafe |

llama.cpp got right the two things MLX got structurally wrong — `n == 0` is correctly treated as
peer-closed (`transport.cpp:497`), and the connection cache is non-owning — and then throws the
advantage away by converting that correct detection into a process abort. Detecting the failure
accurately and then calling `abort()` is not better than MLX's post-Stage-1 behaviour; it is worse,
because a caller can catch an exception and cannot catch an `abort()`.

**The complementarity is the point.** llama.cpp RPC supplies portable execution and a wire protocol,
which is exactly what `ShareComputeCore` cannot supply and MLX cannot make portable.
`ShareComputeCore` supplies epochs, leases, failure detection and stage planning, which is exactly
what llama.cpp RPC lacks. Neither is redundant with the other.

The MLX patches do **not** transfer — different codebase, different failure mechanics. The *approach*
does: record the failure, return it rather than aborting, give the caller a bounded wait.

### Net assessment, after two rounds of review

This finding was corrected twice under review, both times in the same direction: **every revision
made llama.cpp RPC look further from ready.** The first draft called the connection cache a solved
teardown story; the second found buffers pin the socket so the abort fires first; the third found
`add_server`'s `reg_map` reproduces the MLX static-cache defect outright. That trend is itself worth
recording — the parts that look finished from the README are the parts that have not been read yet.

What has *not* moved is the reason to care. Nothing else supplies portable execution plus a wire
protocol across all five target OSes, and the gaps are without exception the kind of work this
project has already done once. Adopting it means signing up for, at minimum:

1. bounded waits on **both** socket directions (gap 2);
2. an error-returning path in place of 19 `GGML_ABORT` sites (gap 1);
3. a `finalize()`-equivalent for `reg_map` (teardown, above);
4. real `supports_op` negotiation before heterogeneous placement can be trusted (gap 6);
5. membership, leases and failure detection — which is `ShareComputeCore`, already built;
6. authentication, which neither project has (F7).

Items 1–4 are upstream patches of roughly Stage 1/Stage 2 size and character. That is a real cost
and it should be priced in before committing, but it is a **known** cost against a working
cross-platform engine, which is not true of any alternative examined.

### Not verified

Nothing here was built or run. This is a source reading at one commit.

**The portability claim is the weakest link and should not be leaned on.** llama.cpp's *general* CI
builds for all five OSes, but that is not an RPC build. The only RPC-specific job is
`ubuntu-24-rpc`, it runs on Ubuntu arm64 alone, and it is marked `continue-on-error: true`
(`build-rpc.yml:37-38`) — so RPC is not gating upstream CI on any platform, and `GGML_RPC=ON` for
iOS, Android and Windows is unproven rather than merely untested-here.

Also unproven: whether an iOS app may run an `rpc-server` at all given no background execution (this
project's own F4 and §12.1 constraints); real throughput over Wi-Fi; and whether `--tensor-split`
carries the apportionment defects `StagePlanner` fixed (F5).

---

## F16 — First real Xcode build: F13 resolved, and two new facts

CI on macOS finally compiled this project (PR #7). Three results, all from the
`Build for iOS Simulator` job log at commit `31f914d`.

### F13 is answered: the branch-named-tag resolves cleanly

F13 recorded that `project.pbxproj` declares `kind = branch, branch = "ios-distrib-0.3.0"`
for a name that exists only as a **tag**, and flagged as unverified "whether Xcode currently
resolves this, fails, or silently falls back."

It resolves, without complaint, to exactly the commit the patches were written against:

```text
Checking out ios-distrib-0.3.0 (c53d302) of package 'mlx-swift'
  mlx-swift:    https://github.com/N1k1tung/mlx-swift    @ ios-distrib-0.3.0 (c53d302)
  mlx-swift-lm: https://github.com/N1k1tung/mlx-swift-lm @ ios-distrib-0.3.0 (e2b659f)
```

**So this is not a defect and Stage 3 does not need to fix it.** Repointing at the forks
should keep the same form rather than "correcting" it to `kind = revision`. F13's
consequence is withdrawn; its factual half — that the name is a tag — stands, and `c53d302`
is confirmed as what actually gets built.

### New: two packages claim the identity `mlx-swift`

```text
Conflicting identity for mlx-swift: dependency 'github.com/ml-explore/mlx-swift' and
dependency 'github.com/n1k1tung/mlx-swift' both point to the same package identity
'mlx-swift'. This will be escalated to an error in future versions of SwiftPM.
```

Something in the graph — most likely `mlx-swift-lm` or `swift-transformers` — depends on
**upstream** `ml-explore/mlx-swift` while the app pins the `N1k1tung` fork. SwiftPM currently
picks one and warns. Upstream says plainly that this becomes an **error** in a future SwiftPM.

This is a real time bomb under the fork strategy, and it is *not* caused by anything in this
project: it exists in the vendored app as shipped. It matters for `Patches/` because when it
does become an error, the build stops until the graph is de-duplicated — and every patch here
assumes the `N1k1tung` fork is what gets built. Not yet investigated: which dependency pulls
in upstream, and whether a `.package(name:)` override or a fork of the intermediate package is
the cheaper fix.

### `ShareComputeCore`'s Xcode wiring works

`README.md` warned that the `XCLocalSwiftPackageReference` with `relativePath = "../.."` might
be rejected because the package root is an *ancestor* of the `.xcodeproj`, and gave a fallback
procedure, noting it "could not be verified here — no macOS."

The build log shows `Linking ShareComputeCore.o`, and `xcodebuild -list` reports a
`ShareComputeCore` scheme alongside `Infer Ring` and `Ring`. **The reference resolves and the
target builds.** The fallback is not needed.

### The build failure: `fmt/src/format.cc`, cause not yet known

Both Xcode jobs died in the same place, and the first diagnosis was **wrong**:

| Job | Config | Arch | Result |
|---|---|---|---|
| iOS Simulator | Release | **x86_64** | `format.o` failed |
| macOS | Debug | **arm64** | `format.o` failed |

Seeing only the iOS log, this looked architectural — a generic simulator destination builds
every slice including x86_64, and MLX is Apple-Silicon-only. The macOS job then failed on
**arm64**, on the same file, in a different build configuration. So it is neither
architecture- nor configuration-specific, and `ARCHS=arm64` is **not** the fix.

`ARCHS=arm64` is kept anyway on its own merits — this app requires Apple Silicon, so an x86_64
slice is work with no consumer — but it should not be mistaken for a repair.

The actual cause is still unknown, because `xcpretty` swallowed every diagnostic: the failure
reported *which* compile command failed and never *why*. Both jobs now tee the raw log and grep
it for `error:` with context on failure, which is what should produce the answer. Standing
hypothesis, untested: `macos-latest` tracks a current Xcode, and the pinned mlx-swift vendors an
`fmt` old enough to disagree with that toolchain — the Metal sources in the same build already
warn `constexpr if is a C++17 extension`, which hints at a standard-level mismatch somewhere in
this vendored tree.

The lesson that does hold: **a CI whose purpose is to surface compile errors must not pipe them
through a prettifier that drops them.** One filtered log produced one confident wrong diagnosis.

### Still not established

Whether the Apple-side Swift **type-checks**. The failure above is in a vendored C++
dependency, reached before any of this project's Swift was compiled. `RingHealthMonitor`,
the `@MainActor` boundaries, and the drain path remain uncompiled.

---

## F17 — The pinned mlx-swift does not compile with current Xcode

This is a **project-level blocker, not a CI artifact**. It will hit anyone building infer-ring
today, on a real Mac as much as on a runner.

### The error

```text
fmt/include/fmt/format-inl.h:1389:33: error: call to consteval function
'fmt::basic_format_string<char, int>::basic_format_string<FMT_COMPILE_STRING, 0>'
is not a constant expression
 1389 |       out = fmt::format_to(out, FMT_STRING("p{}"),
5 errors generated.
```

Both Xcode jobs die here, on `arm64` and `x86_64`, in Debug and Release alike — it is neither
architecture- nor configuration-specific (correcting the first diagnosis in F16).

### The two halves

| | |
|---|---|
| Vendored `fmt` | **10.2.1** (`FMT_VERSION 100201`, January 2024), in `mlx-swift/Source/Cmlx/fmt` |
| Runner toolchain | **Xcode 26.6**, SDK `iPhoneSimulator26.5`, from the compile command line |

Newer clang tightened how `consteval` propagates through immediate-escalating functions, and
fmt 10.2.1 predates the fix. `FMT_STRING(...)` builds a compile-string type whose `consteval`
constructor is no longer accepted as a constant expression.

### Why the chosen workaround is legitimate rather than a hack

`FMT_CONSTEVAL` is defined behind `#ifndef` (`fmt/core.h:224`), and fmt itself defines it **empty**
for toolchains where consteval misbehaves — its own comment says *"consteval is broken in MSVC
before VS2019 16.10 and Apple clang before 14."* A no-consteval build is therefore a **supported
fmt configuration**, not a workaround invented here. Xcode 26.6 is simply another toolchain in
that category for this fmt version. The cost is losing compile-time format-string validation
inside MLX's own logging.

Both Xcode jobs now pass `OTHER_CPLUSPLUSFLAGS="-DFMT_CONSTEVAL="`. `OTHER_CPLUSPLUSFLAGS` rather
than `GCC_PREPROCESSOR_DEFINITIONS` deliberately: mlx-swift's `Package.swift` declares
`.define("MLX_VERSION", …)` and `.define("MLX_ENABLE_NAX", "1")` for `Cmlx`, which land in
`GCC_PREPROCESSOR_DEFINITIONS`, so overriding *that* from the command line would clobber them.

### The durable fix belongs in the fork

A CI build setting fixes CI. It does nothing for a developer opening the project in Xcode, who
hits the identical error. The real repair is in `joeydd032995-pixel/mlx-swift`, and it is exactly
the idiom `Patches/` already uses — a `Patches/mlx-swift/0002-…` alongside the existing `0001`.
Two candidates, neither yet attempted:

- **Define `FMT_CONSTEVAL` empty in `Package.swift`'s `cxxSettings`** — one line, matches what fmt
  already does for older Apple clang, and keeps fmt 10.2.1.
- **Bump vendored fmt to 11.x** — fixes it properly, but is a larger vendored-source change and
  MLX may use fmt 10 APIs.

### Consequences beyond the build

- **Any Stage 3 work needs a toolchain decision.** "Build it on a Mac" is not sufficient
  instruction while this stands; the Mac needs either an older Xcode or the fork patched.
- This is the **second** upstream-toolchain problem in the same vendored tree, after the
  conflicting `mlx-swift` package identity in F16. Both are latent in the app as shipped, and both
  are arguments for the fork strategy rather than against it.

### Not verified

That `-DFMT_CONSTEVAL=` actually clears the build — it is applied but not yet observed passing.
Whether anything downstream depends on fmt's compile-time checking is also unchecked; nothing in
this project calls fmt directly.

---

## F18 — First type-check of the Apple adapter: three real defects

The `FMT_CONSTEVAL` workaround (F17) cleared `fmt`, the build went the whole way through MLX, and
**for the first time this project's own Swift reached a compiler**:

```text
CompileSwift normal arm64 (in target 'Infer Ring' from project 'Infer Ring')
```

It found three defects, all in code written by this project, none of which `swiftc -parse` could
ever have caught. The session-long caveat — *"passes `swiftc -parse`, which is syntax only"* — was
not a formality; it was hiding exactly this.

| File | Error | Cause |
|---|---|---|
| `Screens/RingManagement/RingManagementView.swift:17` | `'let' binding pattern cannot appear in an expression` | missing `import ShareComputeCore` |
| `Services/ModelManager.swift:271` | same | missing `import ShareComputeCore` |
| `Services/RingHealthMonitor.swift:168` | `ambiguous use of 'init(name:priority:operation:)'` | bare `Task { }` |

### The misleading diagnostic

Both `let` errors point at column 31 — the `let` *inside* `if case .lost(let reason) = …` — and the
pattern syntax is correct. The real cause is that `RingHealth` lives in `ShareComputeCore`, which
neither file imports. Both reach its cases through leading-dot syntax (`.lost(…)`) without ever
naming the type, so a search for the type name finds nothing: **the defect is invisible to grep for
`RingHealth`.** Only two files of the seven touching ring health were missing the import, and both
happened to be the two that never spell the type out.

### A second defect at the same site, not reported

`ModelManager.swift:271` had `if case .lost(let reason) = health` where
`health` is `RingHealth?` — `MainActor.run { self?.ringHealthMonitor?.refreshHealth() }` yields an
optional through the chaining. A bare pattern does not match an optional; it needs `.lost(let
reason)?`. The compiler never said so because the missing import failed first. Fixed pre-emptively
rather than waiting for a second round.

### The `Task` ambiguity is real, not cascading

`RingHealthMonitor.swift` *does* import `ShareComputeCore`, so this one stands alone. Swift 6.2 added
`Task.init(name:priority:operation:)` beside `init(priority:operation:)`; every leading parameter of
both has a default, so a bare `Task { }` with only a trailing closure does not select one. Fixed with
explicit generic arguments — `Task<Void, Never> { … }` — since `announceLocalDrain()` is `async` and
non-throwing.

This is the concurrency-boundary code `findings.md` and every agent definition flagged as the
least-verified thing in the repository. The first compiler contact found a defect in it.

### Round two: two fixed, one replaced by a fix-induced error

The next build confirmed both missing-import errors gone and the optional-pattern fix holding.
One error remained, and it was **caused by the previous fix**:

```text
RingHealthMonitor.swift:167:23: error: conflicting arguments to generic parameter 'T'
('Void' vs. 'Task<Void, Never>')
```

`MainActor.assumeIsolated` is generic over its closure's result. Writing `Task<Void, Never> { … }`
as the closure's single expression makes it an implicit *return*, so `T` inferred as
`Task<Void, Never>` where the notification handler wants `Void`. The original bare `Task { }` had
the same shape; the ambiguity error simply fired first and masked it.

Fixed with `_ = Task<Void, Never> { … }`, which makes the body a statement. The discard is also
correct on its own terms: the drain is deliberately fire-and-forget, because awaiting a peer during
`willResignActive` risks suspension having told nobody.

Worth recording as a pattern rather than a one-off: **one compile round can only reveal the errors
that come first.** Three rounds so far have each uncovered a defect the previous round hid — the
architecture error masked the fmt error, fmt masked the imports, and the import fix let a
fix-induced generic-inference error surface. Budget several rounds, not one.

### Not verified

That this is the last of them. The compiler still stops early, and `RingCoordinator`, `DataServer`
and `InferringApp` all touch ring health without having been reached.

---

## F19 — The gated roles are not read-only, and inline dispatch does not restrict tools either

PR #8 justified answering the nine gated commands (`/linux-*`, `/windows-*`, `/android-*`) inline
rather than forking, with this rationale, repeated in **13 places**:

> The nine gated roles are read-only by construction, and that toolset *is* the gate.

Both halves are false.

**They are not read-only.** Every gated definition lists `Bash`:

```text
.claude/agents/linux-backend.md:4     tools: Read, Grep, Glob, Bash, TaskCreate, TaskUpdate
.claude/agents/linux-designer.md:4    tools: Read, Grep, Glob, Bash
.claude/agents/windows-designer.md:4  tools: Read, Grep, Glob, Bash
…all nine, same shape
```

`Bash` writes files. "No `Write`, no `Edit`" narrows the write surface; it does not close it.

**And the toolset never applies anyway.** A gated command runs **inline** — the body executes in the
caller's context, with the caller's tools. The gated agent is never spawned, so its `tools:` list is
not consulted on this path at all. The rationale appealed to a restriction that the chosen mechanism
does not invoke, to justify choosing that mechanism.

The design survives; the reason changes. Inline is not the safe option, it is the **visible** one: it
fails where an operator can see it, whereas forking on a *wrong* name lands on `general-purpose`
(which has `Write`) with no error anywhere.

The two dispatch paths have genuinely different properties, and conflating them is what produced the
original error:

| Path | Is the definition's `tools:` consulted? | What restrains it |
|---|---|---|
| `/linux-backend` — **inline** | **no.** The agent is never spawned; the body runs with the caller's tools | the instruction alone |
| `Agent(subagent_type: "linux-backend")` — **forked** | **yes** — no `Write`, no `Edit` | a real narrowing, but `Bash` still writes, so it is partial |

So the absent `Write`/`Edit` is defence in depth **on the forked path only**. On the inline path it
contributes nothing at all — there is no second layer there, and calling it one would repeat the
same mistake in weaker language. The gate that covers both paths is instructional: the definitions
state the block and refuse the work.

Surfaced by CodeRabbit on PR #8, though filed as "use `allowed-tools` to enforce it", which would
have made things worse: `allowed-tools` pre-approves tools for an invocation, it does not deny
others, so a read-only-looking list would have read as enforcement while enforcing nothing.

**Verified:** `grep -m1 '^tools:' .claude/agents/{linux,windows,android}-*.md` — nine files, all
carrying `Bash`. The `agentType === "general-purpose") ?? m[0]` fallback was read out of the shipped
CLI bundle at `/opt/claude-code/bin/claude`.

**Not verified:** no slash command has ever been typed in this repository. That a fork spawns the
named agent, that a gated command spawns nothing, that `background: false` blocks rather than
detaching, and that inline bodies really do inherit the caller's toolset are all read from the bundle
and from documentation, never observed at dispatch. Confirming them needs an interactive session.

**Consequence:** the claim is withdrawn from all 13 sites. Gated roles keep `Bash` — a blocked role
still needs to explore the repository to answer the platform questions it exists for.

Scope the conclusion to what the evidence supports. **The gate is not a permission boundary**: it is
instructional on the forked path and instructional-only on the inline one. That is narrower than
"nothing in the roster restricts anything" — a forked role's `tools:` list *is* consulted and does
narrow it, which is why `senior-architect` alone holds `Skill` and why the read-only roles cannot
`Edit`. What that narrowing does not do is stop a determined `Bash` call, so it should never be
described as preventing writes.

`scripts/validate-agents.py` pins the mechanical half it *can* check: that `agent:` resolves to a
real definition, and that gated skills never fork.
