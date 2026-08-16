# Patches

Changes to MLX that infer-ring needs in order to survive a node leaving the ring.

The app builds against a chain of three repositories, and the fixes span all three:

| Repo | Pinned at | Patches |
|---|---|---|
| [`mlx`](mlx/) | `38ad257088fb2193ad47e527cf6534a689f30943` | `0001` fail instead of hang · `0002` `finalize()` |
| [`mlx-c`](mlx-c/) | `0726ca922fc902c4c61ef9c27d94132be418e945` | `0001` export `group_free` + `finalize` |
| [`mlx-swift`](mlx-swift/) | tag `ios-distrib-0.3.0` (`c53d302`) | `0001` free the group, expose `finalize()` · `0002` declare both in the vendored `Cmlx` headers · `0003` define `FMT_CONSTEVAL` empty |

The two submodule pins are read from the mlx-swift tree at that tag; the tag itself is what
`project.pbxproj` names. (It names it as a *branch*, which it is not — see `findings.md` F13. That
is Stage 3's problem, not the patches'.)

`Cmlx` is a **source** SwiftPM target, not a prebuilt binary, so a patched submodule is compiled
directly. There is no CMake step and no artifact to regenerate.

## Forks

`joeydd032995-pixel/mlx`, `joeydd032995-pixel/mlx-c` and `joeydd032995-pixel/mlx-swift` are where
these patches land. **They have landed:**

| Fork | Branch | Tip |
|---|---|---|
| `mlx` | `sharecompute/stage1-2` | `18e53a6f9b88` |
| `mlx-c` | `sharecompute/export-finalize` | `55a61ffbe301` |
| `mlx-swift` | `sharecompute/free-and-finalize` | `c0b3b0264631` |

`project.pbxproj` points at the mlx-swift branch and defines `MLX_HAS_FINALIZE`, so Stage 3's
teardown compiles its `#if` branch rather than the `#else` that F20 forced. **Both Xcode jobs build
green against these branches** — see verification step 7.

[`land-mlx-swift-0002.sh`](land-mlx-swift-0002.sh) and
[`land-mlx-swift-0003.sh`](land-mlx-swift-0003.sh) added `0002` and `0003` to the already-published
mlx-swift branch, each as a fast-forward. Neither force-pushes, and re-running after a successful
push reports "already applied" rather than doing damage:

```bash
bash Patches/land-mlx-swift-0003.sh          # apply, show the diff, push nothing
bash Patches/land-mlx-swift-0003.sh --push   # and push
```

Without `--push` the script clones the fork to a temp directory, applies the patch, commits it
**locally**, prints the before→after SHAs and stops. The SHA it prints is real but unpublished, and
a `--push` run produces a *different* one because the commit timestamp is part of the hash. Check
the remote, not the printed SHA, to know whether it landed.

[`land-on-forks.sh`](land-on-forks.sh) is what put the branches there, and now applies both
mlx-swift patches on a fresh run:

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
  && git apply "$PATCHES/mlx-swift/0001-free-group-and-expose-finalize.patch" \
  && git apply "$PATCHES/mlx-swift/0002-declare-cmlx-symbols-in-vendored-headers.patch" \
  && git apply "$PATCHES/mlx-swift/0003-define-fmt-consteval-empty.patch" )
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

**Stage 2 (`mlx/0002` + `mlx-c/0001` + `mlx-swift/0001` + `mlx-swift/0002`) — the group can be rebuilt.** Stage 1 makes
loss *detectable*; it does not make it *survivable*, because `distributed::init` memoises the group
it creates and nothing can release it. Every re-init returns the stale group, so the ring re-forms
with the departed member still in it. Stage 2 adds the teardown that was missing.

Stage 2 does **not** by itself re-form the ring — that is Stage 3, in the app. It supplies the
operation Stage 3 needs and which `findings.md` Spike A recorded as unavailable.

**`mlx-swift/0002` is what makes the other three reachable from Swift.** mlx-swift does not include
the mlx-c submodule's headers. It vendors its own copies under `Source/Cmlx/include/` and
`Source/Cmlx/include-framework/`, and the `Cmlx` module map exposes exactly one header —
`include/mlx.h` — which reaches `include/mlx/c/distributed_group.h`, the *copy*. Patching the
submodule therefore compiles the definitions without declaring them, and `0001`'s Swift fails with
`cannot find 'mlx_distributed_group_free' in scope`. `0002` adds both declarations to both copies.
See `findings.md` F22.

**`mlx-swift/0003` is not part of either stage — it is a toolchain fix.** mlx-swift vendors fmt
10.2.1, which predates clang tightening how `consteval` propagates, so `FMT_STRING(...)` stops being
a constant expression and `fmt/src/format.cc` fails with five errors. `0003` defines `FMT_CONSTEVAL`
empty in the fork's `Package.swift` `cxxSettings` — a configuration fmt supports and applies itself
for toolchains where consteval misbehaves, not a workaround invented here.

It belongs in the fork rather than in build settings because **no CI setting can help a developer
who clones the repository and opens it in Xcode**. Before this, three CI jobs each carried their own
spelling of the same define and a fresh clone carried none. See `findings.md` F17 and F24.

