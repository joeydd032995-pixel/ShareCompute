---
name: android-backend
description: GATED — /android-backend answers inline and spawns nothing. Covers the Android adapter — Doze and App Standby handling and the LiteRT or ORT-Mobile delegates, all blocked until a non-MLX execution path exists. Use it to plan, or to ask an Android question, not to write code yet.
argument-hint: <platform question, or what the adapter will need from the contract>
disable-model-invocation: true
---

This command answers inline. It does **not** fork `android-backend`, for the reason at the bottom.

## The gate

`android-backend` is **blocked**. MLX is Apple-only, so until the specification's Phase 1a — a portable graph
IR and a wire protocol — and a non-MLX execution path exist, an Android node has no runtime to run.
Adapter code written today would have nothing to execute it. Unblocking is Phase 1a, and that work
belongs to `senior-architect`.

## What was asked

$ARGUMENTS

If that asks for adapter code, say what the gate is and stop. If it asks an Android question, or asks
what this role will need from the contract once it unblocks, answer it — that work is useful now, and
`.claude/agents/android-backend.md` carries the material to answer from.

The one Android fact worth carrying now: Doze and App Standby suspend network access on an idle
device. That is the same class of failure as iOS backgrounding on a longer timescale, so the lease
clamp and drain-on-background pattern in `ios-backend` transfers directly. LiteRT and ORT-Mobile are
the candidate delegates once there is a graph to execute.

## Why nothing spawns

Fork resolution falls back to `general-purpose` on an agent name it cannot resolve, and that
fallback is **silent** — no error anywhere. Answering inline is not a tool restriction: this body
runs with the caller's toolset, so it is the option that fails *visibly*, not the one that fails
safe. On this path the gated definition's `tools:` list is **not consulted at all** — the agent is
never spawned — so its missing `Write`/`Edit` restrains nothing here. (It does apply when the role is
forked through the `Agent` tool, though `Bash` still writes even then.) The gate is instructional:
`.claude/agents/` states the block and refuses the work. `findings.md` F19 records this; an earlier
version of this file claimed the toolset *was* the gate, which was wrong.

## Report

State what you verified and what you did not. `ShareComputeCore` is testable here with
`/opt/swift/usr/bin/swift test`; nothing about this adapter is, because it does not exist yet.
