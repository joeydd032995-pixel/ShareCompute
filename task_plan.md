# Task Plan — ShareCompute Milestone 1: Elastic membership for infer-ring

**Goal:** a node can join or leave the ring — gracefully or by dying — and the ring detects it,
re-plans, and keeps working, with no corrupted output and no deadlock.

Full plan and rationale: `/root/.claude/plans/noble-gathering-parnas.md`.

---

## Phases

| # | Phase | Status |
|---|---|---|
| 0 | Planning files + SPM scaffold + vendor infer-ring | complete |
| 1 | Spikes A & B (MLX teardown; blocked collective) | **complete — BOTH FAILED**, see `findings.md` |
| 2 | Core value types: `CapabilityProfile`, `Lease`, `NodeState` | complete |
| 3 | `StagePlanner` — fixes the three F5 defects | complete |
| 4 | `MembershipService` — epochs, leases, failure detection | complete |
| 5 | Apple adapter wiring — **option 3 scope only** (detection, no re-formation) | code complete; compiles in macOS + iOS CI; **never run** |
| 6 | Chaos tests via the F6 hooks | not started — needs a Mac |
| 7 | README + docs | complete for what landed |

> **Phase 5 scope.** After the spikes failed, the direction chosen was option 3: detection without
> re-formation. Delivered: `RingWatchdog` in the core (tested), plus adapter wiring — iOS
> drain-on-background, a `/drain` endpoint restricted to self-announcements, heartbeats driving
> `MembershipService` through the previously-unused `/ping`, discovery no longer stopped at MLX
> init, generation abandonment on confirmed loss, fail-fast on new requests, and a UI banner.
>
> **`MLXManager.teardown()` was not written** — at the time it was an unavailable operation.
> Nothing in milestone 1 rebuilds the ring; it converts a hang into a reported failure.
> *(Superseded by Stage 2, which adds the `finalize()` this needed — see the milestone 2 section.)*
>
> *(Superseded: the adapter now type-checks and builds in macOS and iOS CI, against the patched MLX.
> It has still never been run — this container has no macOS, no Xcode and no MLX, and a build is not
> a behaviour.)*

> **Phase 1 outcome.** Both spikes were answerable by reading the pinned MLX sources, so neither
> needed hardware. Both failed. `MLXManager.teardown()` cannot be written: there is no free API
> and `distributed::init` caches its group in a process-lifetime static, so re-initialising
> returns the stale group. A survivor of a hard failure also cannot recover — the ring backend
> abandons unfulfilled promises on abort, so the collective waits forever with nothing thrown.
>
> The approved plan said "if Spike A fails, stop and re-plan". Phase 5 is therefore **not**
> started: writing a teardown path now known to be impossible would be waste. `ShareComputeCore`
> is unaffected because it never imported MLX. See "Re-plan" in `findings.md` for the three
> options.

---

## Key decisions

| Decision | Rationale |
|---|---|
| Membership changes are **epochs**, not mutations | F1/F2: MLX groups cannot be joined or left; per-token `allGather` makes partial membership impossible |
| iOS keeps `canHostRequiredStage = true` under a **foreground-scoped lease** | Spec §12.1 says otherwise, but combining iPhone RAM with a Mac's is infer-ring's entire value proposition (README headline benchmark). Honors the spec's intent, not its letter |
| `ShareComputeCore` has **zero external dependencies** | Refinement on the approved plan, which said Foundation + NIO. Transport is a protocol, so NIO is not needed; a dependency-free core builds and tests on Linux with nothing to resolve |
| Guaranteed graceful departure, **best-effort** hard failure | Detection ≠ recovery (F2). Proactive drain covers iOS backgrounding, the dominant real failure; hard-kill recovery is bounded by Spike B |
| Ring secret is in M1, not deferred | F7: the new endpoints would otherwise add a remote ring-teardown primitive that does not exist today |

---

## Errors Encountered

| Error | Attempt | Resolution |
|---|---|---|
| `swift: command not found` — no toolchain in container | 1 | Installed Swift 6.1.2 for Ubuntu 24.04 to `/opt/swift` |
| Spikes A & B require MLX on Apple Silicon; container is x86_64 Linux | 1 | Cannot resolve here. Wrote runnable harnesses under `Spikes/` for the user to run on a Mac; recorded as blocked in `findings.md` rather than faking results |
| Xcode project and all MLX/UIKit code uncompilable in this container | 1 | Scoped delivered-and-verified work to `ShareComputeCore`; adapter wiring left unstarted rather than shipped unverified |

---

---

## Milestone 2 — patch MLX so the ring survives node loss

