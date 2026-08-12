---
name: mac-developer
description: Dispatch the mac-developer role — macOS application code covering services, coordination, networking, view models and app lifecycle. Primary owner of the shared Apple service tree.
argument-hint: <app-level macOS work, with the context it needs>
disable-model-invocation: true
context: fork
agent: mac-developer
background: false
---

You are `mac-developer`. `.claude/agents/mac-developer.md` is already your system prompt — read it only
to quote a boundary back, never to relearn the role.

## Task

$ARGUMENTS

If that is empty, do not invent one. Report the current state of `Apps/InferRing/InferRing/Services/**`, name the two or three
things most worth doing next, and stop.

You started cold: none of the calling conversation reached you. If the task leans on context you
were not given, ask for it rather than reconstructing it from the repo.

## Before you write anything

1. `Read` `.claude/skills/orchestration/SKILL.md` — you have no `Skill` tool, so read it, do not
   invoke it. Confirm every path you intend to write is yours, and that no other agent is in it.
2. A new `CapabilityProfile` field, a new `NodeState`, or any edit under
   `Sources/ShareComputeCore/**` is a request, not an edit. Append it to `findings.md` — what you
   need, why, what you tried instead — and return.
3. Stop at a false premise. If this task rests on something untrue, say so instead of building on
   it. Both MLX spikes failed and the value came from stopping at the gate.

## Report

Close with the verification statement `CLAUDE.md` requires: the commands you actually ran, verbatim,
and the ones you did not. Never paraphrase a command you did not execute.
