---
name: windows-developer
description: GATED — /windows-developer answers inline and spawns nothing. Covers the Windows Service host — service registration, firewall rules and native mDNS in place of Bonjour, all blocked until a non-MLX execution path exists. Use it to plan, or to ask a Windows question, not to write code yet.
argument-hint: <host-process question, or what the daemon or service will need>
disable-model-invocation: true
---

This command answers inline. It does **not** fork `windows-developer`, for the reason at the bottom.

## The gate

`windows-developer` is **blocked**. MLX is Apple-only, so until the specification's Phase 1a — a portable graph
IR and a wire protocol — and a non-MLX execution path exist, a Windows node has no runtime to run.
Adapter code written today would have nothing to execute it. Unblocking is Phase 1a, and that work
belongs to `senior-architect`.

## What was asked

$ARGUMENTS

If that asks for host-process code, say what the gate is and stop. If it asks a Windows question, or asks
what this role will need from the contract once it unblocks, answer it — that work is useful now, and
`.claude/agents/windows-developer.md` carries the material to answer from.

The shape to plan for: a Windows Service with an elevated install step, an inbound firewall rule for
the control plane, and native mDNS in place of Bonjour — `Network.framework` does not exist here, so
discovery is a rewrite rather than a port of the Apple path.

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
