#!/usr/bin/env bash
#
# Land the four ShareCompute patches on the forks, and repoint mlx-swift's submodules at them.
#
# Why this exists: the app resolves mlx-swift as a SwiftPM dependency, so the patches only take
# effect once they are commits on a branch the project can point at. Until then
# `DistributedGroup.finalize()` does not exist in any build, and Stage 3's teardown path compiles
# only its `#else` branch (see findings.md F20).
#
#   bash Patches/land-on-forks.sh            # apply everything, stop before pushing
#   bash Patches/land-on-forks.sh --push     # apply and push all three branches
#
# Everything except the push has been run in CI's Linux container; the push needs your credentials.
#
# Requires: git, and push access to the three joeydd032995-pixel forks.

set -euo pipefail

OWNER=${OWNER:-joeydd032995-pixel}
UPSTREAM_SWIFT=${UPSTREAM_SWIFT:-https://github.com/N1k1tung/mlx-swift}
PUSH=0
[ "${1:-}" = "--push" ] && PUSH=1

PATCHES=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK=${WORK:-$(mktemp -d)}

# The revisions the patches were written against. Do not "update" these casually: a patch that no
# longer applies is the signal that upstream moved, which is the whole reason Patches/ keeps them
# as files rather than as a silently-diverging fork.
MLX_PIN=38ad257088fb2193ad47e527cf6534a689f30943
MLXC_PIN=0726ca922fc902c4c61ef9c27d94132be418e945
SWIFT_PIN=c53d302197489acbb6b3a81dc1635d0aae75b163   # tag ios-distrib-0.3.0 on N1k1tung/mlx-swift

MLX_BRANCH=sharecompute/stage1-2
MLXC_BRANCH=sharecompute/export-finalize
SWIFT_BRANCH=sharecompute/free-and-finalize

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# Fetch one commit shallowly and branch from it. Shallow because mlx is large and only the tree at
# the pin is needed.
prepare() { # <dir> <url> <sha> <branch>
    mkdir -p "$WORK/$1" && cd "$WORK/$1" && git init -q .
    git fetch --depth=1 -q "$2" "$3"
    git checkout -q -b "$4" FETCH_HEAD
}

# --check first, so a stale patch reports rather than half-applying.
apply_patch() { # <patch path>
    printf '   %-40s ' "$(basename "$1" .patch)"
    git apply --check "$1"
    git apply "$1"
    echo "applied"
}

commit_as() { git -c user.email=sharecompute@local -c user.name=ShareCompute commit -q -am "$1"; }

say "mlx  @ ${MLX_PIN:0:7}  ->  $MLX_BRANCH"
prepare mlx "https://github.com/$OWNER/mlx" "$MLX_PIN" "$MLX_BRANCH"
apply_patch "$PATCHES/mlx/0001-ring-fail-instead-of-hang.patch"
apply_patch "$PATCHES/mlx/0002-distributed-finalize.patch"
commit_as "ShareCompute Stage 1+2: ring fails instead of hanging; distributed finalize()"
MLX_SHA=$(git rev-parse HEAD)

# mlx-c after mlx: its patch calls mlx::core::distributed::finalize(), which mlx/0002 declares.
# Applying it first gives "'finalize' is not a member of 'mlx::core::distributed'".
say "mlx-c  @ ${MLXC_PIN:0:7}  ->  $MLXC_BRANCH"
prepare mlx-c "https://github.com/$OWNER/mlx-c" "$MLXC_PIN" "$MLXC_BRANCH"
apply_patch "$PATCHES/mlx-c/0001-export-group-free-and-finalize.patch"
commit_as "ShareCompute: export mlx_distributed_group_free and mlx_distributed_finalize"
MLXC_SHA=$(git rev-parse HEAD)

# mlx-swift comes from N1k1tung, not from the $OWNER fork: that fork was taken from ml-explore and
# does not contain this commit or the ios-distrib-0.3.0 tag. Pushing unrelated history to a branch
# in your own fork is fine -- git does not require it to share ancestry with main.
say "mlx-swift  @ ${SWIFT_PIN:0:7} (from N1k1tung)  ->  $SWIFT_BRANCH"
prepare mlx-swift "$UPSTREAM_SWIFT" "$SWIFT_PIN" "$SWIFT_BRANCH"
apply_patch "$PATCHES/mlx-swift/0001-free-group-and-expose-finalize.patch"

# Repoint the vendored submodules at the patched commits. Both halves are needed: the gitlink says
# *which commit*, .gitmodules says *which repository* -- and the patched commits exist only in the
# forks, so leaving the ml-explore URLs would make the pins unresolvable.
git config -f .gitmodules submodule."submodules/mlx".url    "https://github.com/$OWNER/mlx"
git config -f .gitmodules submodule."submodules/mlx-c".url  "https://github.com/$OWNER/mlx-c"
git update-index --cacheinfo 160000,"$MLX_SHA",Source/Cmlx/mlx
git update-index --cacheinfo 160000,"$MLXC_SHA",Source/Cmlx/mlx-c
git add .gitmodules
commit_as "ShareCompute: free the group in deinit, expose finalize(), pin patched submodules"
SWIFT_SHA=$(git rev-parse HEAD)

say "Result"
printf '  %-10s %s  %s\n' mlx       "${MLX_SHA:0:12}"   "$MLX_BRANCH"
printf '  %-10s %s  %s\n' mlx-c     "${MLXC_SHA:0:12}"  "$MLXC_BRANCH"
printf '  %-10s %s  %s\n' mlx-swift "${SWIFT_SHA:0:12}" "$SWIFT_BRANCH"
echo "  worktrees: $WORK"

if [ "$PUSH" -eq 0 ]; then
    echo
    echo "  Nothing pushed. Re-run with --push once the above looks right."
    exit 0
fi

# Push mlx and mlx-c before mlx-swift: its gitlinks reference those commits, so a consumer that
# fetched mlx-swift first would find pins that do not resolve yet.
say "Pushing"
for repo_branch in "mlx:$MLX_BRANCH" "mlx-c:$MLXC_BRANCH" "mlx-swift:$SWIFT_BRANCH"; do
    repo=${repo_branch%%:*}; branch=${repo_branch#*:}
    cd "$WORK/$repo"
    # Refuse rather than clobber: if the branch is already there, something else put it there.
    if git ls-remote --exit-code --heads "https://github.com/$OWNER/$repo" "$branch" >/dev/null 2>&1; then
        echo "  $repo: $branch already exists on the remote -- not overwriting. Delete it or pick another name."
        exit 1
    fi
    git push -q "https://github.com/$OWNER/$repo" "$branch"
    echo "  $repo -> $branch pushed"
done

say "Next"
echo "  Repoint Apps/InferRing/Infer Ring.xcodeproj/project.pbxproj at:"
echo "    https://github.com/$OWNER/mlx-swift   branch = $SWIFT_BRANCH"
echo "  and pass MLX_HAS_FINALIZE in the Xcode CI jobs so the gated teardown path compiles."