| # | Stage | Status |
|---|---|---|
| 1 | `ring.cpp` fail-instead-of-hang | **complete** — `Patches/mlx/0001`, 16 harness checks pass |
| 2 | Make the group rebuildable (`distributed.cpp` `finalize()`, mlx-c free/finalize, mlx-swift) | **complete** — 4 patches, 31 harness checks pass, TSan-clean; C++ now compiles on Apple |
| 3 | Epoch re-formation in infer-ring (`MLXManager.teardown()`, non-terminal `RingWatchdog`) | **complete and building** — 67 core tests; compiles on macOS and iOS behind `MLX_HAS_FINALIZE`; **never run** |

**Stage 1 outcome.** The design changed materially during implementation. Two findings (F10, F11 in
`findings.md`) ruled out the obvious approach: `CommandEncoder::dispatch` has no `try`/`catch`, so
throwing from a collective would crash the process rather than fix the hang; and all ten future
sites use `wait()`, which discards an exception silently, so fixing the promises alone would have
produced data corruption. The failure is therefore *recorded* on the scheduler thread and rethrown
from `to_group()` on the caller's thread.

Stage 1 needs **no mlx-c or mlx-swift change** — the error rides the existing `mlx_error` /
`withError` path — so it is independently shippable and worth offering upstream to `ml-explore/mlx`.

**Stage 2 outcome.** Three coupled patches, one per repository, documented in `Patches/README.md`.
`distributed::init`'s function-local static moves behind an accessor and a new `finalize()` drops the
cached references, which runs the teardown that `~RingGroup()` and `~SocketThread()` always
implemented but never reached.

Two things changed during implementation, both worth carrying forward:

- **`finalize()` returns `bool`, and checks before it clears.** The first version cleared the cache
  and reported afterwards. That is accurate but leaves the worst state on failure — the old group
  gone from the cache yet still running its socket threads, with the next `init()` building a second
  live ring beside it. Checking every impl's use count first makes a failed teardown inert. The test
  caught this, not review.
- **F14 — the loaded model holds the ring.** Auditing whether restoring mlx-swift's `deinit` could
  cause a use-after-free (it cannot; everything retains the Swift wrapper) turned up the retain chain
  `ModelContext → model → sharded layers → DistributedGroup`. This is a hard constraint on Stage 3:
  `teardown()` must release the model *before* calling `finalize()`, or the call correctly does
  nothing.

## Next actions

> **Done since this list was written:** Stage 3 (item 1) landed in PR #9, gated behind
> `MLX_HAS_FINALIZE` because the patches had never reached the dependency (F20). Item 4 — the
> repoint — is done: `Patches/land-on-forks.sh` put the patches on
> `joeydd032995-pixel/{mlx, mlx-c, mlx-swift}`, and `project.pbxproj` points at the patched
> `mlx-swift` at `sharecompute/free-and-finalize`, with the flag on.
>
> **CI took two rounds and both are now settled.** Round one: SwiftPM *resolved* the patched fork
> (the `mlx-swift` identity conflict F16 recorded did not bite) and the patched MLX C++ compiled for
> arm64-apple, but both jobs failed on `cannot find 'mlx_distributed_group_free' /
> 'mlx_distributed_finalize' in scope` — F22, because mlx-swift vendors its own copies of the mlx-c
> headers and the module map exposes only those, so patching the submodule compiled the definitions
> without declaring them. `Patches/mlx-swift/0002` fixed it.
>
> Round two, against fork `6c15a2e`: **all five checks green.** The patched MLX builds end to end
> for `arm64-apple-macos` and iOS Simulator; both new C symbols link across all three repositories;
> and Stage 3's teardown compiled its `#if MLX_HAS_FINALIZE` branch. **Milestone 2 is code-complete
> and building.**
>
> Item 2's Swift lifecycle test is now possible. Item 3 still needs Apple hardware and two devices,
> and remains the whole of what is unverified — a build is not a behaviour.
>
> **Milestone 2 is closed.** PR #13 merged as `0b16529`, six checks green, with `mlx-swift/0003`
> verified against a controlled negative control (identical Xcode 26.6 / clang-2100.1.1.101 on both
> runs; only the fork commit changed).
>
> **The next action is no longer on this list.** F27 measured the portable path's transport cost for
> the first time and it reorders what matters: prompt processing **halves** across loopback RPC with
> no network involved, which is the opposite sign to the +11% the MLX path gains on Mac + iPhone.
> Combined with the warning in `Apps/InferRing/README.md` that Wi-Fi causes "sharp performance
> decline" — and the fact that an iPhone and a Windows PC have no USB path between them — the
> published benchmark describes a configuration this project's target cannot use.
>
> So **T2 is now a throughput measurement, not a connectivity check**, and it gates Phases 3–5
> rather than running beside them. If Wi-Fi PP collapses, the product re-scopes (wired Ethernet,
> smaller models, prefill-only offload, or plain remote inference) and that is much cheaper to learn
> now than after three phases are built on the assumption. Same reasoning F25 used to reorder 4.1
> and 4.2.
>
> **F26 also reshaped Phase 4.2 before any of it was written:** 10 of the 18 `GGML_ABORT` sites have
> no error channel, because their signatures come from ggml's shared backend vtable. It becomes a
> sticky-flag record-and-convert, with an open decision about `get_tensor` and silent corruption.

