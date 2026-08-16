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
- `Patches/mlx/tests/socket_thread_failure_test.cpp` — **16 checks, all passing**, against real
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

### Review round 1 (CodeRabbit, PR #3)

Ten actionable comments. One was a real defect in the patch; the rest were documentation.

**The cache was not thread-safe — valid, and worse than reported.** `finalize()`'s `clear()` racing
`init()`'s `find`/`insert` is undefined behaviour, but the sharper problem is that the
check-then-clear was not *atomic*: a concurrent `init()` could hand out a `Group` in the window
between the use-count check and the clear, so `finalize()` would destroy an impl the caller had just
taken a reference to — the exact case the check exists to prevent. Upstream's `find`/`insert` were
already unsynchronised; `finalize()` made it consequential.

Fixed with one mutex covering both functions, held across group construction in `init()` (so two
threads cannot each build a ring) and across the destructor joins in `finalize()`. The join-under-
lock is safe only because the ring's socket workers never re-enter `distributed.cpp`; that is now a
comment in the patch, because a future change could break it.

Verified rather than asserted: a new bounded concurrency case (4 × `init` against 2 × `finalize`,
200 ms, asserting at most one live group) is **TSan-clean**, and deleting the two `lock_guard` lines
makes the same case report **43 data races**. Harness now 31 checks.

The mutex protects the cache, not the caller's handles — a `Group` copied on another thread still
moves a use count without touching the cache. The header says so rather than implying the lock makes
`finalize()` fully thread-safe.

**Documentation fixes.** The `Patches/README.md` apply block was genuinely broken: successive
`cd`s nested, so mlx-c would have been cloned inside mlx. Rewritten with sibling clones in subshells
and *executed end to end* to confirm, along with the compile and negative-control commands, which
were placeholders (`<file>`, `...`) rather than runnable. mlx-swift now checks out the immutable
commit with the tag verified against it first, since a tag can move. Also documented `finalize()`'s
release-then-check ordering in `Patches/README.md` and `task_plan.md` (release *every* handle —
`ModelContext` and `MLXManager.group` — then check the `bool`); corrected a stale "15 checks" in the
session 3 entry to 16; removed a superseded "remains impossible" line in `task_plan.md`; labelled
three unlabelled fenced blocks.

**Not taken: a Swift lifecycle regression test.** Asked for an XCTest asserting ARC calls
`mlx_distributed_group_free`, that both C symbols link, and that release → `finalize()` → re-init
yields a new group. It is the right test and it cannot run here — no macOS, no Xcode, and the Swift
half has never been type-checked. Committing an unrunnable test file would assert coverage this
container cannot back. It is instead written up as a required hardware test in `task_plan.md`.

## Session 5 — Stage 3, and the patches reach a build

Milestone 2's last stage, plus the step that had been sitting in `task_plan.md`'s next-actions since
Stage 2: getting the patches somewhere the app can actually resolve them.

### Stage 3 — epoch re-formation

`RingWatchdog` no longer ends at `.lost`. `RingHealth` gained `.reforming(RingLossReason, since:)`,
`.lost` gained a `RingFailureCause?`, and the host reports outcomes back through
`reformationSucceeded/Failed/Unavailable(at:)`. Failure detection stays a pure function of injected
time — the watchdog still owns no timer and performs no I/O.

The adapter follows F14's required order: release `ModelContext`, release `MLXManager.group`,
`finalize()`, check the `Bool`, re-`init()` **only on `true`**. `ModelManager` gained
`releaseModelForReformation()` and `reloadAfterReformation()`, which re-plans through the existing
`assignShardMetadata` rather than constructing shard metadata itself.

Core tests 58 → 67, green on Linux.

### The gate — F20

Both Xcode jobs failed on `Manager.swift:58: type 'DistributedGroup' has no member 'finalize'`.
The patches had never been applied to anything the build resolves: `project.pbxproj` pointed at
unpatched `N1k1tung/mlx-swift`, and `Patches/README.md` said the forks were where the patches were
"meant to land". Stage 3 had been written against an API that existed only as a patch file, and
`swiftc -parse` could not have caught it — it resolves no imports.

