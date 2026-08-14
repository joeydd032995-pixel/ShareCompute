# `mlx-swift` patches

Patches against `N1k1tung/mlx-swift` — the fork the app actually builds against, not upstream
`ml-explore/mlx-swift`. See [`../README.md`](../README.md) for the repository chain and the order all
the patches must be applied in.

```bash
git clone https://github.com/N1k1tung/mlx-swift
cd mlx-swift
git checkout ios-distrib-0.3.0        # a TAG, not a branch -- see findings.md F13
git apply /path/to/Patches/mlx-swift/0001-free-group-and-expose-finalize.patch
```

---

## 0001 — Free the group, expose `finalize()`

**Requires `mlx/0002` and `mlx-c/0001`.** This patch calls two C symbols that do not exist at the
pinned revisions until those are applied.

**The bug.** `DistributedGroup.deinit` is empty, with its only real line commented out:

```swift
deinit { // this requires slight update for cmlx which I rather avoid, commented out for now
//        mlx_distributed_group_free(group)
}
```

The comment is accurate about the cause — `mlx_distributed_group_free` genuinely was not exported at
the pinned mlx-c revision — but the consequence is that **every `DistributedGroup` leaks its C
handle**, and with it a reference to the underlying group. `mlx-c/0001` exports the symbol, so the
line can now be restored.

**What the patch changes.**

| Change | Notes |
|---|---|
| `deinit` calls `mlx_distributed_group_free(group)` | The commented-out line, restored |
| New `static func finalize() -> Bool` | Wraps `mlx_distributed_finalize` |

**Why freeing in `deinit` is safe here.** Every `DistributedGroup` owns a *distinct* C handle, so
there is no aliasing and no double free. Checked against all three construction paths:

- `initialize(strict:)` → `mlx_distributed_init` → `mlx_distributed_group_new_(...)` → a fresh
  `new Group(...)`
- `split(color:key:)` → `mlx_distributed_group_split` → likewise a fresh allocation
- `init(group:)` is `internal` and is called only from those two, both within this file

On the failure paths both C functions return `{nullptr}`, and `mlx_distributed_group_free_` is
null-guarded, so freeing a failed group is a no-op. `group` is never reassigned after
initialisation.

**Why `finalize()` is `static`.** It releases the library's *internal* cache, which is process-wide
and not owned by any one instance. Making it an instance method would suggest it tears down *that*
group, which is precisely the misunderstanding that makes the call silently fail.

**Deliberately not `@discardableResult`.** A `false` means nothing was torn down, and ignoring it
reproduces the original defect silently — re-init hands back a stale group, nothing crashes, and the
ring is merely wrong. A caller who really means to ignore it has to write `_ =`.

**The ordering trap, and why the doc comment is long.** `finalize()` can only destroy the group once
*every* reference is gone, including Swift's. Setting a property to `nil` is not sufficient if
anything else still retains the instance — a group captured by a running generation task is the
realistic case. The doc comment says this explicitly, because the failure is silent at the call site
and the return value is the only signal.

### Verification

```bash
swiftc -parse Source/MLX/DistributedGroup.swift
```

Clean — and that is **syntax only**. `-parse` does not resolve `import Cmlx`, so it does *not* check
that `mlx_distributed_group_free` or `mlx_distributed_finalize` exist, that their signatures match,
or that C `bool` bridges to Swift `Bool` as expected. Those are type-checking and linking questions
that need macOS.

`git apply --check` against a pristine `ios-distrib-0.3.0` checkout passes.

### Restoring `deinit` cannot cause a use-after-free in this app

The leak has been the behaviour for the life of this fork, so anything relying on a C `Group`
outliving its Swift wrapper would now be reading freed memory. The app's `Ring/` code was audited
for this, and it is clean:

| Holder | What it holds |
|---|---|
| `MLXManager.group` | `DistributedGroup?` |
| `AllToShardedLinear.group`, `ShardedToAllLinear.group` (`AutoParallel.swift`) | `public let group: DistributedGroup` |
| sharded MoE layers (`TensorParallel.swift`) | `public let group: DistributedGroup` |

Every holder retains the **Swift wrapper class**, not the underlying `mlx_distributed_group`. No
call site extracts the C handle or stores one separately, so ARC guarantees the wrapper — and
therefore the C handle — outlives every user. Freeing in `deinit` is safe here.

### The same audit is a constraint on Stage 3

`loadModel` passes the group into `tensorAutoParallel` / `pipelineAutoParallel`, which store it on
the sharded layers. That produces this retain chain:

```text
ModelManager → ModelContext → model → sharded layers → DistributedGroup → C handle → shared_ptr<GroupImpl>
```

So `MLXManager.teardown()` **cannot** be `group = nil; DistributedGroup.finalize()`. While a sharded
model is loaded, the layers still hold the group, `finalize()` returns `false`, and — by the
check-before-clear design in `mlx/0002` — correctly does nothing. The loaded `ModelContext` has to be
released first.

This is the ordering trap the doc comment warns about, and it is not hypothetical: it is the default
state of the app whenever a model is loaded.

**Verified in CI:** type-checks, compiles and links, on `arm64-apple-macos` and iOS Simulator, with
`import Cmlx` genuinely resolved — which `swiftc -parse` never did. Both C symbols resolve from
Swift, but only once `0002` declares them in the vendored headers; `0001` alone does not build (F22).

**Not verified:** never *run*. Nothing has called `finalize()` or let ARC fire the patched `deinit`.
The audit above is a reading of the Swift sources, not a run.
