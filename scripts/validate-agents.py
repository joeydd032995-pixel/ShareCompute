#!/usr/bin/env python3
"""Lint the agent roster in .claude/agents/ and the skills in .claude/skills/.

These definitions are configuration, so nothing else catches a mistake in them: a bad
`name` or a missing `description` fails silently at dispatch time rather than loudly here.
This checks the things that would.

    python3 scripts/validate-agents.py

The skill half matters more than it looks. A forked skill resolves its agent like this:

    agentType === "general-purpose") ?? m[0]

— so a misspelled `agent:` does not error. It silently lands on `general-purpose`, which
carries the full toolset including Write. The `agent:` resolution check below is what
catches that, and it is the highest-value check here.

What it is *not* is a permission boundary. The gated roles' gate is instructional — their
definitions refuse the work — and those definitions are not read-only either: all nine
carry Bash. See `findings.md` F19; an earlier version of this docstring said the toolset
was the gate, which was wrong.

Exits non-zero on any failure.
"""

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTS = ROOT / ".claude" / "agents"
SKILLS = ROOT / ".claude" / "skills"

# Tools the harness actually provides. A typo here means the agent silently loses a
# capability it thinks it has.
KNOWN_TOOLS = {
    "Read", "Write", "Edit", "Bash", "Grep", "Glob",
    "WebFetch", "WebSearch", "Skill", "Agent", "Artifact",
    "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput", "TaskStop",
    "NotebookEdit", "AskUserQuestion", "SendUserFile", "ToolSearch", "Monitor",
    "SendMessage", "ReportFindings",
}

KNOWN_MODELS = {"opus", "sonnet", "haiku", "fable", "inherit"}

# Roles with no runtime to target until the spec's Phase 1a lands. See CLAUDE.md.
GATED_PREFIXES = ("linux-", "windows-", "android-")

# Every definition must carry this rule; it is the one that keeps unverified work from
# being reported as done.
HONESTY_RULE = "State what you verified and what you did not"

# Skills that are not a role command. Anything else in .claude/skills/ must match an agent
# name — an unclassified skill is a failure, not a skip, or it evades every check below.
ORCHESTRATION_SKILL = "orchestration"
WORKFLOW_SKILLS = {"verify", "patches", "findings"}
NON_ROLE_SKILLS = WORKFLOW_SKILLS | {ORCHESTRATION_SKILL}

# Front-matter keys the harness recognises. Unknown keys are silently tolerated at runtime,
# so `disable_model_invocation` (underscores) would leave a skill fully model-invocable with
# no warning at all. That is the failure this set exists to catch.
KNOWN_SKILL_KEYS = {
    "name", "description", "model", "allowed-tools", "disallowed-tools",
    "argument-hint", "arguments", "disable-model-invocation", "user-invocable",
    "effort", "shell", "version",
    "when_to_use", "paths", "hooks", "context", "agent", "background", "metadata",
}

# A role skill must not touch the agent's tool or model contract: these apply to the forked
# run and would override it. For the gated roles that contract *is* the gate.
FORBIDDEN_ROLE_SKILL_KEYS = ("allowed-tools", "disallowed-tools", "model")

# A role skill that starts carrying role knowledge grows. Length is the check that actually
# enforces "the agent file is the single source of truth"; referencing it is not enough.
ROLE_SKILL_MAX_BODY_LINES = 40

problems: list[str] = []
seen_names: dict[str, str] = {}
descriptions: list[tuple[str, str]] = []
skill_descriptions: list[tuple[str, str]] = []


def fail(path: pathlib.Path, message: str) -> None:
    """Record one problem against a file. Collected rather than raised, so a single run
    reports every fault instead of stopping at the first."""
    problems.append(f"{path.relative_to(ROOT)}: {message}")


def parse_front_matter(path: pathlib.Path, text: str):
    """Parse the leading `---` block, or record why it could not be parsed and return None.

    Strict on purpose: the harness logs `YAML frontmatter failed to parse and was ignored`
    and drops the *whole* block, so an unquoted `: ` in a description would silently take
    `agent:`, `context:` and `disable-model-invocation` with it.
    """
    if not text.startswith("---\n"):
        fail(path, "missing YAML front matter (file must start with '---')")
        return None
    end = text.find("\n---\n", 3)
    if end == -1:
        fail(path, "front matter is not terminated by a closing '---'")
        return None
    try:
        meta = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        fail(path, f"front matter is not valid YAML: {exc}")
        return None
    if not isinstance(meta, dict):
        fail(path, "front matter did not parse to a mapping")
        return None
    return meta


