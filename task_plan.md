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
| 5 | Apple adapter wiring — **option 3 scope only** (detection, no re-formation) | code complete; **compiles unverified** |
| 6 | Chaos tests via the F6 hooks | not started — needs a Mac |
| 7 | README + docs | complete for what landed |

> **Phase 5 scope.** After the spikes failed, the direction chosen was option 3: detection without
> re-formation. Delivered: `RingWatchdog` in the core (tested), plus adapter wiring — iOS
> drain-on-background, a `/drain` endpoint restricted to self-announcements, heartbeats driving
> `MembershipService` through the previously-unused `/ping`, discovery no longer stopped at MLX
> init, generation abandonment on confirmed loss, fail-fast on new requests, and a UI banner.
>
> **`MLXManager.teardown()` was not written** — it remains impossible. Nothing here rebuilds the
> ring; it converts a hang into a reported failure.
>
> The adapter code passes `swiftc -parse` but has **never been type-checked or built**: this
> container has no macOS, no Xcode and no MLX. Treat it as unverified until it builds on a Mac.

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
| 1 | `ring.cpp` fail-instead-of-hang | **complete** — `Patches/mlx/`, 15 harness checks pass |
| 2 | Make the group rebuildable (`distributed.cpp` reset, mlx-c free/finalize, mlx-swift) | not started |
| 3 | Epoch re-formation in infer-ring (`MLXManager.teardown()`, non-terminal `RingWatchdog`) | not started |

**Stage 1 outcome.** The design changed materially during implementation. Two findings (F10, F11 in
`findings.md`) ruled out the obvious approach: `CommandEncoder::dispatch` has no `try`/`catch`, so
throwing from a collective would crash the process rather than fix the hang; and all ten future
sites use `wait()`, which discards an exception silently, so fixing the promises alone would have
produced data corruption. The failure is therefore *recorded* on the scheduler thread and rethrown
from `to_group()` on the caller's thread.

Stage 1 needs **no mlx-c or mlx-swift change** — the error rides the existing `mlx_error` /
`withError` path — so it is independently shippable and worth offering upstream to `ml-explore/mlx`.

## Next actions

1. Decide whether to fork `ml-explore/mlx`, `ml-explore/mlx-c` and `N1k1tung/mlx-swift`. Stage 1 is
   held as a patch file rather than a fork because creating repositories under an account is the
   owner's call.
2. On a Mac: apply the patch, build MLX as part of the app, and run the Stage 1 hardware test —
   hard-kill a peer mid-generation and confirm a **thrown Swift error within about one token**,
   neither a hang nor a crash. Test both `SIGKILL` and cable-pull, which take different `errno` paths.
3. Add `ShareComputeCore` to the Xcode project (now wired in `project.pbxproj`) and type-check the
   adapter — actor isolation around `@MainActor RingHealthMonitor` is the likely breakage.
4. Then Stage 2, then Stage 3.
