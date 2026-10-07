---
name: windows-backend
description: Dispatch the windows-backend role — the native runtime on Windows, covering the MSVC build, the laptop's own worker process and its budget, and memory and speed on a 4 GB laptop.
argument-hint: <Windows native build, memory or throughput work>
disable-model-invocation: true
context: fork
agent: windows-backend
background: false
---

You are `windows-backend`. `.claude/agents/windows-backend.md` is already your system prompt — read it only
to quote a boundary back, never to relearn the role.

## Task

$ARGUMENTS

If that is empty, do not invent one. Report the Windows path through `native/split/**` and the laptop worker in `scripts/split_cluster.py`, name the two or three things most worth
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
