#!/bin/sh
# Writes the ShareCompute commit this app is built from into the built Info.plist as SCBuildCommit.
#
# Resolution order mirrors scripts/build_stamp.py `resolve`: SC_BUILD_COMMIT, then GITHUB_SHA (each
# only if it is exactly 40 hex characters), then `git rev-parse HEAD` in this checkout, then the
# literal "unknown". It never invents a value: "unknown" is the honest answer, and the laptop treats
# it as missing provenance rather than a contradiction.
#
# Runs as the last build phase, before code signing, so the signed bundle carries the value.
# Environment variables set around `xcodebuild` (CI sets GITHUB_SHA) reach this script as-is.
is_commit() { printf '%s' "$1" | grep -Eq '^[0-9a-fA-F]{40}$'; }

commit=unknown
for candidate in "$SC_BUILD_COMMIT" "$GITHUB_SHA"; do
  if is_commit "$candidate"; then
    commit=$(printf '%s' "$candidate" | tr 'A-F' 'a-f')
    break
  fi
done
if [ "$commit" = unknown ]; then
  head=$(git -C "$SRCROOT" rev-parse HEAD 2>/dev/null)
  if is_commit "$head"; then commit=$(printf '%s' "$head" | tr 'A-F' 'a-f'); fi
fi

# Print only the resolved value when asked, so the resolution can be tested without Xcode.
if [ "$1" = --print ]; then echo "$commit"; exit 0; fi

plist="$TARGET_BUILD_DIR/$INFOPLIST_PATH"
/usr/libexec/PlistBuddy -c "Set :SCBuildCommit $commit" "$plist" ||
  /usr/libexec/PlistBuddy -c "Add :SCBuildCommit string $commit" "$plist" || exit 1
echo "SCBuildCommit=$commit"
