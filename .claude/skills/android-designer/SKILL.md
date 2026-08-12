---
name: android-designer
description: GATED — /android-designer answers inline and spawns nothing. Covers the Android interface — Jetpack Compose, Material 3, the service notification and consent flows, all blocked until a non-MLX execution path exists. Use it to plan, or to ask an Android question, not to write code yet.
argument-hint: <interface question, or a state that will need presenting>
disable-model-invocation: true
---

This command answers inline. It does **not** fork `android-designer`, for the reason at the bottom.

## The gate

`android-designer` is **blocked**. MLX is Apple-only, so until the specification's Phase 1a — a portable graph
IR and a wire protocol — and a non-MLX execution path exist, an Android node has no runtime to run.
Adapter code written today would have nothing to execute it. Unblocking is Phase 1a, and that work
belongs to `senior-architect`.

## What was asked

$ARGUMENTS

If that asks for interface code, say what the gate is and stop. If it asks an Android question, or asks
what this role will need from the contract once it unblocks, answer it — that work is useful now, and
`.claude/agents/android-designer.md` carries the material to answer from.

The shape to plan for: Jetpack Compose and Material 3, with the foreground-service notification as a
first-class surface rather than an afterthought — §18.2 requires it for contribution visibility and
Android requires it for the service to run at all. Permission and consent flows are part of the
design, not a preamble to it.

## Why nothing spawns

Fork resolution falls back to `general-purpose` on an agent name it cannot resolve, and that
fallback is **silent** — no error anywhere. Answering inline is not a tool restriction: this body
runs with the caller's toolset, so it is the option that fails *visibly*, not the one that fails
safe. The gate is instructional — `.claude/agents/` refuses the work — and the gated definitions'
missing `Write`/`Edit` is defence in depth rather than enforcement, partial at that, since all nine
carry `Bash`. `findings.md` F19 records this; an earlier version of this file claimed the toolset
*was* the gate, which was wrong.

## Report

State what you verified and what you did not. `ShareComputeCore` is testable here with
`/opt/swift/usr/bin/swift test`; nothing about this adapter is, because it does not exist yet.
