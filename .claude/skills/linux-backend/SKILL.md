---
name: linux-backend
description: GATED — /linux-backend answers inline and spawns nothing. Covers the Linux adapter — cgroup memory accounting, OOM-risk reporting and llama.cpp or ONNX Runtime backends, all blocked until a non-MLX execution path exists. Use it to plan, or to ask a Linux question, not to write code yet.
argument-hint: <platform question, or what the adapter will need from the contract>
disable-model-invocation: true
---

This command answers inline. It does **not** fork `linux-backend`, for the reason at the bottom.

## The gate

`linux-backend` is **blocked**. MLX is Apple-only, so until the specification's Phase 1a — a portable graph
IR and a wire protocol — and a non-MLX execution path exist, a Linux node has no runtime to run.
Adapter code written today would have nothing to execute it. Unblocking is Phase 1a, and that work
belongs to `senior-architect`.

## What was asked

$ARGUMENTS

If that asks for adapter code, say what the gate is and stop. If it asks a Linux question, or asks
what this role will need from the contract once it unblocks, answer it — that work is useful now, and
`.claude/agents/linux-backend.md` carries the material to answer from.

The one Linux fact worth carrying now: cgroup `memory.current` counts page cache as well as RSS, so
an mmap-heavy model can be OOM-killed while the resident set alone still looks safe. Linux **kills**
where Windows trims and macOS pages, which is why `MemoryReclaimModel.linuxCgroupOOM` is its own case
in `CapabilityProfile` and why placement has to stay conservative near the limit.

## Why nothing spawns

Fork resolution falls back to `general-purpose` on an agent name it cannot resolve, and
`general-purpose` has `Write`. The nine gated roles are read-only by construction, and that toolset
*is* the gate — so these commands answer here rather than risk handing a blocked role write access.

## Report

State what you verified and what you did not. `ShareComputeCore` is testable here with
`/opt/swift/usr/bin/swift test`; nothing about this adapter is, because it does not exist yet.