def check(path: pathlib.Path) -> None:
    """Lint one `.claude/agents/<name>.md` role definition.

    Identity keys off the filename, since that is what `subagent_type` resolves against —
    a `name:` that disagrees with it dispatches to nothing.
    """
    text = path.read_text(encoding="utf-8")
    meta = parse_front_matter(path, text)
    if meta is None:
        return

    name = meta.get("name")
    if not name:
        fail(path, "missing 'name'")
    else:
        if name != path.stem:
            fail(path, f"name '{name}' does not match filename '{path.stem}'")
        if name in seen_names:
            fail(path, f"duplicate name '{name}', also in {seen_names[name]}")
        seen_names[name] = path.name

    description = meta.get("description")
    if not description:
        fail(path, "missing 'description' — dispatch routes on this")
    elif len(description) < 60:
        fail(path, "description too short to route on reliably (<60 chars)")
    else:
        descriptions.append((path.stem, description))

    tools = meta.get("tools")
    if tools is None:
        fail(path, "missing 'tools'")
    else:
        listed = [t.strip() for t in tools.split(",")] if isinstance(tools, str) else list(tools)
        for tool in listed:
            if tool not in KNOWN_TOOLS:
                fail(path, f"unknown tool '{tool}'")

    model = meta.get("model")
    if model is None:
        fail(path, "missing 'model'")
    elif str(model) not in KNOWN_MODELS:
        fail(path, f"unknown model '{model}' (expected one of {sorted(KNOWN_MODELS)})")

    body = text[text.find("\n---\n", 3) + 5:]

    if HONESTY_RULE.lower() not in body.lower():
        fail(path, f"body must carry the verification rule: '{HONESTY_RULE}'")

    if "CLAUDE.md" not in body:
        fail(path, "body should point the agent at CLAUDE.md")

    if name and name.startswith(GATED_PREFIXES):
        if "GATED" not in (description or ""):
            fail(path, "gated role must say GATED in its description")
        if "blocked" not in body.lower():
            fail(path, "gated role must state its gate in the body")


def check_skill(path: pathlib.Path, agent_names: set[str]) -> None:
    """Lint one .claude/skills/<slug>/SKILL.md.

    The directory name is authoritative — `name:` in the front matter is cosmetic, it only
    sets the display name — so every identity check below keys off the parent directory.
    """
    slug = path.parent.name
    text = path.read_text(encoding="utf-8")
    meta = parse_front_matter(path, text)
    if meta is None:
        return

    for key in meta:
        if key not in KNOWN_SKILL_KEYS:
            fail(path, f"unknown front-matter key '{key}' — the harness ignores these "
                       "silently, so a typo turns the setting off with no warning")

    name = meta.get("name")
    if name and name != slug:
        fail(path, f"name '{name}' does not match directory '{slug}'")

    description = meta.get("description")
    if not description:
        fail(path, "missing 'description' — it is the text in the slash menu")
    elif len(description) < 60:
        fail(path, "description too short to tell apart in the menu (<60 chars)")
    else:
        skill_descriptions.append((slug, description))

    body = text[text.find("\n---\n", 3) + 5:]
    is_role = slug in agent_names

    if not is_role and slug not in NON_ROLE_SKILLS:
        fail(path, f"'{slug}' matches no agent and is not one of "
                   f"{sorted(NON_ROLE_SKILLS)} — classify it or it escapes every check")
        return

    if slug in WORKFLOW_SKILLS:
        # These run inline with no agent definition behind them, so nothing else carries
        # the rule for them. Role skills must NOT carry it — that is the restatement the
        # whole design forbids.
        if HONESTY_RULE.lower() not in body.lower():
            fail(path, f"workflow skill must carry the verification rule: '{HONESTY_RULE}'")

        # The inverse of the role-skill rule, and it needs enforcing for the same reason.
        # These three dispatch nobody, so they are not a routing decision and do not compete
        # with orchestration. `verify` in particular *shadows* a model-invocable built-in:
        # disabling it here would subtract a capability rather than replace one.
        if meta.get("disable-model-invocation") is True:
            fail(path, "workflow skills stay model-invocable — they dispatch nobody, so they "
                       "are not a routing decision, and a disabled 'verify' would strictly "
                       "subtract from the built-in skill it shadows")

    if not is_role:
        return

    # --- role skills only -------------------------------------------------------------
    if meta.get("disable-model-invocation") is not True:
        fail(path, "role skills need 'disable-model-invocation: true' so they stay a human "
                   "command surface and leave routing to the orchestration skill")

    for key in FORBIDDEN_ROLE_SKILL_KEYS:
        if key in meta:
            fail(path, f"'{key}' overrides the agent's own contract on the forked run — "
                       f".claude/agents/{slug}.md owns that, not the skill")

    if f".claude/agents/{slug}.md" not in body:
        fail(path, f"body must point at .claude/agents/{slug}.md as the definition")

    lines = [line for line in body.strip().splitlines()]
    if len(lines) > ROLE_SKILL_MAX_BODY_LINES:
        fail(path, f"body is {len(lines)} lines (max {ROLE_SKILL_MAX_BODY_LINES}) — a role "
                   "skill this long is restating the agent definition")

    if description and slug not in description:
        fail(path, f"description must name the role '{slug}' so the menu is unambiguous")

    gated = slug.startswith(GATED_PREFIXES)
    if gated:
        # Inline, deliberately: a wrong `agent:` resolves to general-purpose with no error,
        # and inline is the option that fails visibly. Not a tool restriction — see F19.
        if "context" in meta or "agent" in meta:
            fail(path, "gated role skills must not fork: a mistyped 'agent' falls back to "
                       "general-purpose silently, and inline dispatch is the variant that "
                       "fails where someone can see it")
        if "GATED" not in (description or ""):
            fail(path, "gated role skill must say GATED in its description")
        if "blocked" not in body.lower():
            fail(path, "gated role skill must state its gate in the body")
    else:
        if meta.get("context") != "fork":
            fail(path, "active role skill needs 'context: fork' to spawn its agent")
        agent = meta.get("agent")
        if agent != slug:
            fail(path, f"agent '{agent}' does not match directory '{slug}' — a wrong value "
                       "resolves to general-purpose silently, with no error anywhere")
        elif not (AGENTS / f"{slug}.md").is_file():
            fail(path, f"agent '{agent}' has no definition at .claude/agents/{slug}.md")
        if meta.get("background") is not False:
            fail(path, "needs 'background: false' — the default is true, which detaches the "
                       "run and reports back as a notification instead of blocking")


