# Progress log

## Session 1 — 2026-08-07

### Environment
- Container is **x86_64 Linux (Ubuntu 24.04)**. No macOS, no Xcode, no Apple Silicon.
- No Swift toolchain present at start → installed **Swift 6.1.2** to `/opt/swift`.
- Consequence: `ShareComputeCore` is fully buildable and testable here; the Xcode project and
  anything importing MLX or UIKit is not.

### Done
1. Vendored `infer-ring-main` → `Apps/InferRing/` (unmodified).
2. SPM workspace with a dependency-free `ShareComputeCore` target.
3. Planning files per `planning-with-files`.
4. `CapabilityProfile`, `Lease`, `Epoch`, `NodeState` + `NodeStateMachine`, `RingClock`.
5. `StagePlanner` replacing `ModelManager.assignShardMetadata`.
6. `MembershipService` — epochs, leases, heartbeat hysteresis, anti-flap dwell.
7. **Spikes A and B — both answered from source, both failed.** See `findings.md`.

### Test results

`/opt/swift/usr/bin/swift test` — **46 tests, 0 failures.**

| Suite | Tests |
|---|---|
| `NodeStateMachineTests` | 14 |
| `StagePlannerTests` | 17 |
| `MembershipServiceTests` | 15 |

Includes two 500-iteration property tests over apportionment (exact coverage; no node exceeding
the ceiling of its proportional entitlement).

### Notable events

**A test caught a design flaw in my own algorithm.** The first `apportion` implementation granted
every node one layer up front and divided the remainder by weight. On a 30 GB / 6 GB ring over 36
layers that yielded 29/7 instead of 30/6 — systematically pushing layers *toward* the
memory-constrained device, a milder form of the very defect being fixed. Corrected to apportion
the full total by weight first, applying the minimum-one-layer rule afterwards by taking from the
largest holder.

**Both spikes failed, and the plan's gate fired.** Rather than run them on hardware I do not have,
I read the pinned sources: the `N1k1tung/mlx-swift` fork at `ios-distrib-0.3.0` and upstream
`ml-explore/mlx` at the pinned submodule commit `38ad257`. The answers were unambiguous at source
level — no group-free API and a process-lifetime static cache in `distributed::init` (Spike A);
abandoned promises with no exception on the ring backend's abort path (Spike B).

Phase 5 (adapter wiring) was **not** started as a result. The approved plan gated on Spike A and
said to stop and re-plan if it failed.

### Errors encountered

| Error | Attempt | Resolution |
|---|---|---|
| `swift: command not found` | 1 | Installed Swift 6.1.2 for Ubuntu 24.04 |
| SPM: "target has overlapping sources" | 1 | Test target directory did not exist yet; created `Tests/ShareComputeCoreTests/` |
| `XCTAssertEqual(_:_:accuracy:)` rejects optionals | 1 | Made the tests `throws` and used `try XCTUnwrap` |
| Apportionment biased toward the smallest node | 1 | Reordered: apportion by weight first, apply the min-one-layer floor afterwards |
| `git sparse-checkout init -q` — unknown switch | 1 | Dropped `-q`; the subcommand does not accept it |
| MLX not in `Package.resolved` | 1 | Xcode stores branch-pinned remote packages in `project.pbxproj`; found the forks there |

---

## Session 2 — option 3: detection without re-formation

Direction chosen after the spikes failed: report the failure, do not attempt to repair the ring.

### Done
1. `main` created as an orphan baseline holding the vendored infer-ring app, so the PR diff shows
   only new work. Feature branch rebuilt on top of it, tree verified byte-identical before pushing.
   PR #1 opened.
2. `RingWatchdog` in the core — **12 tests**. Total now **58, 0 failures**.
3. Adapter wiring: `RingHealthMonitor`, `/drain` endpoint and client call, heartbeats via the
   previously-unused `/ping`, discovery kept alive past MLX init, generation abandonment, fail-fast
   on new requests, `RingLostBanner`.

### Test results

`/opt/swift/usr/bin/swift test` — **58 tests, 0 failures.**