Fixed by gating on `MLX_HAS_FINALIZE`, with `RingError.finalizeUnavailable` distinct from
`handlesOutlivedTeardown` (one means the operation does not exist in this build, the other means a
handle survived — different diagnoses) and a terminal `reformationUnavailable`, since retrying
cannot conjure a missing symbol. On unpatched MLX the app degrades to exactly Milestone 1, which is
all unpatched MLX can do.

A second CI round caught `RingManagementView.swift` — the `.lost` arity change. Services had been
audited, views had not.

### The forks

`Patches/land-on-forks.sh` applies all four patches in dependency order (mlx before mlx-c, because
the mlx-c patch calls `mlx::core::distributed::finalize()`), repoints mlx-swift's two submodules —
both the gitlink and `.gitmodules`, since the patched commits exist only in the forks — and refuses
to overwrite an existing remote branch. Run with `--push`:

| Fork | Branch | Tip |
|---|---|---|
| `mlx` | `sharecompute/stage1-2` | `18e53a6f9b88` |
| `mlx-c` | `sharecompute/export-finalize` | `55a61ffbe301` |
| `mlx-swift` | `sharecompute/free-and-finalize` | `01b72119cc77` |

`project.pbxproj` then repointed at the mlx-swift branch with `MLX_HAS_FINALIZE` defined at project
level for both Debug and Release. Setting it in CI alone would have left local Xcode builds
compiling the `#else` while CI compiled the `#if` — a divergence worse than the gate it replaces.
Three lines changed; brace and paren counts and `objectVersion` verified identical against a
pre-edit baseline.

### Verified this session

- `swift test` — 67 core tests green on Linux.
- The four patched translation units compile (`g++ -fsyntax-only -std=c++17`) **from the tree
  SwiftPM checks out**, not from a local patch application, with the negative control still firing.
- The fork chain resolves the way SwiftPM resolves it: full clone of the mlx-swift branch (422
  commits, `c53d302` as parent, `fsck` clean), then `git submodule update --init` checking out both
  fork tips. Full clone rather than shallow on purpose — `land-on-forks.sh` pushes a `--depth=1`
  fetch into a fork whose network is `ml-explore/mlx-swift`, so a truncated base history would pass
  a shallow fetch and fail only later inside SwiftPM. It did not.
- `DistributedGroup.finalize()` in the resolved tree is `public static func finalize() -> Bool`,
  matching the call at `Manager.swift:58`.

### Not verified

**The macOS jobs for the repoint had not finished when this was written.** Nothing in this
repository can establish either half of what they test: that SwiftPM resolves the patched fork past
the `mlx-swift` identity conflict F16 recorded, and that a patched MLX compiles under a real Apple
toolchain. `g++ -fsyntax-only` on Linux is not `clang` targeting arm64-apple.

Beyond that, unchanged and still the larger half: no ring has ever formed, no re-formation has been
observed, and none of the four patches has been *run*. Re-formation needs two Apple devices.

### Errors encountered (session 5)

| Error | Attempt | Resolution |
|---|---|---|
| Stage 3 written against `DistributedGroup.finalize()`, which no build had | 1 | Gated behind `MLX_HAS_FINALIZE`; recorded as F20. The root fix was landing the patches |
| `.lost` arity change missed in `RingManagementView.swift` | 2 | Audited services but not views; fixed and swept the whole app for both cases |
| `RingHealthMonitor` used `Ring` symbols without `import Ring` | 1 | Found by auditing imports against symbol usage — F18's exact failure mode |
| Fabricated a `ShardMetadata` initialiser that does not exist | 1 | Checked the real signature instead of trusting `swiftc -parse`; moved the reload into `ModelManager`, which owns the planning |
| `reformRing` marked `@MainActor` on a nonisolated class | 1 | Would force cross-domain access to a non-`Sendable` `ModelManager`; attribute removed |
| `git checkout -- .claude/skills/` destroyed uncommitted work | 1 | Harmless only because the files were untracked. Reapplied, and switched to per-file backups |
| Concluded from a `grep -A6` window that upstream had removed `ggml_abort`'s `abort()` | 1 | Read the whole function: the `abort()` sits past a callback block. F15 stands, and Phase 4.2 would have been deleted on the strength of a six-line window |
| Nearly agreed that `ios-distrib-0.3.0` does not exist, on `git ls-remote --heads` returning empty | 1 | It is a **tag**. Same trap as F13, twice in one session — `--heads` filters tags out |

