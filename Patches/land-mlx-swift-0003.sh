#!/usr/bin/env bash
#
# Land Patches/mlx-swift/0003 on the existing fork branch.
#
# What it fixes: mlx-swift vendors fmt 10.2.1, which current clang rejects -- FMT_STRING(...) is no
# longer a constant expression, five errors in fmt/src/format.cc. Until now three CI jobs each
# carried their own spelling of -DFMT_CONSTEVAL= to work around it, and anyone opening the project
# in Xcode carried none and hit the error. This puts the define in the one place every consumer
# already resolves. See findings.md F17 and F24.
#
#   bash Patches/land-mlx-swift-0003.sh          # apply, show the diff, push nothing
#   bash Patches/land-mlx-swift-0003.sh --push   # and push
#
# Fast-forward only; it never force-pushes, and re-running after a successful push reports
# "already applied" rather than doing damage.
#
# Requires: git, and push access to joeydd032995-pixel/mlx-swift.

set -euo pipefail

OWNER=${OWNER:-joeydd032995-pixel}
BRANCH=${BRANCH:-sharecompute/free-and-finalize}
PUSH=0
[ "${1:-}" = "--push" ] && PUSH=1

PATCHES=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PATCH="$PATCHES/mlx-swift/0003-define-fmt-consteval-empty.patch"
WORK=${WORK:-$(mktemp -d)}

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "Cloning $OWNER/mlx-swift @ $BRANCH"
# Full clone, not shallow: pushing a fast-forward needs the history the remote already has.
git clone -q --branch "$BRANCH" "https://github.com/$OWNER/mlx-swift" "$WORK/mlx-swift"
cd "$WORK/mlx-swift"
BEFORE=$(git rev-parse HEAD)
echo "  at ${BEFORE:0:12}"

say "Applying 0003"
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
    commit -q -am "ShareCompute: define FMT_CONSTEVAL empty so vendored fmt builds on current clang"
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
git push -q "https://github.com/$OWNER/mlx-swift" "$BRANCH"
echo "  pushed $BRANCH -> ${AFTER:0:12}"

say "Next"
echo "  Once CI is green against this, the -DFMT_CONSTEVAL= flag can come out of all three jobs:"
echo "    .github/workflows/ios.yml"
echo "    .github/workflows/objective-c-xcode.yml"
echo "    .github/workflows/mlx-lifecycle.yml"
echo "  Remove them only AFTER this is on the fork -- removing first breaks every build."