**`finalize()` has a precondition, and it is the easy thing to get wrong.** It returns `false` and
changes nothing while *any* group handle is still held. Callers must release every one first — in
infer-ring that means the loaded `ModelContext` (whose sharded layers each retain the group, see
`findings.md` F14) **and** `MLXManager.group`, not just one of them. Then check the result: a `false`
means the teardown did not happen and a subsequent `init()` returns the same group, so proceeding as
though the ring were rebuilt would re-form it with stale membership. The order is: release every
handle → `finalize()` → only on `true`, re-`init()`.

## Verification

MLX is Apple-only and cannot be built on the Linux container this was written in, so verification is
in eight layers — steps 1–6 run here, steps 7 and 8 are macOS CI, which builds and (8) runs it.
Steps 1–6 were run against a fresh checkout produced by the block above, from the directory holding
the three sibling clones.

Steps 1–6 are kept rather than deleted now that step 7 exists. A green Xcode build says the whole
set works; it does not say *which* patch broke when one does, and it takes fifteen minutes on a
runner instead of seconds here. The negative controls in steps 2 and 6 also assert things a passing
build cannot: that the halves are genuinely coupled, and that a check would fail if the fix were
absent.

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

**4. Patches apply to pristine checkouts.** `git apply --check` for each of the six patches against
its pinned revision — this is what the block under *Applying* above does, with `git apply` in place
of `--check`.

**5. The forks resolve, and the resolved tree is the patched one.** Layers 1–4 test patches applied
locally, which is not the same as testing what the app checks out. This repeats the check against
the fork content itself, by performing SwiftPM's own operations:

```bash
git clone --branch sharecompute/free-and-finalize \
    https://github.com/joeydd032995-pixel/mlx-swift fullclone
cd fullclone
git fsck --connectivity-only
git submodule update --init Source/Cmlx/mlx Source/Cmlx/mlx-c
```

Full clone rather than shallow on purpose: `land-on-forks.sh` fetches `--depth=1` and pushes into a
fork whose network is `ml-explore/mlx-swift`, not `N1k1tung`, so an incomplete base history would
still pass a shallow fetch and fail only later, inside SwiftPM. It is complete — 422 commits,
`c53d302` as the parent, `fsck` clean. Both submodules check out at the two fork tips above, and
steps 1 and 2 above re-run green from that tree with the negative control still firing.

**6. The two new symbols are reachable through the header Swift actually sees.** Layers 1–2 compile
the mlx-c translation unit, which proves the *definitions* exist. It does not prove Swift can
*declare* them, because the `Cmlx` module map exposes only `Source/Cmlx/include/mlx.h` and that
chain reaches mlx-swift's vendored header copy rather than the submodule's. Missing this is what
failed CI; the check is a C file that reaches both symbols the same way Swift does:

```bash
cat > /tmp/chain.c <<'EOF'
#include "mlx.h"
int probe(mlx_distributed_group g) {
    int r = mlx_distributed_group_free(g);
    return r + (mlx_distributed_finalize() ? 1 : 0);
}
EOF
gcc -fsyntax-only -Werror=implicit-function-declaration -I Source/Cmlx/include /tmp/chain.c
```

Run from an mlx-swift checkout: it must pass with `0002` applied and fail without it. `-Werror=` is
required — C would otherwise accept both as implicit declarations and pass, which is precisely the
false green that let the gap through.

Separately, `swift test` at the repository root stays green (67 tests) — these patches touch no
Swift in `ShareComputeCore`, so any change there would mean something unintended happened.

**7. The whole set builds as part of MLX, under a real Apple toolchain.** This is CI, not a local
check, and it is what the layers above could never establish. With the project pointed at the
patched fork and `MLX_HAS_FINALIZE` defined, both Xcode jobs pass:

| Job | Result |
|---|---|
| Build and analyse the Infer Ring scheme — `arm64-apple-macos` | `▸ Analyze Succeeded` |
| Build for iOS Simulator — `arm64` | succeeded |

That covers three things the Linux layers cannot: the patched C++ compiles under `clang` for Apple
targets rather than `g++` for x86_64 Linux; `mlx_distributed_group_free` and
`mlx_distributed_finalize` **link**, with the latter reaching `mlx::core::distributed::finalize()`
across all three repositories; and `DistributedGroup.swift` type-checks with `import Cmlx` genuinely
resolved, which `swiftc -parse` never did.

**8. ARC's release is observed by the C++ use count.** [`mlx-swift/tests/`](mlx-swift/tests/) — a
standalone SwiftPM package, run by the `MLX lifecycle` CI job on macOS. The first check to execute
the real patched code rather than a mirror of it, crossing all three repositories in one assertion:
Swift `deinit` → `mlx_distributed_group_free` → `mlx::core::distributed::finalize()`.

It checks the precondition every caller of `finalize()` depends on, and which `MLXManager.teardown()`
is built on. It **cannot** check that a re-`init()` returns a genuinely new group — that needs two
processes, see `findings.md` F23 and the note in the test file.

*Status: written, and it has never run.* The job is new; treat this row as unproven until it is
green.

**Not verified anywhere: essentially none of this has been _run_.** Building is not behaving. No
ring has formed, `~RingGroup()` and `~SocketThread()` have never executed against a peer that has
already gone, and the Stage 1 fail-instead-of-hang path has never fired on a real socket. The
harnesses mirror that behaviour against real primitives, which is why they exist, but a mirror is
not the thing. That needs macOS and at least two devices — see *What to run on hardware* in
`task_plan.md`.