## Session 6 — the patched MLX builds

Short session, one result: **all five patches compile as part of MLX under a real Apple toolchain,
and the new C symbols link.** That retires a caveat this project has carried since Stage 1.

### Two CI rounds

**Round one** (fork at `01b7211`) failed with two errors and no others:

```
DistributedGroup.swift:22:9: cannot find 'mlx_distributed_group_free' in scope
DistributedGroup.swift:69:9: cannot find 'mlx_distributed_finalize' in scope
```

It settled two open questions on the way down, both worth separating from the failure. SwiftPM
**resolved** the patched fork — the `mlx-swift` package-identity conflict F16 recorded did not bite,
so the recorded fallback of forking `mlx-swift-lm` too is unnecessary. And `Cmlx` builds before
`MLX`, so the fact that both jobs reached Swift `MLX` compilation means the patched C++ —
`ring.cpp`, `distributed.cpp`, `distributed_group.cpp` — had already compiled for arm64-apple.

The cause was F22, recorded in full there: mlx-swift vendors its own copies of the mlx-c headers and
the module map exposes only those, so patching the submodule compiled the definitions without
declaring them. `Patches/mlx-swift/0002` adds both declarations to both copies.

**Round two** (fork at `6c15a2e`), all five checks green:

| Check | Result |
|---|---|
| Build and analyse the Infer Ring scheme — macOS | ✅ `▸ Analyze Succeeded`, 15m47s |
| Build for iOS Simulator | ✅ 11m24s |
| swift test on Linux | ✅ 67 tests |
| MLX patch harnesses | ✅ 47 checks |
| Agent roster lint | ✅ |

Read from the log rather than the conclusion field, because this project has been caught by a false
green twice.

### Verified this session

- All five patches **compile as part of MLX** for `arm64-apple-macos` and arm64 iOS Simulator.
- `mlx_distributed_group_free` and `mlx_distributed_finalize` **link**, the latter reaching
  `mlx::core::distributed::finalize()` — the whole three-repository chain resolves.
- `DistributedGroup.swift` type-checks with `import Cmlx` genuinely resolved, which `swiftc -parse`
  never did.
- Stage 3's teardown compiled its `#if MLX_HAS_FINALIZE` branch. F20's `#else` is no longer what
  builds.
- Before re-triggering: the fork commit is a fast-forward on `01b7211`, both vendored headers carry
  both declarations, submodule pins unchanged at `18e53a6` / `55a61ff`.

**Milestone 2 is code-complete and building.**

### Not verified — and this is now the entire remaining gap

**Nothing has been run.** No ring has formed, `finalize()` has never been called, `~RingGroup()` and
`~SocketThread()` have never executed against a peer that already left, and the fail-instead-of-hang
path has never fired on a real socket. A build is not a behaviour.

The wording matters in both directions now. Under-claiming is as wrong as over-claiming was: the
compile-and-link claims are CI-backed on every run and should not be hedged. What stays hedged is
execution, and that needs two Apple devices.

### Errors encountered (session 6)

| Error | Attempt | Resolution |
|---|---|---|
| Read `swift test` as "0 tests passed" | 1 | `tail -4` had cut off the XCTest summary; the trailing line is the swift-testing runner, which finds none. 67 tests did run. Widened the filter instead of trusting the tail |
| `Patches/mlx-c/README.md` predicted this failure and reassured past it | — | It flagged that symbol visibility was unproven, guessed export maps, and said "no known reason it would not be". The mechanism was wrong; the instinct to withhold the claim was right. Kept in the file as a note rather than quietly rewritten |

## Session 7 — closing Milestone 2, and two corrections

