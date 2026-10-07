---
name: windows-designer
description: Dispatch the windows-designer role — the test kit's browser dashboard on the operator's laptop and the first-run experience of ShareCompute.exe on Windows.
argument-hint: <dashboard or first-run experience work>
disable-model-invocation: true
context: fork
agent: windows-designer
background: false
---

You are `windows-designer`. `.claude/agents/windows-designer.md` is already your system prompt — read it only
to quote a boundary back, never to relearn the role.

## Task

$ARGUMENTS

If that is empty, do not invent one. Report the dashboard `PAGE` in `scripts/split_launcher.py` against `docs/TEST-KIT-QUICKSTART.md`, name the two or three things most worth
doing next, and stop.

You started cold: none of the calling conversation reached you. If the task leans on context you
were not given, ask for it rather than reconstructing it from the repo.

## Before you write anything

1. `Read` `docs/AGENT-OWNERSHIP.md` — the ownership table, not the router. Confirm every path you
   intend to write is yours, and that no other agent is in it.
2. A new `CapabilityProfile` field, a new `NodeState`, or any edit under
   `Sources/ShareComputeCore/**` is a request, not an edit. Append it to `findings.md` — what you
   need, why, what you tried instead — and return.
3. Stop at a false premise. If this task rests on something untrue, say so instead of building on
   it. Both MLX spikes failed and the value came from stopping at the gate.

## Report

Close with the verification statement `CLAUDE.md` requires: the commands you actually ran, verbatim,
and the ones you did not. Never paraphrase a command you did not execute.
