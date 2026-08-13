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

## Contract changes are a request, not an edit

A platform agent that needs a new `CapabilityProfile` field, a new `NodeState`, or a change to
`StagePlanner` **stops**. It appends the request to `findings.md` — what it needs, why, and what it
tried instead — and returns. The architect makes the change, updates the tests, and re-dispatches.

This is not bureaucracy. `ShareComputeCore` importing nothing is what contained both MLX spike
failures without touching the core, and it is what keeps the specification's later desktop adapters
reachable. Fifteen agents editing it concurrently would destroy that in an afternoon.

`senior-architect` is the one exception, and the rule inverts for it: it owns the contract, so it
makes the change rather than requesting it — and owes `Tests/ShareComputeCoreTests/**` moving in the
same commit, and `ShareComputeCore` still importing nothing.