1. **Stage 3** — epoch re-formation in the app. Now unblocked: `finalize()` is the operation Spike A
   recorded as unavailable. `RingWatchdog`'s loss becomes non-terminal.

   `MLXManager.teardown()` has a required sequence, and every step is load-bearing:

   1. **Release every `DistributedGroup` handle** — the loaded `ModelContext` (its sharded layers
      each retain the group, F14) *and* `MLXManager.group`. Releasing only one leaves the other
      holding the ring.
   2. **Call `finalize()` and check the result.** It returns `false` if any handle survived.
   3. **Only on `true`, re-`init()`.** On `false` the teardown did not happen and a re-init returns
      the *same* group — treating that as success re-forms the ring with stale membership, silently.
      Surface it as an error and find the handle that outlived the teardown; retrying without
      releasing it fails identically.
2. **Write the Swift lifecycle regression test** — on macOS, where it can actually run. Nothing in
   this repository covers the Swift half; `group_finalize_test.cpp` mirrors the C++ cache only, and
   `swiftc -parse` does not even resolve `import Cmlx`. It must assert:

   - ARC actually calls `mlx_distributed_group_free` when the last `DistributedGroup` reference goes;
   - `finalize()` returns `false` while a handle is held, and `true` once every one is released.

   **Two items are struck from the original list, for different reasons — see F23.**

   *Symbol linkage* is already proven: both Apple jobs build and link the app against the patched
   MLX on every run. A test asserting it would restate a check that is strictly stronger.

   *"A re-`init()` returns a genuinely new group"* **cannot be asserted on a single machine, and
   writing it there would be worse than omitting it.** `ring::init` declines without `MLX_HOSTFILE`
   and `MLX_RANK`, so a bare runner gets an `EmptyGroup` — and the no-backend path caches it under
   the last-tried backend key, never under `"any"`. A second `init("any")` therefore always builds a
   fresh group, `finalize()` or not. The assertion would pass **vacuously**, producing a green check
   for the defect this whole milestone exists to fix while proving nothing about it. A one-rank ring
   is not a way out either: rank 0's peer is itself, and it connects before it listens, so it throws
   after ~5 s.

   A genuine single-host test of the memoisation needs **two processes** on `127.0.0.1` at different
   ports with `MLX_RANK` 0 and 1. Until that exists, the memoisation defeat stays on the hardware
   list rather than being dressed up as covered.

   This also needs a **new macOS CI job**: `ios.yml` and `objective-c-xcode.yml` both run
   `xcodebuild clean build [analyze]`, and neither runs tests.

   Still deliberately not committed as an unrunnable file — this container has no macOS or Xcode —
   but it is now runnable on a runner, which it was not before.

3. On a Mac: **run** the hardware tests. Building is done — CI compiles all five patches as part of
   the app on macOS and iOS. What remains is execution. For
   Stage 1, hard-kill a peer mid-generation and confirm a **thrown Swift error within about one
   token**, neither a hang nor a crash — test both `SIGKILL` and cable-pull, which take different
   `errno` paths. For Stage 2, confirm `~RingGroup()` and `~SocketThread()` complete promptly and
   without deadlock when a peer has *already* gone; the harness stub cannot show this.
4. Repoint `project.pbxproj` at the forks. **Keep the existing `kind = branch` form** — F16 showed
   SwiftPM resolves the branch-named-tag cleanly to `c53d302`, so F13's worry was unfounded and
   "correcting" it to `kind = revision` would be a change for its own sake. Watch instead for the
   conflicting `mlx-swift` package identity F16 records, which upstream says becomes a hard error in
   a future SwiftPM.
5. Add `ShareComputeCore` to the Xcode project (now wired in `project.pbxproj`) and type-check the
   adapter — actor isolation around `@MainActor RingHealthMonitor` is the likely breakage.
