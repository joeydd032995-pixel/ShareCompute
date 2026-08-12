---
name: linux-designer
description: GATED — /linux-designer answers inline and spawns nothing. Covers the Linux operator experience — CLI and TUI shape, config file design and log output, all blocked until a non-MLX execution path exists. Use it to plan, or to ask a Linux question, not to write code yet.
argument-hint: <interface question, or a state that will need presenting>
disable-model-invocation: true
---

This command answers inline. It does **not** fork `linux-designer`, for the reason at the bottom.

## The gate

`linux-designer` is **blocked**. MLX is Apple-only, so until the specification's Phase 1a — a portable graph
IR and a wire protocol — and a non-MLX execution path exist, a Linux node has no runtime to run.
Adapter code written today would have nothing to execute it. Unblocking is Phase 1a, and that work
belongs to `senior-architect`.

## What was asked

$ARGUMENTS

If that asks for interface code, say what the gate is and stop. If it asks a Linux question, or asks
what this role will need from the contract once it unblocks, answer it — that work is useful now, and
`.claude/agents/linux-designer.md` carries the material to answer from.

The shape to plan for: CLI and TUI rather than GTK or Qt, plus the config-file and log-output design —
on a headless node those *are* the interface. `RingHealth.stalled` means still working, so never
render it as broken; a large prefill legitimately produces nothing for many seconds.

## Why nothing spawns

Fork resolution falls back to `general-purpose` on an agent name it cannot resolve, and
`general-purpose` has `Write`. The nine gated roles are read-only by construction, and that toolset
*is* the gate — so these commands answer here rather than risk handing a blocked role write access.

## Report

State what you verified and what you did not. `ShareComputeCore` is testable here with
`/opt/swift/usr/bin/swift test`; nothing about this adapter is, because it does not exist yet.
