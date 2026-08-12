---
name: findings
description: Append a finding to ShareCompute's research log correctly — the next F-number, evidence with file and line references, and the mandatory statement of what was not verified. Never rewrite an existing finding.
argument-hint: <what was discovered, and how you established it>
---

# Appending to `findings.md`

`findings.md` is the load-bearing document in this repository. `CLAUDE.md` points every agent at it,
and its purpose is that expensive facts are established **once**. A finding that is vague, or that
overstates its evidence, costs more than no finding at all — the next session builds on it.

## $ARGUMENTS

That is the discovery. If it is empty, do not invent one: report which F-number is next and what the
last three findings established, then stop.

## Append only

**Never rewrite, reorder or delete another agent's finding.** This is the one file every role may
write to, and append-only is what makes that safe. If an earlier finding turns out to be wrong, the
correction is a **new** finding that says so and names the one it supersedes — F13's consequence was
withdrawn by F16 exactly this way, and both entries still stand. The record of having been wrong is
part of what the file is for.

## The next number

Findings are `F1`, `F2`, … in order of discovery. Check what exists before writing:

```bash
grep -nE '^#+ *F[0-9]+' findings.md | tail -3
```

Take the next integer. Do not reuse or renumber.

There is no lock on this, and it does not need one: `findings.md` is a file in a git working tree,
not a shared database. If you are in a worktree and another agent appends concurrently, you both
append at end-of-file and **git conflicts** — that is the detection. Resolve it by renumbering
*yours* to follow theirs; never drop a finding to make a merge clean.

## The shape

```markdown
## F<n> — <the claim, in one line, as a fact rather than a topic>

<What was established, and the evidence.>

**Verified:** <the command you ran or the file:line you read, verbatim>

**Not verified:** <the boundary — what this does not establish, and what it would take>

**Consequence:** <what changes because of this — a design decision, a blocked path, a rule>
```

The last two are slots, not decoration. The rule below is stated in prose all over this repository
and skipped anyway; a template with a blank in it is harder to skip than a paragraph asking nicely.

Four things separate a finding from a note:

- **The heading is a claim, not a subject.** "F1 — MLX `DistributedGroup` is static and never torn
  down" tells you the answer; "MLX group lifecycle" tells you nothing.
- **Evidence is a `file:line`, not a recollection.** Name the file, the line, and what it says. A
  finding whose evidence is "I recall reading" is an assumption wearing a finding's heading.
- **A negative result is a finding.** Both MLX spikes *failed*, and those two entries are the most
  valuable in the file. Stop at a false premise and record it.
- **State the consequence.** The heading is what happened; the consequence is why anyone cares. If
  you cannot write one, the discovery may not be a finding yet.

## The part that is skipped most often

Every finding must say **what was not verified.** This container is x86_64 Linux with no macOS, no
Xcode, no Android SDK and no Windows, so most claims about Apple behaviour here rest on reading
source rather than running it — and a finding that omits this reads as though it were tested.

Be specific about the gap, not decorative about it. "Not built as part of MLX, not linked, never run
on Apple hardware" is useful. "This may not be fully verified" is not.

The project has been bitten by this precisely: F18 records that `swiftc -parse` had been passing
Apple code for the whole project's life while three real type errors sat in it, because parsing is
syntax alone. One filtered log also produced one confident wrong diagnosis. Confidence that outruns
the evidence is the failure mode this section exists to prevent.

## Report

**State what you verified and what you did not** — in the finding itself, not only in your reply.
Then say which F-number you appended and quote its heading back, so the caller can see what entered
the record.
