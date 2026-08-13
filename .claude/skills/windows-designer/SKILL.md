---
name: windows-designer
description: GATED — /windows-designer answers inline and spawns nothing. Covers the Windows interface — WinUI 3 or a tray application, Fluent conventions and the operator experience, all blocked until a non-MLX execution path exists. Use it to plan, or to ask a Windows question, not to write code yet.
argument-hint: <interface question, or a state that will need presenting>
disable-model-invocation: true
---

This command answers inline. It does **not** fork `windows-designer`, for the reason at the bottom.

## The gate

`windows-designer` is **blocked**. MLX is Apple-only, so until the specification's Phase 1a — a portable graph
IR and a wire protocol — and a non-MLX execution path exist, a Windows node has no runtime to run.
Adapter code written today would have nothing to execute it. Unblocking is Phase 1a, and that work
belongs to `senior-architect`.

## What was asked

$ARGUMENTS

If that asks for interface code, say what the gate is and stop. If it asks a Windows question, or asks
what this role will need from the contract once it unblocks, answer it — that work is useful now, and
`.claude/agents/windows-designer.md` carries the material to answer from.

The shape to plan for: most likely a tray application plus a settings window rather than a full app,
since the node ships as a service — settle that with `windows-developer` before designing either.
Toast is the Windows analogue of the foreground-service notification §18.2 requires for contribution
visibility.

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