| Suite | Tests |
|---|---|
| `NodeStateMachineTests` | 14 |
| `StagePlannerTests` | 17 |
| `MembershipServiceTests` | 15 |
| `RingWatchdogTests` | 12 |

Adapter files pass `swiftc -parse` (syntax only). **Not type-checked, not built** — no macOS, no
Xcode, no MLX here.

### Notable events

**A test caught a second design flaw of mine.** `RingWatchdog.isTerminal` only flipped inside
`evaluate(at:)`, so `beginGeneration` could admit a request into an already-dead ring — precisely
the hang the type exists to prevent. Fixed by having `beginGeneration` confirm any overdue loss
first.

**Chose not to add a shared ring secret.** The plan called for one to protect membership-mutating
endpoints. The only such endpoint in option 3 is `/drain`, and a secret would need a key-exchange
UX that is a product decision, not mine. Restricting `/drain` to self-announcements — verified
against the connection's remote address — closes the specific hole being opened, with no new
dependency and no new UX. Broader control-plane auth stays open.

**Did not edit `project.pbxproj`.** Adding the local package by hand-editing a generated project
file that cannot be opened or built here risks leaving the app unbuildable. Documented as a manual
Xcode step instead.

### Errors encountered (session 2)

| Error | Attempt | Resolution |
|---|---|---|
| Edit to `RingCoordinator` failed — string not found | 1 | Assumed 20-space indentation; actual was 16. Re-read via grep with context and matched exactly |
| `grep: No such file or directory` on a path that existed | 1 | Bash cwd had reset between calls; used an absolute path |
| `RingWatchdog.isTerminal` stale unless `evaluate` was called | 1 | `beginGeneration` now confirms overdue loss before answering |

### Next

See "Next actions" in `task_plan.md`.

---

## Session 3 — milestone 2, stage 1

### Done
1. `main` made the default branch (by the user), which restored CodeRabbit auto-review.
2. `project.pbxproj` wired to `ShareComputeCore` as a local package — the manual step deferred in
   session 2, done once the file contents were available.
3. **Stage 1 MLX patch**: `Patches/mlx/0001-ring-fail-instead-of-hang.patch` plus a standalone
   verification harness.

### Verification

- `g++ -fsyntax-only -std=c++17` over the patched `ring.cpp` and `ops.cpp` — clean. (MLX's own
  headers compile on Linux with the `json`/`fmt` headers vendored in mlx-swift, which was an
  unexpected and very useful discovery: it means the patch can be compile-checked here.)
- `Patches/mlx/tests/socket_thread_failure_test.cpp` — **15 checks, all passing**, against real
  `socketpair` sockets, including closing the peer end to drive the `r == 0` path directly. Every
  wait is bounded so a regression is reported rather than reproduced as a hang.
- `ShareComputeCore` — still 58 tests, 0 failures.

### Notable events

**The planned approach was wrong and the research caught it.** The plan said "make `ring.cpp`'s
abort path throw". Reading `mlx/backend/cpu/encoder.h` showed `CommandEncoder::dispatch` has no
`try`/`catch` and hands tasks to `scheduler::enqueue` — so that change would have converted a hang
into `std::terminate()` on a shipping App Store app. Separately, all ten future sites use `wait()`,
which discards exceptions, so fixing the promises alone would have produced silent corruption.
Both were found before writing code, not after.

**The harness pins the bug, not just the fix.** Its final case mirrors the *unpatched* worker and
asserts that it hangs. Without that, the suite would prove the new code works but not that the old
code was broken.

**Did not fork the upstream repos.** Creating repositories under the user's GitHub account is their
decision, so Stage 1 is held as an appliable patch file with instructions.

### Errors encountered (session 3)

| Error | Attempt | Resolution |
|---|---|---|
| `Edit` refused: "File has not been read yet" on the mlx sources | 1 | Read the target ranges first; the tool requires it even for files outside the project |
| `git sparse-checkout set mlx/scheduler.h` — "not a directory" | 1 | sparse-checkout takes directories; widened to the parent instead |