def main() -> int:
    """Lint every agent and skill, then report. Returns 0 when clean, 1 on any problem."""
    if not AGENTS.is_dir():
        print(f"no agent directory at {AGENTS}", file=sys.stderr)
        return 1

    files = sorted(AGENTS.glob("*.md"))
    if not files:
        print("no agent definitions found", file=sys.stderr)
        return 1

    for path in files:
        check(path)

    # Two roles with near-identical descriptions will route unpredictably.
    #
    # Deliberately NOT applied to skills: dispatch routes on an agent's description, but a
    # role skill is model-invisible, so its description is only menu text for a human. The
    # rationale does not transfer, and the near-identical wording across platform trios
    # would fail this on sight.
    for i, (name_a, desc_a) in enumerate(descriptions):
        for name_b, desc_b in descriptions[i + 1:]:
            words_a = set(re.findall(r"\w+", desc_a.lower()))
            words_b = set(re.findall(r"\w+", desc_b.lower()))
            if not words_a or not words_b:
                continue
            overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
            if overlap > 0.85:
                problems.append(
                    f"{name_a} and {name_b}: descriptions are {overlap:.0%} similar — "
                    "dispatch cannot reliably tell them apart"
                )

    agent_names = set(seen_names)
    skill_files, role_skills = [], set()

    if SKILLS.is_dir():
        skill_files = sorted(SKILLS.glob("*/SKILL.md"))
        for path in skill_files:
            check_skill(path, agent_names)
            if path.parent.name in agent_names:
                role_skills.add(path.parent.name)

        # Every role gets a command and every command names a real role. This is what keeps
        # the two directories from drifting apart as the roster changes.
        for missing in sorted(agent_names - role_skills):
            problems.append(f"{missing}: agent has no command at .claude/skills/{missing}/SKILL.md")

        seen_slugs: dict[str, str] = {}
        for slug, desc in skill_descriptions:
            if desc in seen_slugs:
                problems.append(
                    f"{seen_slugs[desc]} and {slug}: identical descriptions — "
                    "the slash menu cannot tell them apart"
                )
            seen_slugs[desc] = slug
    else:
        problems.append(f"no skills directory at {SKILLS.relative_to(ROOT)}")

    gated = sum(1 for n in seen_names if n.startswith(GATED_PREFIXES))
    workflow = sum(1 for p in skill_files if p.parent.name in WORKFLOW_SKILLS)
    router = sum(1 for p in skill_files if p.parent.name == ORCHESTRATION_SKILL)
    print(
        f"checked {len(files)} agent definitions ({gated} gated), "
        f"{len(skill_files)} skills ({len(role_skills)} role, {workflow} workflow, "
        f"{router} router)"
    )

    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
