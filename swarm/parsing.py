#!/usr/bin/env python3
"""Parsing helpers for the planner/worker protocols.

Small local models produce right content in wrong shapes: unclosed tags,
markdown tables, code-fenced "plans", bold checkboxes, bare "DECISION:
DONE" lines. Everything here tolerates those shapes. (Same hardening as
local-swarm/swarm/orchestrator.py — kept local so this project stands
alone.)
"""

import json
import re


def _clean_task_line(line: str) -> str:
    """One plan task from a raw line.

    Small distills wrap tasks in bullets, numbering, markdown bold,
    checkboxes or table cells — strip all of that noise.
    """
    item = line.strip()
    item = re.sub(r"^[-*+]\s+", "", item)          # bullet
    item = re.sub(r"^#{1,6}\s+", "", item)         # heading
    item = re.sub(r"^\[[ xX]\]\s*", "", item)      # checkbox
    item = re.sub(r"^\*+\s*", "", item)            # leading bold
    item = re.sub(r"^\d+[.)]\s*", "", item)        # numbering

    return item.strip().strip("`").strip().strip("*").strip()


def _plan_score(text: str) -> int:
    """How much a slice of text looks like a task list.

    Numbered/bulleted lines are the signal; prose paragraphs ("Brief
    explanation of each step.") are what models append after a plan.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    numbered = sum(
        1 for line in lines if re.match(r"^\s*(\d+[.)]|[-*+]|\[[ xX]\])\s+", line)
    )

    return numbered * 10 + min(len(lines), 5)


def extract_plan(text: str) -> list:
    """Ordered task lines from a <PLAN> block (tag may be unclosed).

    Tolerates the shapes small distills emit instead of a plain numbered
    list: a code-fenced block, a markdown table, bold/checkbox bullets.
    """
    match = re.search(
        r"<PLAN>\s*(.*?)(?:</PLAN>|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )

    block = match.group(1) if match else text

    # Template scaffolding some distills emit INSTEAD of a plan, often
    # wrapped in a code fence ("[Analyze the problem, consider edge
    # cases, choose your approach…]"). Checked before fence stripping —
    # otherwise the fence eats the evidence and a junk line parses like
    # a task. Not a plan at all: the caller retries.
    if re.search(
        r"\[\s*(analyze|understand|explain|describe|consider|choose|think)\b",
        block,
        re.I,
    ) or "EXACTLY as" in block:
        return []

    # A whole plan wrapped in one code fence is unwrapped; a language-tagged
    # fence (```bash, ```python, …) is code the distill emitted INSTEAD of
    # a plan, and is rejected so the caller retries.
    fenced = re.fullmatch(r"\s*```(\w*)\n(.*?)\n?```\s*", block, re.DOTALL)
    if fenced:
        if fenced.group(1).lower() in (
            "bash", "sh", "shell", "zsh", "python", "py", "json", "console",
        ):
            return []
        block = fenced.group(2)
    else:
        # A plan fence with chatter around it is common ("```\n1. …\n```\n
        # Brief explanation of each step."). Stripping fences would throw
        # the plan away and keep the chatter — use whichever slice looks
        # most like a task list.
        slices = [re.sub(r"```.*?```", "", block, flags=re.DOTALL)]
        slices += [
            m.group(2)
            for m in re.finditer(r"```(\w*)\n(.*?)\n?```", block, re.DOTALL)
            if m.group(1).lower()
            not in ("bash", "sh", "shell", "zsh", "python", "py", "json", "console")
        ]
        block = max(slices, key=_plan_score)

    # Markdown table plan: first column holds the task; rows before the
    # separator row are the header.
    rows = [l for l in block.splitlines() if l.strip().startswith("|")]

    if len(rows) >= 2:
        separator = next(
            (i for i, r in enumerate(rows) if re.fullmatch(r"[\s|:-]+", r.strip())),
            None,
        )
        body = rows[separator + 1:] if separator is not None else rows

        tasks = []
        for row in body:
            if re.fullmatch(r"[\s|:-]+", row.strip()):
                continue
            cells = row.strip().strip("|").split("|")
            # A leading index column is not the task — prefer the first
            # cell that carries text.
            item = ""
            for cell in cells:
                candidate = _clean_task_line(cell)
                if candidate and not re.fullmatch(r"\d+[.)]?", candidate):
                    item = candidate
                    break
            if item:
                tasks.append(item)

        return tasks[:8]

    lines = []
    for line in block.splitlines():
        item = _clean_task_line(line)
        if item:
            lines.append(item)

    # Template scaffolding some distills emit instead of a plan (e.g.
    # "[Understand what is being asked…]"). If the block smells like it,
    # it is not a plan at all — the caller retries for a real one.
    if any(l.startswith("[") or "EXACTLY as" in l for l in lines):
        return []

    # Echo noise: few-shot continuation prompts make tiny models repeat
    # the prompt's labels ("Objective: …", "Plan:", "Example …") as if
    # they were tasks. Those lines are not tasks.
    tasks = [
        l for l in lines
        if not l.startswith(("<", "["))
        and not re.match(
            r"^(objective|plan|example|now your turn)\b\s*[:\-]?", l, re.I
        )
    ]

    return tasks[:8]


def extract_decision(text: str):
    """Return "DONE", "REPLAN", or None when no marker was produced.

    Tolerates unclosed tags, markdown bold around the marker, and a bare
    "DECISION: DONE" line — small distills rarely close tags cleanly.
    """
    match = re.search(
        r"<DECISION>\s*\**\s*(DONE|REPLAN)\b",
        text,
        re.DOTALL | re.IGNORECASE,
    )

    if match:
        return match.group(1).upper()

    match = re.search(
        r"\bDECISION\b\**\s*[:\-]?\s*\**\s*(DONE|REPLAN)\b",
        text,
        re.IGNORECASE,
    )

    return match.group(1).upper() if match else None


def extract_answer(text: str) -> str:
    match = re.search(
        r"<ANSWER>\s*(.*?)(?:</ANSWER>|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )

    return (match.group(1) if match else text).strip()


def _strip_scaffold(text: str) -> str:
    """Remove template scaffolding from prose.

    Small distills answer with the template itself —
    "[Complete, well-structured plan with all steps accounted for]" plus
    "Brief justification confirming …". That is filler, not an answer,
    and it must never reach the user as the final answer.
    """
    out = text or ""

    out = re.sub(
        r"```[^`]*\[[^\]]*(analyze|complete|explain|describe|justify|verify|consider|choose)"
        r"[^\]]*\][^`]*```",
        "",
        out,
        flags=re.I | re.DOTALL,
    )
    out = re.sub(
        r"^\s*\[[^\]]*(analyze|complete|explain|describe|justify|verify|consider|choose)"
        r"[^\]]*\]\s*$",
        "",
        out,
        flags=re.I | re.DOTALL | re.M,
    )
    out = re.sub(
        r"^\s*(brief|short)\s+(explanation|justification|confirmation|summary|overview)\b.*$",
        "",
        out,
        flags=re.I | re.M,
    )

    return out.strip()


_STOP = {
    "with", "that", "this", "from", "your", "have", "been", "will",
    "what", "when", "where", "which", "should", "would", "could",
    "into", "about", "there", "their", "them", "then", "than", "also",
    "just", "only", "more", "most", "make", "made", "need", "needs",
    "using", "used", "does", "the", "and", "for", "you", "are",
}


def _content_keys(text: str) -> set:
    """Content-word keys (first 4 chars) so 'file' matches 'files'."""
    return {
        w[:4]
        for w in re.findall(r"[a-z0-9_.-]{4,}", (text or "").lower())
        if w not in _STOP
    }


def plan_matches_goal(tasks: list, goal: str) -> bool:
    """False when a 'plan' is generic filler unrelated to the objective.

    Small distills sometimes answer planning prompts with research
    boilerplate ("Research the current state of the problem and identify
    constraints…"). That parses like a plan but gives the executor
    nothing to do. A real plan shares at least one content word with the
    objective.
    """
    goal_keys = _content_keys(goal)

    if not goal_keys or not tasks:
        return True

    return any(_content_keys(task) & goal_keys for task in tasks)


def tasks_for(goal: str, text: str) -> list:
    """Plan tasks that parse AND relate to the objective.

    Generic research-boilerplate plans parse fine but give the executor
    nothing real to do — treat them as no plan so the caller retries.
    """
    tasks = extract_plan(text)

    return tasks if plan_matches_goal(tasks, goal) else []


def _evidence_key(text: str) -> str:
    """Normalized evidence identity: digits collapsed, so repeated runs
    of one command ('up 3:55' vs 'up 3:56') count as a single fact."""
    return re.sub(r"\d+", "#", " ".join((text or "").split()).lower())


def dedupe_evidence(items: list, limit: int = 6) -> list:
    """Drop repeated evidence (models re-run one command per task)."""
    seen = set()
    kept = []

    for item in items:
        if not item:
            continue
        key = _evidence_key(item)
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)

    return kept[:limit]


def _repair_json(fragment: str) -> str:
    """Close open strings/brackets so truncated JSON can still parse."""
    stack = []
    in_string = False
    escape = False

    for ch in fragment:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()

    if in_string:
        fragment += '"'

    fragment = fragment.rstrip().rstrip(",:")

    return fragment + "".join(reversed(stack))


def parse_report(text: str):
    """Worker JSON report, tolerantly. None when nothing parses.

    Handles: prose around the JSON, bad control characters, and reports
    truncated mid-string by the completion cap (models ramble, then the
    JSON gets cut off).
    """
    start = text.find("{")

    if start == -1:
        return None

    end = text.rfind("}")
    fragment = text[start:end + 1] if end > start else text[start:]

    for candidate in (fragment, re.sub(r"[\x00-\x1f]", "", fragment)):
        for attempt in (candidate, _repair_json(candidate)):
            try:
                report = json.loads(attempt)
                if isinstance(report, dict):
                    return report
            except Exception:
                continue

    # Hopeless text: salvage scalar fields so decisions still have a basis.
    scalars = {}
    match = re.search(r'"status"\s*:\s*"([^"]*)"', text)
    if match:
        scalars["status"] = match.group(1)
    match = re.search(r'"needs_replanning"\s*:\s*(true|false)', text, re.I)
    if match:
        scalars["needs_replanning"] = match.group(1).lower() == "true"

    return scalars or None