---

## Session 4 — Milestone 2 Stage 2: the group becomes rebuildable

### What was built

Three coupled patches, one per repository in the build chain:

| Patch | Change |
|---|---|
| `Patches/mlx/0002-distributed-finalize.patch` | `init`'s function-local static moves behind a `backends()` accessor; new `bool finalize()` |
| `Patches/mlx-c/0001-export-group-free-and-finalize.patch` | Public `mlx_distributed_group_free` (private inline helper already existed) + `mlx_distributed_finalize` |
| `Patches/mlx-swift/0001-free-group-and-expose-finalize.patch` | `deinit` restored; `static func finalize() -> Bool` |

Plus `Patches/README.md` as the entry point for the whole chain, per-repo READMEs, and
`Patches/mlx/tests/group_finalize_test.cpp`.

### Test results

| Check | Result |
|---|---|
| `ShareComputeCore` | 58 tests, 0 failures |
| `tests/socket_thread_failure_test.cpp` (Stage 1) | 16 checks, all pass |
| `tests/group_finalize_test.cpp` (Stage 2) | 27 checks, all pass |
| `g++ -fsyntax-only` — `distributed.cpp`, `ring.cpp`, `ops.cpp`, `distributed_group.cpp` | all clean, from pristine checkouts with patches applied in order |
| `swiftc -parse` — `DistributedGroup.swift` | clean (**syntax only** — does not resolve `import Cmlx`) |
| `git apply --check` — all four patches against pristine pinned checkouts | all apply |

### Notable events

**The test corrected the design, not review.** The first `finalize()` cleared the cache and then
reported via `weak_ptr::expired()`. Writing the case for "what happens on a failed teardown" showed
the result was worse than the bug being fixed: the old group leaves the cache but keeps running its
socket threads, so the next `init()` builds a *second* live ring in the same process. Rewritten to
check every impl's use count **before** clearing, so a failed `finalize()` is inert. The doc comments
had already been written asserting the wrong consequence.

**A negative control, not just a green compile.** The mlx-c half was compiled against the *unpatched*
MLX header as well as the patched one. It fails with `'finalize' is not a member of
'mlx::core::distributed'`, which is what makes the passing compile evidence that the two halves agree
on the symbol rather than two changes that merely look consistent.

**Bumping the mlx-c submodule was considered and rejected.** Upstream HEAD does export
`mlx_distributed_group_free`, but it redesigned the whole distributed API to out-param plus `int`
status, so every mlx-swift call site would change against an MLX version the app has never built
against — and `mlx_distributed_finalize` still would not exist. A ten-line patch at the known-good
pin is smaller and more verifiable.

**Two findings recorded.** F13: `ios-distrib-0.3.0` is a **tag**, and `project.pbxproj` asks for it
as a branch — which matters when Stage 3 repoints at the forks. The submodule pins Stage 1 and 2 were
written against are confirmed correct. F14: the loaded model retains the ring through the sharded
layers, so Stage 3's `teardown()` must release `ModelContext` before calling `finalize()`.

### What is still unverified

Unchanged from session 3, and it is the larger half. None of the four patches has been built as part
of MLX, linked, run on Apple hardware, or exercised against a real multi-device ring. Specifically
for Stage 2: nothing here shows that `~RingGroup()` and `~SocketThread()` complete promptly and
without deadlock against real sockets whose peer has already left — the harness stub has a trivial
destructor. That is the first thing to check on hardware.

### Errors encountered (session 4)

| Error | Attempt | Resolution |
|---|---|---|
| `Edit` refused: "File has not been read yet" on `distributed.cpp` | 1 | Read the file first — same lesson as session 3, on a file re-opened after a context break |
| Assumed the pinned mlx-c already exported `mlx_distributed_group_free` | 1 | The attached clone was at HEAD, not the pin. Fetched `0726ca9` and diffed: only the *private* helper existed there |
| Documented `finalize()`'s failure mode wrongly in three places | 1 | The test disproved it before commit; implementation changed and comments rewritten |
