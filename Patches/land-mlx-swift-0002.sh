#!/usr/bin/env bash
#
# Land Patches/mlx-swift/0002 on the existing fork branch.
#
# Why this is separate from land-on-forks.sh: that script builds the three branches from scratch and
# refuses to touch a branch that already exists. The branches exist now, so this adds one commit on
# top of sharecompute/free-and-finalize rather than rebuilding it. Fast-forward only -- it never
# force-pushes, so it cannot lose whatever is already there.
#
#   bash Patches/land-mlx-swift-0002.sh          # apply, show the diff, push nothing
#   bash Patches/land-mlx-swift-0002.sh --push   # and push
#
# Only mlx-swift changes. The mlx and mlx-c branches are untouched and still correct.
#
# Requires: git, and push access to joeydd032995-pixel/mlx-swift.

set -euo pipefail

OWNER=${OWNER:-joeydd032995-pixel}
BRANCH=${BRANCH:-sharecompute/free-and-finalize}
PUSH=0
[ "${1:-}" = "--push" ] && PUSH=1

PATCHES=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PATCH="$PATCHES/mlx-swift/0002-declare-cmlx-symbols-in-vendored-headers.patch"
WORK=${WORK:-$(mktemp -d)}

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "Cloning $OWNER/mlx-swift @ $BRANCH"
# Full clone, not shallow: pushing a fast-forward needs the history the remote already has.
git clone -q --branch "$BRANCH" "https://github.com/$OWNER/mlx-swift" "$WORK/mlx-swift"
cd "$WORK/mlx-swift"
BEFORE=$(git rev-parse HEAD)
echo "  at ${BEFORE:0:12}"

say "Applying 0002"
# --check first so a stale patch reports rather than half-applying.
if ! git apply --check "$PATCH" 2>/dev/null; then
    if git apply --reverse --check "$PATCH" 2>/dev/null; then
        echo "  Already applied on $BRANCH -- nothing to do."
        exit 0
    fi
    echo "  Patch does not apply. The branch is not where this expects it; stopping." >&2
    exit 1
fi
git apply "$PATCH"
git -c user.email=sharecompute@local -c user.name=ShareCompute \
    commit -q -am "ShareCompute: declare group_free and finalize in the vendored Cmlx headers"
AFTER=$(git rev-parse HEAD)

say "Result"
git --no-pager diff --stat "$BEFORE" "$AFTER"
printf '  %s -> %s  %s\n' "${BEFORE:0:12}" "${AFTER:0:12}" "$BRANCH"
echo "  worktree: $WORK/mlx-swift"

if [ "$PUSH" -eq 0 ]; then
    echo
    echo "  Nothing pushed. Re-run with --push once the above looks right."
    exit 0
fi

say "Pushing"
# No --force anywhere: this is a fast-forward on top of what is already published.
git push -q "https://github.com/$OWNER/mlx-swift" "$BRANCH"
echo "  pushed $BRANCH -> ${AFTER:0:12}"

say "Next"
echo "  Nothing to change in ShareCompute -- project.pbxproj already points at $BRANCH,"
echo "  so the next CI run picks this up. Re-run the two Xcode jobs."