Milestone 2 is **complete**. PR #11 merged the repoint and the gate; PR #12 merged the lifecycle
test, six-for-six green, with the macOS job showing `Build complete! (169.73s)` and then
`Executed 1 test, with 0 failures`. Patched MLX code has now *executed* — ARC's `deinit` reaches
`mlx_distributed_group_free` and the C++ use count observes it.

### Verified this session

- **`.define("FMT_CONSTEVAL", to: "")` emits exactly `-DFMT_CONSTEVAL=`**, and `MLX_VERSION` and
  `MLX_ENABLE_NAX` survive alongside it. Checked by building a throwaway SwiftPM package with all
  three defines and grepping `swift build -v` for the emitted flags. This closes the "not verified"
  F24 left open — the risk it named was that a `cxxSettings` define might clobber the existing two.
  It cannot: `cxxSettings: cxxSettings + [...]` is an array append, not a dictionary.
- `Patches/mlx-swift/0003` applies cleanly to the live fork tip `6c15a2e`, verified by the landing
  script cloning the actual remote rather than a local copy.

### Two corrections, both mine

**`isAvailable` does not mean what the lifecycle test's comment said.** The comment predicted it
would print `false` on a bare runner. It printed `true`. `ring::is_available()` is `return true;`
unconditionally — it reports that the ring backend was *compiled in*, not that a ring can be formed,
so on any Apple build it is a constant. F23's analysis is unaffected (an `EmptyGroup` was still what
got built), but the stated reasoning was wrong. Comment rewritten to say so, and to warn against
reaching for it as a readiness check. The app calls it nowhere, which is the correct number.

**infer-ring is not this project's app.** `CLAUDE.md` said "infer-ring is a shipping App Store app
(`id6757767558`)" without saying *whose*, and that phrasing was then read as though the App Store
presence belonged to this project — leading to a plan built on the premise that an Apple Developer
account and signing identity already existed. The project's own files say otherwise:

```text
PRODUCT_BUNDLE_IDENTIFIER = com.n1k1tung.InferRingApp   ·   com.n1k1tung.Ring
DEVELOPMENT_TEAM          = 669J55BPZE                  ·   TP4SYTX9NF
```

It is N1k1tung's app, vendored here; the claim traces to *their* README. Vendoring open-source code
conveys the code and nothing else — no account, no bundle IDs, no signing identity. `CLAUDE.md` now
says so explicitly, including the warning not to infer an account from the listing.

The consequence is real rather than cosmetic: **nothing can be put on an iPhone until this project
has its own Apple Developer account and bundle IDs**, and whether one exists is currently unknown.
That gates the whole iOS half of the next milestone.

### Errors encountered (session 7)

| Error | Attempt | Resolution |
|---|---|---|
| New macOS job failed on fmt/consteval | 1 | F17 had already diagnosed it; the job simply carried no `-DFMT_CONSTEVAL=`. Added as `-Xcxx`, which is SwiftPM's spelling — the setting does not carry across build systems by name. Recorded as F24 |
| Asserted the operator ships infer-ring on the App Store | 1 | Inferred from `CLAUDE.md`'s unattributed phrasing; the bundle IDs and team IDs are N1k1tung's. Caught by the operator, not by me. `CLAUDE.md` corrected at the source so the next reader cannot repeat it |
| Test comment predicted `isAvailable == false`; it printed `true` | 1 | Read `ring::is_available()` rather than re-guessing: it returns `true` unconditionally. Comment rewritten around what the value actually reports |

---

## Session 8 — Milestone 2 merged, and the first throughput number

### PR #13 merged as `0b16529`, six checks green

`mlx-swift/0003` landed on the fork at `c0b3b026` and the `-DFMT_CONSTEVAL=` flag came out of all
three CI jobs. **The verification is a controlled comparison, not just a green tick:**

| | Aug 14, `52f48af` | Aug 16, `dac8f23` |
|---|---|---|
| Swift / clang | 6.3.3, `clang-2100.1.1.101` | 6.3.3, `clang-2100.1.1.101` |
| Xcode | 26.6 (`17F113`) | 26.6 (`17F113`) |
| Command | `swift test -Xcxx -DFMT_CONSTEVAL=` | `swift test` |
| Fork resolved | `6c15a2e` (pre-`0003`) | `c0b3b02` (with `0003`) |

