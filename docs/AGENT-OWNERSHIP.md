# File ownership — one writer per path

The contract every ShareCompute agent works under. It lives outside `.claude/skills/` deliberately:
a forked role needs the ownership table, not the router that decides *who* gets forked, and a skill
should not have to read a peer skill to find its own boundaries.

Routing, activation gates, isolation and escalation are in `.claude/skills/orchestration/SKILL.md`.
Project facts are in `CLAUDE.md`. Read that first.

## The table

This is the rule that makes parallel work safe. The shared contract is the collision point.

| Path | Primary owner | Everyone else |
|---|---|---|
| `Sources/ShareComputeCore/**` | `senior-architect` | read-only; file a change request |
| `Tests/ShareComputeCoreTests/**` | `tester`, `senior-architect` | may add cases, not rewrite existing |
| `Patches/mlx/**` | `mlx-cpp-specialist` | read-only |
| `Apps/InferRing/Ring/**` | `mac-backend` | read-only |
| `Apps/InferRing/InferRing/Services/**` | `mac-developer` | see the shared-tree rule below |
| `Apps/InferRing/InferRing/Screens/**` | `mac-designer` | see the shared-tree rule below |
| `Apps/InferRing/**/*.xcodeproj/**` | `senior-architect` | read-only |
| `native/split/**`, `scripts/build_split_runtime.py` | `linux-backend` | see the shared-native-tree rule below |
| `scripts/split_cluster.py`, `scripts/test_split_cluster.py`, `scripts/verify_split_runtime.py`, `scripts/download_split_model.py`, `scripts/verify_ios_simulator.py` | `linux-developer` | `linux-designer` and `windows-backend` when the primary is not running |
| `scripts/split_launcher.py`, `scripts/test_split_launcher.py`, `scripts/build_split_kit.py` | `windows-developer` | `windows-designer` (dashboard) and `linux-designer` (banner) when the primary is not running |
| `Apps/AndroidWorker/app/src/main/cpp/**`, `Apps/AndroidWorker/app/src/main/java/com/sharecompute/worker/NativeWorker.java`, `Apps/AndroidWorker/app/src/main/java/com/sharecompute/worker/NativeHost.java` | `android-backend` | read-only |
| `Apps/AndroidWorker/**`, except the `android-backend` paths above | `android-developer` | `android-designer` for screen and notification presentation when the primary is not running |
| `Apps/ComputeWorker/Sources/ComputeWorkerApp.swift`, `Apps/ComputeWorker/Sources/QRScanner.swift` | `ios-designer` | read-only |
| `Apps/ComputeWorker/Worker-Bridging-Header.h` | `ios-backend` | read-only |
| `Apps/ComputeWorker/**`, except the `ios-designer` and `ios-backend` paths above | `ios-developer` | `ios-backend` for native link keys in `project.yml` |
| `docs/evidence/**` | `tester` | **never edited** after commit, except to mask addresses and user names |
| `.github/workflows/**` | `senior-architect` | a platform role may edit its own job in `three-device-split.yml` when no one else is in that file |
| `CLAUDE.md`, `.claude/**`, `docs/AGENT-OWNERSHIP.md` | `senior-architect` | read-only |
| `task_plan.md`, `progress.md` | `junior-architect` | read-only |
| `findings.md` | anyone | **append only** — never rewrite another agent's finding |

**Never run two agents whose owned paths overlap at the same time.**

## The shared Apple tree

macOS and iOS are **one source tree** behind `#if os(...)`, so "the iOS files" is not a real
partition — a change to `RingHealthMonitor` or `RingManagementView` usually lands on both platforms.
Naming a `*-backend` and a `*-developer` owner per platform for the same directory would have been
fiction.

So the Apple app tree has a **single primary owner per directory**, listed above, and:

- `ios-developer`, `ios-designer` and `ios-backend` work in those same paths for iOS-specific
  changes, but **only when the primary is not running**, and they confine edits to platform
  conditionals wherever practical.
- A change that alters shared behaviour — not just the `#if os(iOS)` branch — goes through the
  primary owner.
- `ios-backend` owns iOS lifecycle *behaviour* (drain-on-background, lease clamping) wherever it
  lives, and will own its own directory once the adapter is factored out.

When this rule starts producing collisions, that is the signal to actually split the tree — raise it
with `senior-architect` rather than working around it.

## The shared native tree and the split protocol

`native/split/**` is **one source tree for four platforms**: desktop, Android (NDK), iOS and the
iOS simulator, all built through `build_split_runtime.py`. It works the same way as the Apple tree:

- `linux-backend` is the primary owner, because Linux is where this container can build and run it.
- `windows-backend`, `android-backend` and `ios-backend` edit only their platform's branch of it,
  **one at a time**, and only when the primary is not running.
- The pinned revision (`llama-revision.txt`), `llama-budget.patch` and `llama-cache.patch` change only through the
  primary. A change there means every platform rebuilds and the loopback proof runs again.

The laptop–phone protocol is implemented three times: `split_cluster.py` (`linux-developer`),
`WorkerSession.java` (`android-developer`) and `WorkerModel.swift` (`ios-developer`). A message
change touches all three. Sequence it, starting with the Python side and its tests, rather than
running the three roles in parallel.

## Contract changes are a request, not an edit

A platform agent that needs a new `CapabilityProfile` field, a new `NodeState`, or a change to
`StagePlanner` **stops**. It appends the request to `findings.md` — what it needs, why, and what it
tried instead — and returns. The architect makes the change, updates the tests, and re-dispatches.

This is not bureaucracy. `ShareComputeCore` importing nothing is what contained both MLX spike
failures without touching the core, and it is what keeps the specification's later desktop adapters
reachable. Twenty agents editing it concurrently would destroy that in an afternoon.

`senior-architect` is the one exception, and the rule inverts for it: it owns the contract, so it
makes the change rather than requesting it — and owes `Tests/ShareComputeCoreTests/**` moving in the
same commit, and `ShareComputeCore` still importing nothing.
