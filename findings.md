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

---

## F20 — Stage 3's adapter could not compile against the dependency the app actually resolves

Stage 3 (PR #9) wrote `MLXManager.teardown()` around `DistributedGroup.finalize()`. Both Xcode jobs
failed on the same line:

```text
Apps/InferRing/Ring/Manager.swift:58:32: error: type 'DistributedGroup' has no member 'finalize'
```

`finalize()` is added by `Patches/mlx-swift/0001-free-group-and-expose-finalize.patch`. The app does
not build against a patched MLX:

```text
Apps/InferRing/Infer Ring.xcodeproj/project.pbxproj:680  repositoryURL = ".../N1k1tung/mlx-swift"
Apps/InferRing/Infer Ring.xcodeproj/project.pbxproj:682  branch = "ios-distrib-0.3.0"
```

and `Patches/README.md:21-22` says the three forks "are where these patches are **meant** to land" —
they had not landed. So the symbol existed in a patch file and nowhere a compiler could reach.

**The patches being kept as files rather than applied is deliberate** (`Patches/README.md`: a patch
that fails to apply tells you upstream moved, whereas a fork silently diverges). That decision is
still right. What it also means, and what went unnoticed, is that **no code in this repository may
call a patched API until the forks are patched and the project repointed** — a constraint that had
never bitten, because until Stage 3 nothing had tried.

### Why nothing local caught it

`swiftc -parse` resolves no imports, so it cannot know whether a member exists — the same gap F18
recorded, reached from the other side. F18 was three type errors hidden behind a missing import;
this is a type error hidden behind an unbuilt dependency. Both are invisible to every check that
runs in this container, and both surfaced on the first real compile.

Worth noting the pattern rather than just the instance: this project has now been wrong twice in the
same way, and both times the wrongness lived in the gap between "syntax-checks here" and "compiles
there". The macOS CI job is the only thing that closes it.

**Verified:** the CI failure above, at commit `4dbff7d`, on both `Build for iOS Simulator` and
`Build and analyse the Infer Ring scheme`. The `project.pbxproj` lines were read directly. One call
site, confirmed by `grep -rn finalize Apps/InferRing/` — every other occurrence is prose.

**Not verified:** whether the three forks currently exist in a state the patches apply to. They have
not been attached to a session or inspected; `Patches/README.md`'s claim that they exist is the only
evidence, and it is the same sentence that says the patches have not landed.

**Consequence:** the call is gated behind `#if MLX_HAS_FINALIZE`, with `MLXManager.canReform` so
callers ask before attempting and `RingWatchdog.reformationUnavailable` so an absent capability does
not consume the retry budget. Against stock MLX the app degrades to exactly Milestone 1, which is
what stock MLX can actually do. Landing the patches on the forks and repointing `project.pbxproj`
moves from "Phase 2, later" to **Phase 2's first item**, because every remaining MLX claim depends
on it.

---

## F21 — iOS can host a llama.cpp RPC server in principle, and F15 understated upstream's RPC coverage

The Phase 5 gate, answered ahead of Phases 4–5 so a failure would re-plan them rather than surface
last. Read against `ggml-org/llama.cpp` at `d86c7d6`; F15 read `9afff1b`, and upstream has moved.

### The enabling fact

`ggml_backend_rpc_start_server` is a **public library API**, not merely the `rpc-server` binary:

```text
ggml/include/ggml-rpc.h:27  GGML_BACKEND_API void ggml_backend_rpc_start_server(
                                const char * endpoint, const char * cache_dir,
                                size_t n_threads, size_t n_devices, ggml_backend_dev_t * devices);
```

This is what makes iOS hosting conceivable at all. iOS forbids shipping a *separate executable*, so
a standalone `rpc-server` process is out — but linking the server into the app and calling it on a
background thread is ordinary. The constraint was never "iOS cannot serve", it was "iOS cannot spawn
a second binary", and this API sidesteps that.

Nothing in the RPC sources is iOS-hostile in the usual ways: `grep -rnE '\bfork\(|posix_spawn|daemon\(|execv'`
over `ggml/src/ggml-rpc/` returns **nothing**. The server does not fork per connection, which would
have been fatal — iOS kills forked children.

### F15 was wrong about upstream CI, in llama.cpp's favour

F15 said: *"The only RPC-specific job is `ubuntu-24-rpc` … so RPC is not gating upstream CI on any
platform."* At `d86c7d6` that is **false**. `-DGGML_RPC=ON` appears in:

| Workflow | Platform | Gating? |
|---|---|---|
| `build-apple.yml:65` | macOS arm64 | yes |
| `build-apple.yml:102` | macOS x64 | yes |
| `build-android.yml:148` | Android arm64-v8a | yes |
| `build-cpu.yml`, `build-cuda-windows.yml` | Windows (llvm, CUDA) | yes |
| `build-ibm.yml:80` | s390x etc. | yes |
| `build-rpc.yml` — `ubuntu-24-rpc` | Ubuntu arm64 | **no** — still `continue-on-error: true` |

So F15's *specific* claim about `build-rpc.yml` survives, and its *general* conclusion does not:
RPC is built and gating on four of this project's five target platforms. The portability caveat F15
called "the weakest link and should not be leaned on" is considerably stronger than recorded.

### Except on the one platform Phase 5 needs

The iOS job (`build-apple.yml:112`, `macos-latest-ios-xcode`) sets `-DCMAKE_SYSTEM_NAME=iOS` but
**does not pass `-DGGML_RPC=ON`** — it builds with tools, server, tests and examples all off. So iOS
is the single target platform where an RPC build is genuinely unproven upstream, and it is the one
this project chose for its first ring.

Nothing suggests it *cannot* build. `GGML_RPC` is a ggml backend rather than a tool, so the iOS
job's `LLAMA_BUILD_TOOLS=OFF` does not exclude it, and `ggml/src/CMakeLists.txt` gates `GGML_RPC` on
no platform condition at all. It simply has not been switched on there.

**Verified:** the header signature, the absent `fork`/`exec`/`daemon`, and every workflow line
above, read at `d86c7d6`.

**Not verified:** that `GGML_RPC=ON` actually *compiles* for iOS — this container has no Xcode or
iOS SDK, so only a macOS runner can answer it. Also unproven: throughput over Wi-Fi, and behaviour
under Jetsam. And the app suspends when backgrounded (F4 — no `UIBackgroundModes`), so a hosted
server dies with it; that is a product constraint rather than a build one, and Milestone 1's
drain-on-background already models exactly that departure.

**Consequence: Phase 5 is not resting on a false premise, but its iOS half is unproven at build
time.** The gate does not fail, so Phases 4–5 stand as planned. What it adds is one cheap CI job —
build ggml for iOS with `GGML_RPC=ON` on a macOS runner — which should land before any iOS node work
begins, on the same logic that made both MLX spikes worth running first.

### F15's technical claims re-verified at `d86c7d6`

Since F15's *CI* claim went stale, its load-bearing ones were re-read at the same commit rather than
trusted. **All four still hold**, so Phase 4's scope is confirmed rather than reduced:

| F15 claim | At `d86c7d6` |
|---|---|
| 19 `RPC_STATUS_ASSERT` sites, each a `GGML_ABORT` | still 19 |
| `ggml_abort` cannot be intercepted | holds — a `g_abort_callback` runs first, then `abort()` unconditionally |
| no `SO_RCVTIMEO` / `SO_SNDTIMEO` in `ggml/src/ggml-rpc/` | still absent |
| `add_server`'s `reg_map` is an owning static, no removal path | `ggml-rpc.cpp:2014`, and no `remove_server` in the header |
| 16-device ceiling | `GGML_SCHED_MAX_BACKENDS` 16 (`ggml-backend.cpp:753`), `llama_max_devices()` 16 (`src/llama.cpp:85`) |

One near-miss worth recording, because it is the third instance of the same failure mode. The first
pass checked `ggml_abort` with `grep -A6` and concluded the unconditional `abort()` was **gone** — it
is simply further down than six lines, past the callback block added since F15. Reading the whole
function showed F15 was right all along, and had already described the callback-then-abort ordering
exactly.

A too-narrow window produced a confident wrong answer, which is what one filtered `xcodebuild` log
did earlier in this project and what `swiftc -parse` did twice (F18, F20). The correction is the
same each time: widen to the whole unit before believing a negative result, especially a convenient
one — "upstream already fixed it" would have quietly removed Phase 4.2 from the plan.

---

## F22 — mlx-swift vendors its own copies of the mlx-c headers, so patching the submodule declares nothing

Found by the first CI run that resolved the patched fork (PR #11). Both Xcode jobs failed with two
errors and no others:

```text
SourcePackages/checkouts/mlx-swift/Source/MLX/DistributedGroup.swift:22:9:
    error: cannot find 'mlx_distributed_group_free' in scope
SourcePackages/checkouts/mlx-swift/Source/MLX/DistributedGroup.swift:69:9:
    error: cannot find 'mlx_distributed_finalize' in scope
```

Both are the symbols `Patches/mlx-c/0001` exports, called from `Patches/mlx-swift/0001`.

### Why the submodule patch was not enough

mlx-swift does not compile against the mlx-c submodule's headers. It keeps **its own copies**:

```text
Source/Cmlx/include/mlx/c/distributed_group.h          991 bytes, a regular file, not a symlink
Source/Cmlx/include-framework/mlx-c-distributed_group.h  996 bytes, same but flat-named
Source/Cmlx/mlx-c/mlx/c/distributed_group.h            the submodule -- patched, and unread
```

The `Cmlx` module map exposes exactly one header:

```text
Source/Cmlx/include/module.modulemap:  module Cmlx { header "mlx.h" }
Source/Cmlx/include/mlx.h           -> "mlx/c/linalg.h" -> "mlx/c/distributed_group.h"   (the copy)
```

So Swift's entire view of the C API comes from `include/`. `mlx-c/mlx/c/distributed_group.cpp` *is*
compiled from the submodule, so the **definitions** were present and would have linked — only the
**declarations** were missing. The two copies are otherwise byte-identical to the submodule header,
differing only in `include-framework`'s `#include <Cmlx/mlx-c-stream.h>`.

`include-framework` is excluded from the SwiftPM target on Linux only (`Package.swift:7-11`), so on
Apple platforms it is part of the target. Both copies are patched, to avoid leaving a divergent one
as the next trap.

### Why every earlier check passed

This is the sharp part, and it generalises beyond this bug.

- `g++ -fsyntax-only` on `mlx-c/mlx/c/distributed_group.cpp` compiles the **submodule** header
  directly. It proves the definitions exist. It says nothing about what mlx-swift exposes.
- The negative control in `Patches/README.md` step 2 tests mlx-c against an unpatched *mlx* header.
  Correct, and orthogonal.
- `swiftc -parse` resolves no imports (F18).
- Verifying the fork resolves — full clone, `fsck`, submodule checkout, patched symbols present —
  confirmed the submodule header had them. It checked the wrong file.

Every layer tested a real thing, and the union still had a hole exactly where two file trees are
supposed to mirror each other and one is silently authoritative. **A vendored copy of a dependency's
header is a fork of that dependency**, whatever the directory is called.

### The check that closes it

`Patches/README.md` verification step 6: a C translation unit that reaches both symbols through
`mlx.h`, exactly as Swift does, compiled with `-Werror=implicit-function-declaration`. It passes
with `0002` and fails without. The `-Werror=` is load-bearing — plain C accepts both as implicit
declarations and *passes*, reproducing the same false green in the harness that the harness exists
to prevent.

### What this does not undermine

The macOS job got much further than any before it. `Cmlx` builds before `MLX`, and the log shows
Swift `MLX` compilation well underway, so the patched MLX C++ — `ring.cpp`, `distributed.cpp` and
`distributed_group.cpp`, all four patches' C and C++ — **compiled for arm64-apple under a real Apple
toolchain**, for the first time in this project's life. SwiftPM also resolved the patched fork past
the `mlx-swift` identity conflict F16 recorded, which had been the other candidate failure. The
remaining defect is two missing declarations, not the patch set.

Still unrun: nothing here has executed. Linking, runtime behaviour and a real ring remain untested.

---

## F23 — A single machine cannot exercise the memoisation that Stage 2 exists to defeat

Established while scoping Phase 2.2's Swift lifecycle test, by reading the patched sources at fork
`6c15a2e`. It changes what that test can honestly assert, so it is worth recording before the test
is written rather than after it passes for the wrong reason.

### `ring::init` needs two environment variables, or it declines

```text
mlx/distributed/ring/ring.cpp:  const char* hostfile = std::getenv("MLX_HOSTFILE");
                                const char* rank_str = std::getenv("MLX_RANK");
                                if (!hostfile || !rank_str) { ... return nullptr; }
```

`strict=false` returns `nullptr` rather than throwing. A CI runner has neither variable set, so ring
declines, and mpi and jaccl decline too.

### The no-backend path does not cache under `"any"` — and that is the whole problem

`distributed::init` ends:

```cpp
if (group == nullptr) {
  group = std::make_shared<detail::EmptyGroup>();
} else {
  cache.insert({"any", group});      // only on the real-backend path
}
cache.insert({std::move(bk_), group});
```

With `bk == "any"` and every backend declining, `bk_` has been reassigned down the chain and ends as
`"jaccl"`. So the `EmptyGroup` is cached under `"jaccl"` only. A second `init(false, "any")` misses
the cache — `find("any")` was never populated — walks the chain again and builds a **fresh
`EmptyGroup`**.

**Consequence: on a bare runner, "a re-`init()` returns a genuinely new group" is true whether or not
`finalize()` was ever called.** That assertion is the one which pins the defect this whole milestone
exists to fix, and on a single machine it would pass vacuously. Writing it there would produce a
green check that proves nothing — the precise failure mode this project has already been caught by
three times (F18, F20, F22).

### A one-rank ring is not a workaround

Setting `MLX_HOSTFILE` to a single-entry file and `MLX_RANK=0` does not give a real backend:

```text
mlx/distributed/ring/ring.cpp:441   int connect_to = (rank_ + 1) % size_;   // (0+1) % 1 == 0
```

Rank 0's peer is itself, and `rank_ < connect_to` is false, so it takes the else branch and calls
`make_connections` **before** `accept_connections` creates the listener. It connects to an address
nothing is listening on. It fails rather than hangs — `TCPSocket::connect` is bounded at
`CONN_ATTEMPTS = 5` with `CONN_WAIT = 1000` ms — so the cost is a ~5 s stall and a throw, not a
wedged runner. But it is not a ring.

A real ring on one machine needs **two processes** on `127.0.0.1` at different ports, with
`MLX_RANK` 0 and 1. That is constructible, and it is what a genuine single-host test of the
memoisation would require.

### What a single-process test can therefore honestly assert

- Both C symbols **link** — although the macOS build already proves this, so a test adds little.
- ARC calls `mlx_distributed_group_free` when the last `DistributedGroup` reference drops.
- `finalize()` returns `false` while a handle is held and `true` once every one is released. This
  works against the cached `EmptyGroup` and is a real test of the use-count check that review missed
  and the C++ harness caught.

What it **cannot** assert without a second process is the memoisation defeat itself. That belongs on
the hardware list with the rest, not in a single-runner test dressed up as covering it.

### Not verified

This is a reading of the sources, not a run. Nothing here has been executed on Apple hardware, and
the claim that two local processes *would* form a ring is inference from the connect/accept ordering
— plausible, and untested.

---

## F24 — F17's fmt workaround is confirmed, and now needed in a third place

Two things F17 left open are now settled, and a third has emerged. Evidence is the
`DistributedGroup lifecycle on macOS` job at `88494c1`, plus the two Xcode jobs' history since
`143665d`.

### F17's untested workaround is verified

F17 recorded, under *Not verified*: "That `-DFMT_CONSTEVAL=` actually clears the build — it is
applied but not yet observed passing." It has now passed on both Xcode jobs across many runs,
including the green end-to-end build of the patched MLX. **The workaround works.**

### The diagnosis reproduces exactly, under a different build system

The new SwiftPM job carried no such flag, and failed with F17's error verbatim:

```text
fmt/include/fmt/format-inl.h:1389:33: error: call to consteval function
'fmt::basic_format_string<char, int>::basic_format_string<FMT_COMPILE_STRING, 0>'
is not a constant expression
 1389 |       out = fmt::format_to(out, FMT_STRING("p{}"),
5 errors generated.
```

Same file, same line, same five errors. So it is not an Xcode-specific quirk: it is the vendored
fmt against a current clang, whatever drives the compiler.

**SwiftPM spells the flag differently.** `swift test -Xcxx -DFMT_CONSTEVAL=`, against the Xcode
jobs' `OTHER_CPLUSPLUSFLAGS="-DFMT_CONSTEVAL="`. Anyone adding a fourth consumer needs to know that
the setting does not carry across build systems by name.

### Incidentally: the patched files compile before fmt fails

The failing log reaches `[207/218] Compiling distributed_group.cpp` and
`[208/218] Compiling distributed.cpp` before dying in `format.cc`. The Stage 1 and Stage 2 sources
are not implicated — useful to know, because "the MLX build failed" invites the assumption that the
patches did it.

### The new fact: three consumers now carry the same flag

`ios.yml`, `objective-c-xcode.yml` and now `mlx-lifecycle.yml`, each with its own spelling of the
same define — **and a developer opening the project in Xcode carries none of them and hits the
error**. That is exactly the situation F17 predicted when it said a CI build setting fixes CI and
nothing else.

F17's recommended durable fix therefore stands, and is now better justified than when written:
define `FMT_CONSTEVAL` empty in the fork's `Package.swift` `cxxSettings` for `Cmlx`, as
`Patches/mlx-swift/0003`, then delete the flag from all three jobs. One line, in the one place every
consumer already resolves.

It is **not** done here. Doing it means another push to the fork and a full CI cycle, and folding
it into the PR that introduces the lifecycle test would mix two unrelated changes — and leave an
unlanded patch file sitting in `Patches/`, which is the state that produced F20.

### Not verified

That `0003` would work. Nothing has tried defining `FMT_CONSTEVAL` through `cxxSettings` rather than
on the command line, and `Package.swift` already sets `MLX_VERSION` and `MLX_ENABLE_NAX` for `Cmlx`
— F17 notes that clobbering those was the reason the Xcode jobs use `OTHER_CPLUSPLUSFLAGS` rather
than `GCC_PREPROCESSOR_DEFINITIONS`. A `cxxSettings` `.define` appends rather than replaces, so this
should be safe, but "should be" is not a build.

---

## F25 — llama.cpp RPC works, and dies uncatchably: the first execution-layer result this project has *run*

T1, run in this container against llama.cpp `9d57ce4`. Every execution-layer claim before this one
was a reading of source or a harness *mirroring* code that could not be compiled here, because MLX is
Apple-only. llama.cpp builds and runs on x86_64 Linux, so this drives the real binaries.
Harness: `Spikes/llamacpp-rpc/run.sh`.

### The topology is real

Two `ggml-rpc-server` processes, one `llama-cli` client, Qwen2.5-0.5B-Instruct Q4_K_M:

```text
- RPC0 : 127.0.0.1:50300 (16075 MiB free)
- RPC1 : 127.0.0.1:50301 (16075 MiB free)
     26 assigned to device RPC0
     24 assigned to device RPC1
```

Layers genuinely split across two processes, generation completes, exit 0. This is the first
positive capability result on the portable path, and it is what makes Phases 4–5 worth doing at all.

### Killing a peer aborts the client in ~350 ms

Reproducible 3/3, killed once real output was flowing:

| run | outcome | detect → death |
|---|---|---|
| 1 | ABORT (SIGABRT) | 365 ms |
| 2 | ABORT (SIGABRT) | 361 ms |
| 3 | ABORT (SIGABRT) | 349 ms |

```text
ggml-rpc.cpp:509: Remote RPC server crashed or returned malformed response
E send failed (bytes_sent=0, size_to_send=8)
```

`:509` and `:519` both appear, depending on whether `set_tensor` or `get_tensor` was in flight. Both
reach the same one-line macro:

```c
ggml-rpc.cpp:30:  #define RPC_STATUS_ASSERT(x) if (!(x)) GGML_ABORT("Remote RPC server crashed or returned malformed response")
```

**18 call sites across 16 functions** at this commit. F15 said 19; the count has drifted by one, so
use this number rather than restating F15's.

### This corrects F15's emphasis, and reorders Phase 4

F15 predicted a **hang**, reasoning from the absence of `SO_RCVTIMEO`/`SO_SNDTIMEO`. That absence is
confirmed — still **zero occurrences** in `ggml/src/ggml-rpc/` at HEAD — but it is not what fires
first on a hard peer kill. The *send* path notices immediately (`bytes_sent=0` on a dead socket) and
aborts long before any receive could block.

So the primary failure mode is **abort, not hang**, and that is worse for this project:

- A hang is survivable by a supervisor: something outside can notice and restart.
- `abort()` **cannot be caught**. No exception, no error return, no chance to re-form. The process is
  gone, taking every other node's session state with it.

Detection is not the problem — llama.cpp detects a dead peer promptly and correctly. What it does
next is unrecoverable by construction. **Phase 4.2 (error return in place of `GGML_ABORT`) therefore
comes before Phase 4.1 (bounded waits)**, reversing the order F15 set. Bounded waits still matter,
for the cases this test does not cover.

Note the symmetry with MLX: Stage 1 there turned a hang into a thrown error. Here the equivalent work
turns an abort into a returned error. Same destination, opposite starting point.

### Two false readings caught before they became results

Both are recorded because each produced a *confident wrong answer* first, and the second is the more
dangerous kind — it fails green.

- **`llama-cli` defaults to conversation mode.** Without `-st` and closed stdin it generates
  correctly, then waits at a `>` prompt. The bounded run times out and looks exactly like a hang.
  Case A was initially read as "RPC hangs" when it had worked perfectly.
- **A small model finishes before the kill lands.** stories15M reaches EOS in about a second, so the
  peer was killed after the run was already over and the harness reported "clean exit" — a pass that
  tested nothing. Fixed with `--ignore-eos`, a larger model, and waiting for real output before
  killing; the script now reports "window too short, not a result" rather than a verdict.

### Also confirmed at HEAD

- Upstream's only RPC CI job is still `continue-on-error: true` and runs `ctest -L main` — it never
  starts two processes, so nothing upstream exercises what this measured. F15's characterisation
  stands.
- The server target is **`ggml-rpc-server`** under `tools/rpc/`, not `rpc-server` under
  `examples/rpc/` as F15 and F21 have it. Renamed and moved since those were written.

### Not established

Both peers are on loopback, so there is no real network hop — no latency, no MTU, no firewall, and
crucially no *silent* connection loss. The abort is observed only for a hard `SIGKILL`; a graceful
departure and a network that drops without closing the socket are different paths, and they are
exactly where the missing socket timeouts would bite instead. Nothing here touches iOS, and nothing
re-forms after the abort because there is no surviving process to re-form from.

## F26 — Phase 4.2 cannot be done as planned: most abort sites have no error channel

Read at llama.cpp `9d57ce4`, `ggml/src/ggml-backend-impl.h` and `ggml/src/ggml-rpc/ggml-rpc.cpp`.
This corrects the *shape* of the plan's Phase 4.2, not just its arithmetic. F25 already corrected the
count from 19 to 18; the larger problem is that "replace the aborts with error returns" is not
available for most of them.

### 13 of the 18 sites have no status return; 12 genuinely need the flag

> **Corrected.** This section first said "10", which matches neither the table below nor the
> arithmetic — 5 + 7 + 6 = 18, so 13 lack a status return. Caught in review on PR #14. The
> correction *understated* the scope of Phase 4.2, so it mattered. One of the 13 (`alloc_buffer`)
> does have a usable channel — see the note after the tables — leaving 12 that need the sticky flag.

**Can return one today — 5 sites, genuinely easy:**

| Line | Function | Returns |
|---|---|---|
| 345 | `negotiate_hello` | `bool` |
| 468 | `ggml_backend_rpc_buffer_init_tensor` | `enum ggml_status` |
| 538 | `ggml_backend_rpc_buffer_cpy_tensor` | `bool` |
| 732, 739 | `ggml_backend_rpc_graph_compute` | `enum ggml_status` |

**Declared `void` by the ggml backend vtable — 7 sites:**

| Line | Function | vtable declaration |
|---|---|---|
| 394 | `..._buffer_free_buffer` | `void (*free_buffer)` — impl.h:43 |
| 483 | `..._buffer_memset_tensor` | `void (*memset_tensor)` — impl.h:49 |
| 496, 509 | `..._buffer_set_tensor` | `void (*set_tensor)` — impl.h:50 |
| 519 | `..._buffer_get_tensor` | `void (*get_tensor)` — impl.h:51 |
| 548 | `..._buffer_clear` | `void (*clear)` — impl.h:59 |
| 823 | `get_device_memory` | feeds `void (*get_memory)` — impl.h:168 |

**Return a type with no failure value — 6 sites:** `..._buffer_get_base` (`void *`, impl.h:45),
`..._buffer_type_alloc_buffer` (`ggml_backend_buffer_t`, where NULL is already a legitimate
non-error), `get_alignment` / `get_max_size` / `..._buffer_type_get_alloc_size` (`size_t`),
`ggml_backend_rpc_get_device_count` (`uint32_t`).

**The site F25 measured 3/3 — `ggml-rpc.cpp:509` — is one of the `void` ones.** So the plan's
version of 4.2 would not have addressed the failure that actually fires.

**One of the six sentinel sites is not as bad as the rest.** `..._buffer_type_alloc_buffer` returns
`ggml_backend_buffer_t`, and NULL is a genuine failure signal that ggml's allocator already checks —
`ggml_backend_buft_alloc_buffer` (`ggml-backend.cpp:38`) passes it straight through to callers that
test it. So that site can report failure today without any new machinery. The other five cannot:
`get_base` returns `void *` that callers do not check, `get_alignment` / `get_max_size` /
`get_alloc_size` return `size_t` with no reserved value, and `get_device_count` returns `uint32_t`
where 0 already means "no devices".

**Final arithmetic: 5 already return a status, 1 more can signal via NULL, and 12 need the sticky
flag.**

These signatures belong to `ggml_backend_buffer_i` and `ggml_backend_i`, implemented by CPU, CUDA,
Metal, Vulkan, SYCL and every other backend. Turning `void set_tensor` into `bool set_tensor` is a
change to all of ggml, not an RPC-local patch.

### The design that works is this project's own precedent

Load-bearing fact #3 already says it: an exception must never escape a dispatched MLX task, so
failures are *recorded* and rethrown later from a thread that can carry them. Same structure here:

1. A sticky per-connection failure flag — natural home is `socket_t`, which both
   `ggml_backend_rpc_buffer_context` and `ggml_backend_rpc_context` already reach.
2. The `void` and sentinel sites set it, log, and return. They never `abort()`.
3. `ggml_backend_rpc_graph_compute` converts it: it returns `enum ggml_status` and runs every token.
   The flag must be checked **at entry**, not only after its own sends, so a peer that died during
   `set_tensor` fails the graph *before* it computes on stale data.

Detection latency: at most one token boundary.

### The open risk, which mirrors load-bearing fact #4

`get_tensor` (line 519) is the dangerous one. On failure the caller's `data` buffer is left untouched
or partly written and the caller reads it anyway. If no `graph_compute` follows, the flag is never
converted and the failure is **silent** — corruption in place of a visible abort. That is exactly the
shape of fact #4, where `std::future::wait()` discarding an exception produced silent corruption
"instead of a hang — worse, because a hang is visible."

So the patch is not safe on its own. It needs either a public query
(`ggml_backend_rpc_connection_failed(...)`) that the host's token loop checks, or a deterministic
zero-fill on failure. **Undecided — do not write the patch until it is**, and pin it with a test
mirroring the *unpatched* silent-corruption path, per the rule that tests pin defects and not just
fixes.

### Consequence

4.2 stays ahead of 4.1 — F25's reordering holds, because abort is still worse than hang. But 4.2 is
now "record-and-convert behind a sticky flag", not "replace 18 aborts with error returns", and it
carries a prerequisite decision that did not exist in the plan.

### The propagation path is confirmed — the design's load-bearing assumption holds

This was written as an open risk and then checked, because the whole design rests on it. A failing
`graph_compute` does reach the host as an ordinary error return, with no abort anywhere on the path:

| Step | File and line | Behaviour |
|---|---|---|
| RPC backend returns non-`SUCCESS` | `ggml-rpc.cpp:720` | the conversion point this design proposes |
| Scheduler propagates | `ggml-backend.cpp:1732` and `:1754` | `if (ec != GGML_STATUS_SUCCESS) { return ec; }` — both the plain and `callback_eval` paths |
| llama.cpp logs and returns | `llama-context.cpp:2490–2496` | `LLAMA_LOG_ERROR(...)`, `return status` |
| `process_ubatch` gives up cleanly | `llama-context.cpp:1385–1390` | `ret = status; return nullptr;` |
| `llama_decode` maps it | `llama-context.cpp:1471` (and `:1844` for encode) | `case GGML_STATUS_FAILED: return -3;` |

So a peer that dies mid-generation would surface to the host application as `llama_decode() == -3` —
catchable, recoverable, and exactly what `GGML_ABORT` denies today. **Phase 4.2 is viable**, and the
conversion point is the right one.

### Not established

Nothing here was compiled or run — this is a reading of source. The sticky flag has not been
implemented, so while the *propagation* path is now verified by reading it end to end, the claim that
recording at the `void` sites and checking at `graph_compute` entry is *sufficient* remains reasoned
rather than measured. The `get_tensor` silent-corruption risk above is untouched by this: it escapes
precisely by never reaching `graph_compute` at all, so a verified propagation path does not close it.

## F27 — The RPC transport halves prompt processing before any network is involved

First throughput measurement on the portable path, run in this container.
Harness: `Spikes/llamacpp-rpc/throughput.sh`. Qwen2.5-0.5B-Instruct Q4_K_M, 128 generated tokens,
3 repeats, median reported, on a 4-core Xeon @ 2.10GHz with **no GPU**.

### Why this was measured now

`Apps/InferRing/README.md` reports Mac + iPhone at **−12% token generation and +11% prompt
processing** — cross-device pooling that is genuinely good. But the same README warns that "Wi-Fi and
pre-TB5 over RDMA connections will result in sharp performance decline" and recommends a USB3.2
cable. An iPhone and a Windows PC have no such cable between them, so the near-term target is the
configuration upstream warns against, and it had never been measured on any link at any speed.

### Result

> **Corrected — the first published table's two-peer row measured one peer.** The second
> `ggml-rpc-server` failed to bind (`Failed to create server socket`) and *stayed alive*, so a PID
> check saw a healthy process while llama.cpp silently registered a single device and put all 50
> tensors on it. The harness then reported it as a two-peer result. Numbers below are the re-run with
> a verified `2/2` split; the correction and its cause are written up at the end of this finding.

| configuration | PP t/s | TG t/s | wall ms | peers |
|---|---|---|---|---|
| local, no RPC | **217.3** | 24.9 | 6239 | — |
| 1 peer, loopback | 112.5 | 27.6 | 7148 | 1/1 |
| 2 peers, loopback | 117.7 | 27.4 | 7453 | **2/2** (26 + 24 layers) |

Per-run spread from the original run, which is what makes the deltas trustworthy rather than a
median artifact (the `local` and `1 peer` rows were always genuine; only the two-peer row was not):

```text
local     PP 205.9 / 202.6 / 200.1     TG 25.1 / 24.5 / 24.6
1 peer    PP 107.1 / 115.4 / 109.9     TG 28.0 / 26.8 / 27.5
2 peers   PP 113.7 / 111.4 / 104.6     TG 27.3 / 27.7 / 26.6
```

The PP bands do not overlap and neither do the TG bands, so both effects are real.

**Three conclusions.**

1. **Prompt processing halves (−46%) on loopback**, with no network at all. That is pure transport
   and serialisation cost, and it is the metric where the MLX path *gained* 11%. Opposite sign,
   which matters: whatever makes MLX's prefill faster across devices is absent here.
2. **A second peer is free.** 1 peer and 2 peers are indistinguishable on every metric. Per-peer
   overhead is negligible; the cost is the first hop.
3. **Wall clock rises ~30%**, which is model upload to the server — the part a slow link punishes
   hardest and which the t/s figures deliberately exclude.

### The TG number is an artifact, and must not be read as a transport win

Token generation is *higher* under RPC (26.6–28.0 vs 24.5–25.1). This is not evidence that adding a
network hop makes generation faster. Both processes share one 4-core box: with `-ngl 99` all 25
layers move to the RPC device, so the server's threadpool does the matmuls while the client thread
blocks on the socket, instead of one 4-thread process doing compute, sampling and tokenisation
together. Verified the offload is genuine rather than silently falling back to local compute —
`50 assigned to device RPC0`, `offloaded 25/25 layers to GPU`, and 1918 lines of server-side work.

The honest reading is that on loopback the per-token transport cost is **below this host's
measurement floor**, not that it is negative.

### Not established

Loopback is not a network: no MTU, no contention, no radio, no latency. CPU-only, 4 cores, a 0.5B
model — the absolute numbers transfer to nothing. What transfers is the *shape*: PP is the
transport-sensitive metric and is therefore the leading indicator for T2, and per-peer scaling is
cheap. No Wi-Fi, no cross-machine hop, and no iOS has been measured.

### Three defects in this harness, each of which produced a believable wrong number

All three are the same species — **a check that passes while measuring the wrong thing** — and they
were found in sequence, each by fixing the one before it. Two came out of code review on PR #14.

**1. No `-v`, so nothing proved the RPC devices were used.** The first run's logs carried no
`assigned to device RPC0` lines at all, so a silent fallback to local compute would have been
indistinguishable from a good result. Fixed by making `-v` permanent and counting offloaded tensors.

**2. A failed repetition scored as zero and was folded into the median.** `run_one` returned
`pp=tg=0` on a non-zero exit and `measure` averaged it in; with `REPEATS=2` a single failure would
have halved the reported throughput while looking entirely plausible. Now any failed repetition
invalidates the whole row.

Testing that fix produced a **worse** finding than the one it fixed: with an unreachable endpoint,
`llama-cli` **does not fail at all**. It exits 0, computes locally, and reports 199 t/s — a number
indistinguishable from success. The dangerous case was never the non-zero exit; it was the zero one.

**3. A tensor total cannot tell a split ring from a single node — and this one had already
corrupted a published result.** With two endpoints and one dead, every tensor lands on the survivor
and the total still reads 50. The harness now counts *distinct* RPC devices and requires one per
endpoint (`2/2`), which is what exposed the two-peer row as a one-peer measurement.

The root cause was `sleep 2` and a PID check. `ggml-rpc-server` prints `Failed to create server
socket` and **keeps running**, so the process exists, the PID is valid, and only the absence of a
listener gives it away. Ports were also fixed constants, so a socket still in `TIME_WAIT` from an
earlier run was enough to cause it. `serve` now derives ports from the PID and polls `/dev/tcp`
until the port genuinely accepts a connection, aborting the run if it never does.

**What the correction changes, and what it does not.** The re-run with a verified `2/2` split gives
PP 117.7 against 112.5 for one peer, so *"a second peer is free" survives as a conclusion* — but it
was **not supported by evidence when first published**, and that distinction is the point. The
`−46% PP` result is unaffected: the `local` and `1 peer` rows were always genuine, and the corrected
run reproduces the gap (217.3 → 112.5).

## F28 — SwiftPM manifests are semantically checkable on Linux, and `-parse` is not enough

Cost one CI cycle to learn, on `5047ff6`. Both macOS jobs failed in **17 seconds** — far too fast to
be an MLX build — with:

```text
Package.swift:32:5: error: argument 'products' must precede argument 'dependencies'
```

Adding an executable product to `Patches/mlx-swift/tests/Package.swift` placed `products:` after
`dependencies:`. SwiftPM enforces the argument order of the `Package` initializer.

### Why the local check passed

`swiftc -parse Package.swift` returned 0, because **the file is perfectly valid Swift either way**.
Argument order in that initializer is a *semantic* rule enforced by `PackageDescription`, and
`-parse` never gets that far. This is F18's shape exactly — a check that runs, passes, and tests
something other than what was assumed.

It also broke *two* jobs, not one. `mlx-lifecycle.yml` runs `swift test` and `mlx-ring.yml` runs
`swift build`, both in that directory, so a bad manifest takes out everything downstream of it.

### The check that does work, and it runs here

```bash
cd Patches/mlx-swift/tests && swift package dump-package
```

`dump-package` *evaluates* the manifest rather than parsing it — it compiles `Package.swift` against
`PackageDescription` and runs it — so it catches argument-order rules, bad target paths and
malformed products. It needs no dependency resolution and **no macOS**: it works on this Linux
container against a manifest whose platform is `.macOS(.v14)` and whose dependency is MLX.

**Verified with a negative control**, which is the only reason this is worth recording. Rewriting a
copy of the fixed manifest back into the broken order and running `dump-package` on Linux reproduces
the CI error verbatim, `argument 'products' must precede argument 'dependencies'`, at the same
diagnostic. So the check genuinely discriminates rather than passing on everything.

Added to the verification matrix. **Run it for any `Package.swift` edit** — the root package, this
one, or any future package. It is seconds, and it is the difference between finding this here and
finding it fifteen minutes into a macOS job.

### Not established

`dump-package` validates the manifest, not the code. It says nothing about whether the targets
compile, whether `import MLX` resolves, or whether the probe's API usage is correct — all of which
still need a real Apple toolchain. It would not have caught F18's three type errors, F20, or F22.
It closes exactly one gap: manifest semantics.

## F29 — macOS has no `timeout(1)`, and a harness that cannot say "inconclusive" will lie

Second CI cycle on the ring experiment, `e7a7efa`. `RingFormationProbe` **compiled and linked** —
`Build of product 'RingFormationProbe' complete! (97.91s)` — so the Swift API usage against MLX is
correct, which was the risk flagged as most likely. Then:

```text
ring-formation.sh: line 69: timeout: command not found
  rank 0: exit 127
  rank 1: exit 127
RESULT: no loopback ring. CLAUDE.md's claim stands as written
```

### Two separate defects, and the second is much worse

**1. `timeout(1)` is GNU coreutils and macOS does not ship it.** Available only as `gtimeout`, and
only if someone installed coreutils. Every other harness in this repository uses `timeout` and is
correct to — `Spikes/llamacpp-rpc/*` runs on Linux only. This was the first bounded harness written
for a *macOS* runner, and the assumption came along for the ride.

Replaced with a plain-bash watchdog: one background `sleep`, a marker file, `kill -9`, and
normalisation of the resulting 137 back to 124 so `HANG` reads the same as it would under
`timeout(1)`.

**2. The harness reported a confident false negative.** Exit 127 — *the command does not exist* —
was folded into "no loopback ring", and the script printed that CLAUDE.md's claim stands. **Nothing
had been tested.** The experiment could not run and the harness announced the status quo confirmed.

That is the worst variant of this project's recurring failure, because it is biased toward *not*
disturbing an existing belief. F18, F20, F22, F23 and F27 were all checks that passed while testing
the wrong thing; this one would have closed an open question in the direction of the assumption.

The fix is a **three-way** verdict, not two:

| outcome | meaning |
|---|---|
| every rank `0` | the ring formed |
| any rank `10`–`13`, or `124` | the ranks ran and the ring did not form — **a result** |
| any rank `14`, `127`, or anything else | **INCONCLUSIVE — draw no conclusion at all** |

`124` counts as a result deliberately: a rank that started, tried to reach its peer and blocked has
said something real, and it is the precise failure this project exists to kill. `134`/`139` stay
inconclusive, because a crash is more likely a defect in the probe than a statement about rings.

### The launcher is now testable without MLX

The root cause of shipping it broken was that it could only be exercised by a macOS CI run: the
script builds MLX before it does anything else. A `PROBE_BIN` override skips the build, so the
launcher's own logic runs against a stub anywhere.

Verified on Linux across four stubs — exit 0, exit 10, a missing command, and a process that blocks
forever — and all four verdicts are correct, including the watchdog firing and the 137 → 124
normalisation. That is a real negative control for the harness itself, which is what was missing.

### Not established

**The experiment still has no answer.** Two CI cycles have been spent on the harness — a manifest
argument order (F28) and this — and the ring has never been attempted. Whether two ranks form a
loopback ring remains exactly as open as before, and the probe compiling is the only thing gained.