Identical toolchain; one variable changed. The negative control is real — two minutes before the
Aug-14 pass, the *same image* without the flag produced the five `consteval` errors in `format.cc`.
Read from the job logs rather than the conclusion fields, per the standing rule.

The alternative explanation — "the runner image rolled and fmt compiles now regardless" — was the
one thing that could have made this a false green, and checking the Versions step of both runs is
what killed it.

### F26 — Phase 4.2 is not the shape the plan said

Reading `ggml-backend-impl.h` against `ggml-rpc.cpp` before writing any code: **10 of the 18
`GGML_ABORT` sites have no error channel at all.** Seven are declared `void` by ggml's shared backend
vtable (`set_tensor`, `get_tensor`, `free_buffer`, `memset_tensor`, `clear`, `get_memory`), six more
return `size_t` or a pointer with no failure value. Changing those signatures means changing CUDA,
Metal, Vulkan and every other backend — not an RPC-local patch.

And **the site T1 measured 3/3, `ggml-rpc.cpp:509`, is one of the `void` ones**, so the planned fix
would have missed the failure that actually fires.

The workable design is this project's own precedent (load-bearing fact #3): a sticky per-connection
flag, recorded by the `void` sites, converted at `graph_compute` — which returns `enum ggml_status`
and runs every token. One open risk, deliberately unresolved: `get_tensor` failing leaves the
caller's buffer unwritten, and if no `graph_compute` follows the flag never converts, giving **silent
corruption in place of a visible abort** — precisely fact #4's shape. Decide before writing.

### F27 — the transport halves prompt processing before any network exists

New harness `Spikes/llamacpp-rpc/throughput.sh`. Qwen2.5-0.5B Q4_K_M, 128 tokens, 3 repeats, median,
4-core Xeon, no GPU:

| configuration | PP t/s | TG t/s | wall ms |
|---|---|---|---|
| local, no RPC | **202.6** | 24.6 | 6353 |
| 1 peer, loopback | 109.9 | 27.5 | 8266 |
| 2 peers, loopback | 111.4 | 27.3 | 8268 |

Per-run bands do not overlap, so both deltas are real. PP **halves with no network at all** — and PP
is the metric where `Apps/InferRing/README.md` measures the MLX path *gaining* 11% on Mac + iPhone.
Opposite sign. A second peer is free: the cost is the first hop, not the number of hops.

The TG column is *not* a transport win. Both processes share one 4-core box, so with `-ngl 99` the
server's threadpool does the matmuls while the client blocks on the socket. Per-token transport cost
is below this host's measurement floor, not negative.

**Why this was measured at all:** the encouraging −12%/+11% figures come with a warning two lines
above them — *"Wi-Fi and pre-TB5 over RDMA connections will result in sharp performance decline"* —
and a USB3.2 recommendation. An iPhone and a Windows PC have no such cable. The published numbers
come from the one configuration this project's target cannot use.

### Errors encountered (session 8)

| Error | Attempt | Resolution |
|---|---|---|
| First `throughput.sh` omitted `-v`, so its logs could not prove the RPC devices were used at all | 1 | Caught by grepping the logs for `assigned to device` and finding nothing. A silent fallback to local compute would have produced a perfectly believable table — the same false-green shape as F18/F20/F22/F23. Harness now carries `-v`, counts offloaded tensors, prints the count per row, and fails loudly on an `--rpc` row that offloaded zero. Re-run reproduces the table |
| Nearly reported "RPC makes generation faster" | 1 | TG rising when a network hop is *added* is implausible; checked the per-run spread and the thread allocation instead of publishing the median. Bands genuinely do not overlap, but the cause is same-box scheduling, so the claim is "below the measurement floor", not "negative" |
| Both loopback servers logged to the same file | 1 | Tag collision in `serve` — `srv-a.log` overwritten, losing the evidence the second peer served. Distinct tags |
