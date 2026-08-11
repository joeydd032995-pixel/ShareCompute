#!/usr/bin/env python3
"""Lint the agent roster in .claude/agents/.

These definitions are configuration, so nothing else catches a mistake in them: a bad
`name` or a missing `description` fails silently at dispatch time rather than loudly here.
This checks the things that would.

    python3 scripts/validate-agents.py

Exits non-zero on any failure.
"""

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTS = ROOT / ".claude" / "agents"

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

problems: list[str] = []
seen_names: dict[str, str] = {}
descriptions: list[tuple[str, str]] = []


def fail(path: pathlib.Path, message: str) -> None:
    problems.append(f"{path.relative_to(ROOT)}: {message}")


def parse_front_matter(path: pathlib.Path, text: str):
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


def main() -> int:
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

    gated = sum(1 for n in seen_names if n.startswith(GATED_PREFIXES))
    print(f"checked {len(files)} agent definitions ({gated} gated)")

    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
