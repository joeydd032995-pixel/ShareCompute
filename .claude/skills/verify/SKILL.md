---
name: verify
description: Run ShareCompute's verification matrix — the five checks that actually work in this container, with their expected results — and report honestly which half of the project each one does and does not cover.
argument-hint: <optional — a specific claim to check, otherwise runs everything>
---

# Verifying ShareCompute

This is a **project skill named `verify`, and it deliberately shadows the built-in one.** A project
skill silently replaces a same-named bundled skill, which is normally a reason never to create one;
`verify` is the documented exception, because how a project verifies changes is project-specific.
What follows is this project's answer, not a general one.

The reason it has to be project-specific here: **this container is x86_64 Linux with no macOS, no
Xcode, no Android SDK and no Windows.** Most of this repository targets Apple hardware, so the
honest output of a verification run is two lists, not one.

## $ARGUMENTS

If that names a specific claim, check *that* and say which of the commands below bears on it — many
claims in this repo have no command that bears on them at all, and saying so is the answer. If it is
empty, run all five.

## The five that work

Run from the repository root. Expected results are exact; a different number is a finding, not a
rounding error.

```bash
/opt/swift/usr/bin/swift test                    # 66 tests, 0 failures
```

```bash
# Subshell, so the cd does not leak into the next block — the same idiom Patches/README.md
# uses for its sibling clones.
( cd Patches/mlx/tests
  g++ -std=c++17 -O1 -pthread -o /tmp/stf socket_thread_failure_test.cpp && /tmp/stf  # 16 checks
  g++ -std=c++17 -O1 -pthread -o /tmp/gft group_finalize_test.cpp        && /tmp/gft  # 31 checks
  g++ -std=c++17 -O1 -g -fsanitize=thread -pthread \
      -o /tmp/gft_tsan group_finalize_test.cpp && /tmp/gft_tsan         # 31 checks, TSan clean
)
```

```bash
python3 scripts/validate-agents.py    # 20 agents (9 gated), 24 skills (20 role, 3 workflow, 1 router)
```

Two things about the harnesses are easy to misread. They **mirror** the patched MLX code rather than
including it — MLX cannot be compiled here — so a green run proves the *semantics*, not that the
patch files still match. If a patched file changes, its harness must change in step or it silently
starts testing the old design. And each one ends with a case reproducing the **unpatched** behaviour,
so the defect is pinned rather than merely described; if that case starts passing, the harness has
stopped testing anything.

The TSan run is a real control, not a formality: deleting the two `lock_guard` lines from the Stage 2
harness makes the same case report 43 data races.

For the MLX patch set specifically — apply order, the compile sequence, the negative control — use
`/patches`. It covers ground this skill does not.

## The half that does not work here

| Claim | Checkable here | What it would need |
|---|---|---|
| `ShareComputeCore` behaviour | yes | already covered above |
| MLX patch semantics | yes | already covered above |
| Apple Swift **type-checks** | no | macOS + Xcode. `swiftc -parse` is syntax only and has repeatedly passed code that does not compile — see `findings.md` F18 |
| Xcode project builds | no | macOS + Xcode |
| Actor isolation at runtime | no | Apple hardware |
| A ring actually forms | no | two or more real devices |
| Any MLX patch at runtime | no | macOS; **none of the four has ever been executed** |
| Linux / Windows / Android | no | those SDKs, and a non-MLX runtime that does not exist yet |
| Slash commands at **dispatch** | no | an interactive session. `validate-agents.py` checks the *files*, never the behaviour: not that a fork spawns the named agent, not that `background: false` blocks, not that this skill actually shadows the built-in |

## Report

**State what you verified and what you did not.** Quote the commands you actually ran, verbatim,
with their real output — never paraphrase a command you did not execute, and never let a green run
of the first table imply anything about the second. Silence about the unverified half is treated as
a defect in this project, not an omission.
