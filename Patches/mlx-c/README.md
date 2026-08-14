# `mlx-c` patches

Patches against `ml-explore/mlx-c`. See [`../README.md`](../README.md) for the repository chain and
the order all the patches must be applied in.

```bash
git clone https://github.com/ml-explore/mlx-c
cd mlx-c
git checkout 0726ca922fc902c4c61ef9c27d94132be418e945   # the commit mlx-swift pins
git apply /path/to/Patches/mlx-c/0001-export-group-free-and-finalize.patch
```

The pin comes from the `Source/Cmlx/mlx-c` submodule of `N1k1tung/mlx-swift` at tag
`ios-distrib-0.3.0`.

---

## 0001 — Export `mlx_distributed_group_free` and `mlx_distributed_finalize`

**Requires `mlx/0002` to be applied first.** This patch calls
`mlx::core::distributed::finalize()`, which does not exist at the pinned MLX revision. Applying this
one alone gives `error: 'finalize' is not a member of 'mlx::core::distributed'`.

**Why this is needed at all.** Stage 2's teardown is only reachable from Swift if the C layer
exports it, and at this pin the C layer exports neither half:

| Symbol | At the pin | Needed for |
|---|---|---|
| `mlx_distributed_group_free_` (private, `inline`) | **already present** | — |
| `mlx_distributed_group_free` (public, `extern "C"`) | **missing** | mlx-swift's `deinit`, which is commented out because of exactly this |
| `mlx_distributed_finalize` | **missing** | releasing the cached group |

The private inline helper doing the actual `delete` has been there all along — only the public
export was absent. That is the whole reason mlx-swift's `deinit` carries the comment *"this requires
slight update for cmlx which I rather avoid, commented out for now"*.

**What the patch changes.** Three declarations and two definitions, no logic:

| Change | Notes |
|---|---|
| Declare + define `int mlx_distributed_group_free(mlx_distributed_group)` | Ported verbatim from mlx-c HEAD, which added exactly this function. Wraps the existing private `mlx_distributed_group_free_` |
| Declare + define `bool mlx_distributed_finalize(void)` | Forwards to `mlx::core::distributed::finalize()` |

**Why `bool` and not the file's usual `int` status.** Returning `int` would collide: the value that
matters is *did the teardown happen*, and an `int` here already means *did an error occur*. Rather
than add an out-parameter, this follows `mlx_distributed_is_available` — which is in this same file
at this same pin — and returns the answer directly, routing exceptions to `mlx_error` and returning
`false`. False-on-error is also the conservative answer: it says "assume nothing was torn down".

### Why not just bump the submodule to HEAD

Upstream HEAD (`fba4470`, MLX 0.31.2) does export `mlx_distributed_group_free`, so bumping looks
like it would save a patch. It would not: HEAD redesigned the whole distributed API from
return-value style to out-param plus `int` status.

```c
/* at the pin */   mlx_distributed_group mlx_distributed_init(bool strict, const char* bk);
/* at HEAD     */  int mlx_distributed_init(mlx_distributed_group* res, bool strict, const char* bk);
```

Every call site in mlx-swift's `DistributedGroup.swift` would have to change, against an MLX version
the app has never been built with. Even at HEAD, `mlx_distributed_finalize` still would not exist —
that half has no upstream equivalent to inherit. A ten-line patch at the known-good pin is the
smaller and more verifiable change.

### Verification

1. **Compilation.** `g++ -fsyntax-only -std=c++17` over the patched `distributed_group.cpp`, from a
   pristine `0726ca9` checkout, against a pristine `38ad2570` checkout with `mlx/0001` and
   `mlx/0002` applied. Clean.
2. **Coupling, as a negative control.** The same compile against the *unpatched* MLX header fails
   with `'finalize' is not a member of 'mlx::core::distributed'`. So the success above is real
   evidence that the mlx and mlx-c halves agree on the symbol, not two changes that merely look
   consistent.
3. **Application.** `git apply --check` against a pristine checkout of the pinned revision.

**Verified in CI:** builds as part of mlx-swift's `Cmlx` target and **links** — Swift calls both
`mlx_distributed_group_free` and `mlx_distributed_finalize` on macOS and iOS.

**The caution in the previous version of this paragraph was right, for a reason it did not guess.**
It worried the symbols might not be *visible* to Swift, reasoned there was "no known reason it would
not be", and concluded that "no known reason" is not a build. The build then failed exactly there —
but not because of export maps or visibility. mlx-swift vendors its own copies of these headers and
the module map exposes only those, so the definitions compiled while the declarations were missing
(F22). Worth keeping as a reminder that the instinct to withhold the claim was correct even though
the mechanism guessed was not.

**Not verified:** never *run*. Linking is not calling.
