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
are where these patches are meant to land. **They have not landed yet**, which is why
`DistributedGroup.finalize()` exists in no build and Stage 3's teardown compiles only its `#else`
branch — see `findings.md` F20.

[`land-on-forks.sh`](land-on-forks.sh) does it:

```bash
bash Patches/land-on-forks.sh          # apply everything, push nothing
bash Patches/land-on-forks.sh --push   # and push the three branches
```

Everything up to the push has been run in the Linux container; only the push needs credentials.
Two things it handles that are easy to get wrong by hand:

- **mlx before mlx-c**, because the mlx-c patch calls `mlx::core::distributed::finalize()`, which
  `mlx/0002` declares.
- **mlx-swift's submodules need both halves repointed** — the gitlink for *which commit*, and
  `.gitmodules` for *which repository*. The patched commits exist only in the forks, so leaving the
  `ml-explore` URLs in place would leave the pins unresolvable.

It also fetches mlx-swift from **`N1k1tung/mlx-swift`**, not from the fork: that fork was taken from
`ml-explore` and contains neither `c53d302` nor the `ios-distrib-0.3.0` tag. Pushing that history to
a branch in the fork is fine — git does not require a branch to share ancestry with `main`.

They are still kept here as patch files, which is deliberate: a patch that fails to apply tells you
upstream moved, whereas a fork silently diverges. The patch files stay the source of truth, and the
README in each directory says what the change is *for* — a fork branch alone would not.

## Applying

**Order matters.** `mlx` first: the mlx-c patch calls `mlx::core::distributed::finalize()`, which
does not exist until `mlx/0002` declares it. Applying mlx-c first gives
`error: 'finalize' is not a member of 'mlx::core::distributed'`.

Run this from an empty directory, with `$PATCHES` pointing at this directory. Each repository is
cloned as a *sibling* and patched in a subshell, so the `cd`s do not nest.

```bash
PATCHES=/path/to/ShareCompute/Patches

git clone https://github.com/ml-explore/mlx mlx
( cd mlx \
  && git checkout 38ad257088fb2193ad47e527cf6534a689f30943 \
  && git apply "$PATCHES/mlx/0001-ring-fail-instead-of-hang.patch" \
  && git apply "$PATCHES/mlx/0002-distributed-finalize.patch" )

git clone https://github.com/ml-explore/mlx-c mlx-c
( cd mlx-c \
  && git checkout 0726ca922fc902c4c61ef9c27d94132be418e945 \
  && git apply "$PATCHES/mlx-c/0001-export-group-free-and-finalize.patch" )

git clone https://github.com/N1k1tung/mlx-swift mlx-swift
( cd mlx-swift \
  && test "$(git rev-parse ios-distrib-0.3.0^{commit})" = "$(git rev-parse c53d302^{commit})" \
  && git checkout --detach c53d302 \
  && git apply "$PATCHES/mlx-swift/0001-free-group-and-expose-finalize.patch" )
```

The mlx-swift checkout is by **commit**, not by the `ios-distrib-0.3.0` tag, with the tag checked
against it first. A tag can be moved; the commit is what the submodule pins above were actually read
from, and what the patches were written against. If that `test` fails, the tag has moved and the
pins in the table need re-deriving before anything else here is trustworthy.

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

**`finalize()` has a precondition, and it is the easy thing to get wrong.** It returns `false` and
changes nothing while *any* group handle is still held. Callers must release every one first — in
infer-ring that means the loaded `ModelContext` (whose sharded layers each retain the group, see
`findings.md` F14) **and** `MLXManager.group`, not just one of them. Then check the result: a `false`
means the teardown did not happen and a subsequent `init()` returns the same group, so proceeding as
though the ring were rebuilt would re-form it with stale membership. The order is: release every
handle → `finalize()` → only on `true`, re-`init()`.

## Verification

MLX is Apple-only and cannot be built on the Linux container this was written in, so verification is
in four layers. Everything below was run against a fresh checkout produced by the block above, from
the directory holding the three sibling clones.

**1. Every patched translation unit compiles.**

```bash
J=$PWD/mlx-swift/Source/Cmlx/json/single_include/nlohmann
F=$PWD/mlx-swift/Source/Cmlx/fmt/include
MLXV=-DMLX_VERSION='"0.24.2"'

( cd mlx && for f in mlx/distributed/distributed.cpp \
                     mlx/distributed/ring/ring.cpp \
                     mlx/distributed/ops.cpp; do
    g++ -fsyntax-only -std=c++17 -I. -I"$J" -I"$F" $MLXV "$f" || echo "FAILED: $f"
  done )

( cd mlx-c && g++ -fsyntax-only -std=c++17 -I. -I../mlx -I"$J" -I"$F" $MLXV \
    mlx/c/distributed_group.cpp )
```

**2. The mlx and mlx-c halves are genuinely coupled** — a negative control, not just a green
compile. Compiling mlx-c against the *unpatched* MLX header must fail:

```bash
mkdir -p nc/mlx/distributed
( cd mlx && git show HEAD:mlx/distributed/distributed.h ) > nc/mlx/distributed/distributed.h
( cd mlx-c && g++ -fsyntax-only -std=c++17 -I. -I../nc -I../mlx -I"$J" -I"$F" $MLXV \
    mlx/c/distributed_group.cpp )
# expected: error: 'finalize' is not a member of 'mlx::core::distributed'
```

Without this, a passing compile in step 1 would be equally consistent with two changes that merely
*look* consistent.

**3. Runtime semantics**, mirrored against the real primitives (run from this `Patches/` directory):

```bash
g++ -std=c++17 -O1 -pthread -o /tmp/stf mlx/tests/socket_thread_failure_test.cpp && /tmp/stf
g++ -std=c++17 -O1 -pthread -o /tmp/gft mlx/tests/group_finalize_test.cpp        && /tmp/gft

# the finalize harness's concurrency case, under ThreadSanitizer
g++ -std=c++17 -O1 -g -fsanitize=thread -pthread \
    -o /tmp/gft_tsan mlx/tests/group_finalize_test.cpp && /tmp/gft_tsan
```

47 checks across the two harnesses — 16 for Stage 1, 31 for Stage 2 — all passing, and TSan clean.
Deleting the two `lock_guard` lines from the Stage 2 harness and re-running reports 43 data races,
so its concurrency case genuinely exercises what the mutex prevents.

Each harness **mirrors** the patched code rather than including it, because MLX cannot be compiled
here — so both must be updated in step if the patched files change. Each ends with a case that
reproduces the *unpatched* behaviour, so the defect is pinned rather than merely described.

**4. Patches apply to pristine checkouts.** `git apply --check` for each of the four patches against
its pinned revision — this is what the block under *Applying* above does, with `git apply` in place
of `--check`.

Separately, `swift test` at the repository root stays green (66 tests) — these patches touch no
Swift in `ShareComputeCore`, so any change there would mean something unintended happened.

**Not verified anywhere:** none of this has been built as part of MLX, linked, run on Apple hardware,
or exercised against a real multi-device ring. That needs macOS and at least two devices. The Swift
half in `mlx-swift/0001` has only been through `swiftc -parse`, which is syntax alone and does not
resolve `import Cmlx`.
