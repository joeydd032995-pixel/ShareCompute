# Patches

Changes to MLX that infer-ring needs in order to survive a node leaving the ring.

The app builds against a chain of three repositories, and the fixes span all three:

| Repo | Pinned at | Patches |
|---|---|---|
| [`mlx`](mlx/) | `38ad257088fb2193ad47e527cf6534a689f30943` | `0001` fail instead of hang · `0002` `finalize()` |
| [`mlx-c`](mlx-c/) | `0726ca922fc902c4c61ef9c27d94132be418e945` | `0001` export `group_free` + `finalize` |
| [`mlx-swift`](mlx-swift/) | tag `ios-distrib-0.3.0` (`c53d302`) | `0001` free the group, expose `finalize()` |

The two submodule pins are read from the mlx-swift tree at that tag; the tag itself is what
`project.pbxproj` names. (It names it as a *branch*, which it is not — see `findings.md` F13. That
is Stage 3's problem, not the patches'.)

`Cmlx` is a **source** SwiftPM target, not a prebuilt binary, so a patched submodule is compiled
directly. There is no CMake step and no artifact to regenerate.

## Forks

`joeydd032995-pixel/mlx`, `joeydd032995-pixel/mlx-c` and `joeydd032995-pixel/mlx-swift` exist and
are where these patches are meant to land.

They are still kept here as patch files, which is deliberate: a patch that fails to apply tells you
upstream moved, whereas a fork silently diverges. The patch files stay the source of truth, and the
README in each directory says what the change is *for* — a fork branch alone would not.

## Applying

**Order matters.** `mlx` first: the mlx-c patch calls `mlx::core::distributed::finalize()`, which
does not exist until `mlx/0002` declares it. Applying mlx-c first gives
`error: 'finalize' is not a member of 'mlx::core::distributed'`.

```bash
git clone https://github.com/ml-explore/mlx && cd mlx
git checkout 38ad257088fb2193ad47e527cf6534a689f30943
git apply /path/to/Patches/mlx/0001-ring-fail-instead-of-hang.patch
git apply /path/to/Patches/mlx/0002-distributed-finalize.patch

git clone https://github.com/ml-explore/mlx-c && cd mlx-c
git checkout 0726ca922fc902c4c61ef9c27d94132be418e945
git apply /path/to/Patches/mlx-c/0001-export-group-free-and-finalize.patch

git clone https://github.com/N1k1tung/mlx-swift && cd mlx-swift
git checkout ios-distrib-0.3.0
git apply /path/to/Patches/mlx-swift/0001-free-group-and-expose-finalize.patch
```

Every patch above was checked with `git apply --check` against a pristine checkout of its pinned
revision, in this order.

## What the two stages do

**Stage 1 (`mlx/0001`) — a departing peer fails instead of hanging.** A rank leaving used to wedge
every survivor permanently, with no exception and no diagnostic. It now surfaces as a thrown Swift
error within roughly one token.

**Stage 2 (`mlx/0002` + `mlx-c/0001` + `mlx-swift/0001`) — the group can be rebuilt.** Stage 1 makes
loss *detectable*; it does not make it *survivable*, because `distributed::init` memoises the group
it creates and nothing can release it. Every re-init returns the stale group, so the ring re-forms
with the departed member still in it. Stage 2 adds the teardown that was missing.

Stage 2 does **not** by itself re-form the ring — that is Stage 3, in the app. It supplies the
operation Stage 3 needs and which `findings.md` Spike A recorded as unavailable.

## Verification

MLX is Apple-only and cannot be built on the Linux container this was written in, so verification is
in three layers:

```bash
# 1. every patched translation unit compiles
g++ -fsyntax-only -std=c++17 -I. \
    -I<mlx-swift>/Source/Cmlx/json/single_include/nlohmann \
    -I<mlx-swift>/Source/Cmlx/fmt/include -DMLX_VERSION='"0.24.2"' <file>

# 2. runtime semantics, mirrored against the real primitives
g++ -std=c++17 -O1 -pthread -o /tmp/stf mlx/tests/socket_thread_failure_test.cpp && /tmp/stf
g++ -std=c++17 -O1        -o /tmp/gft mlx/tests/group_finalize_test.cpp        && /tmp/gft

# 3. patches apply to pristine checkouts
git apply --check ...
```

43 checks across the two harnesses, all passing. Each harness **mirrors** the patched code rather
than including it, because MLX cannot be compiled here — so both must be updated in step if the
patched files change. Each ends with a case that reproduces the *unpatched* behaviour, so the defect
is pinned rather than merely described.

**Not verified anywhere:** none of this has been built as part of MLX, run on Apple hardware, or
exercised against a real multi-device ring. That needs macOS and at least two devices.
