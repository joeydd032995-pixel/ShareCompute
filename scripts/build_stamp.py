#!/usr/bin/env python3
"""Which build produced an artifact.

Three physical runs (F39, F42, F43) could not say which kit the operator used, because nothing in a
report named a commit. F42's cause is unsettleable for that reason. Every component now stamps
itself, and a stamp that cannot be resolved says `unknown` rather than guessing.

Resolution order, most to least trustworthy:
  1. `build-stamp.json` beside the code — written at build time, so a packaged kit carries the
     commit it was built from even with no git and no network.
  2. `git rev-parse` in this checkout — for someone running from source.
  3. `unknown` — recorded as such, never invented.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

UNKNOWN = 'unknown'
STAMP_FILE = 'build-stamp.json'

def _git(args, cwd):
    try:
        out = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None

def from_git(root):
    """The commit of a source checkout, with a flag for uncommitted changes."""
    commit = _git(['rev-parse', 'HEAD'], root)
    if not commit: return None
    # --porcelain lists tracked changes; an empty result means the tree matches the commit.
    status = _git(['status', '--porcelain'], root)
    return {'commit': commit, 'short': commit[:7], 'dirty': bool(status), 'source': 'git'}

def read(root):
    """The stamp for the code under `root`: a file written at build time, else git, else unknown."""
    root = Path(root)
    path = root / STAMP_FILE
    if path.is_file():
        try:
            stamp = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(stamp, dict) and isinstance(stamp.get('commit'), str):
                stamp.setdefault('short', stamp['commit'][:7]); stamp.setdefault('source', 'file')
                return stamp
        except (OSError, ValueError):
            pass  # A damaged stamp is unknown provenance, not a reason to fail a run.
    return from_git(root) or {'commit': UNKNOWN, 'short': UNKNOWN, 'dirty': False, 'source': UNKNOWN}

def resolve(root):
    """The commit to stamp a build with: CI's own, else this checkout's, else unknown."""
    # GitHub Actions checks out a detached HEAD, so GITHUB_SHA is the authority there.
    for key in ('SC_BUILD_COMMIT', 'GITHUB_SHA'):
        value = os.environ.get(key, '').strip()
        if len(value) == 40 and all(c in '0123456789abcdef' for c in value.lower()):
            return {'commit': value.lower(), 'short': value[:7].lower(), 'dirty': False, 'source': key}
    return from_git(root) or {'commit': UNKNOWN, 'short': UNKNOWN, 'dirty': False, 'source': UNKNOWN}

def write(directory, root, extra=None):
    """Stamp a build output directory. Called by the build scripts, not at run time."""
    import datetime
    stamp = resolve(root)
    stamp['built_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    stamp.update(extra or {})
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    (directory / STAMP_FILE).write_text(json.dumps(stamp, indent=2) + '\n', encoding='utf-8')
    return stamp

def short_commit(value):
    """Seven characters of a commit, or `unknown` for anything that is not one."""
    return value[:7] if isinstance(value, str) and value and value != UNKNOWN else UNKNOWN

def describe(stamp):
    """One short token for a log line: `c7fb789`, `c7fb789-dirty`, or `unknown`."""
    if not isinstance(stamp, dict): return UNKNOWN
    short = short_commit(stamp.get('short') or stamp.get('commit'))
    return f'{short}-dirty' if stamp.get('dirty') and short != UNKNOWN else short

def compare(stamps):
    """Which components disagree about their build.

    `stamps` maps a component name to its stamp. Returns the distinct known commits and the names
    whose provenance is unknown. Two components built from different commits are not comparable
    evidence; a component that cannot name its build is not comparable either, but for a different
    reason, so the two are reported separately rather than merged into one boolean.
    """
    known = {}; unknown = []
    for name, stamp in stamps.items():
        commit = (stamp or {}).get('commit')
        if isinstance(commit, str) and commit != UNKNOWN and commit: known.setdefault(commit, []).append(name)
        else: unknown.append(name)
    return {'commits': {c: sorted(n) for c, n in known.items()}, 'unknown': sorted(unknown),
            'mismatch': len(known) > 1, 'complete': not unknown and len(known) == 1}

if __name__ == '__main__':
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    print(json.dumps(resolve(root), indent=2))
