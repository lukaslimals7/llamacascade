#!/usr/bin/env python3
"""Worker tools, executed in-process (no pi, no external agent runtime).

Each tool returns a plain string result; every result is truncated so one
huge command cannot blow the router's 8K context.
"""

import re
import subprocess
from pathlib import Path

from .parsing import _repair_json

MAX_OUTPUT = 2000


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text

    return text[:limit] + f"\n… [truncated, {len(text) - limit} chars cut]"


def bash(command: str, timeout: int = 120) -> str:
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"

    out = completed.stdout or ""
    err = completed.stderr or ""

    parts = []
    if out:
        parts.append(out)
    if err:
        parts.append(f"[stderr]\n{err}")
    if not parts:
        # Make emptiness visible. "[exit code: 0]" alone reads like
        # success, and a small model then reports "no users" as a fact
        # (plan.md B3) instead of switching to another source.
        parts.append("(no output)")
    parts.append(f"[exit code: {completed.returncode}]")

    return _clip("\n".join(parts).strip())


def read(path: str) -> str:
    target = Path(path).expanduser()

    if not target.is_file():
        return f"ERROR: no such file: {path}"

    return _clip(target.read_text(encoding="utf-8", errors="replace"))


def _unwrap_cdata(text: str) -> str:
    """Strip an XML/CDATA wrapper off model-supplied text.

    Small models sometimes wrap argument values in <![CDATA[…]]>. It is
    never part of the value: a wrapped shell command would literally
    fail, a wrapped file content would end up on disk.
    """
    stripped = (text or "").strip()

    if stripped.startswith("<![CDATA[") and stripped.endswith("]]>"):
        return stripped[len("<![CDATA["):].rstrip()[:-3]

    return text or ""


def write(path: str, content: str) -> str:
    target = Path(path).expanduser()

    # Small models sometimes wrap file content in XML/CDATA noise. That
    # wrapper is never what the user wants in the file.
    text = _unwrap_cdata(content).strip("\n")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")

    if not text.strip():
        return (
            f"WROTE 0 chars to {target} — WARNING: an empty file is a "
            "failed action. Write the real (cross-checked) content, not "
            "the output of a command that produced nothing."
        )

    return f"WROTE {len(text)} chars to {target}"


def edit(path: str, old: str, new: str) -> str:
    target = Path(path).expanduser()

    if not target.is_file():
        return f"ERROR: no such file: {path}"

    text = target.read_text(encoding="utf-8", errors="replace")

    if old not in text:
        return "ERROR: old text not found in file — nothing changed"

    target.write_text(text.replace(old, new, 1), encoding="utf-8")

    return f"EDITED {target} (1 replacement)"


def grep(pattern: str, path: str = ".") -> str:
    try:
        completed = subprocess.run(
            ["grep", "-rn", "--include=*", "-e", pattern, path],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as error:
        return f"ERROR: {error}"

    return _clip(completed.stdout.strip() or "(no matches)")


def find(pattern: str = "*", path: str = ".") -> str:
    matches = sorted(Path(path).expanduser().glob(pattern))[:50]

    return "\n".join(str(m) for m in matches) or "(no matches)"


def ls(path: str = ".") -> str:
    target = Path(path).expanduser()

    if not target.exists():
        return f"ERROR: no such path: {path}"

    entries = sorted(target.iterdir())[:50]

    return "\n".join(
        f"{'d' if e.is_dir() else '-'} {e.name}" for e in entries
    ) or "(empty)"


# The worker's toolbelt. Names must match the protocol block in agent.py.
TOOLS = {
    "bash": bash,
    "read": read,
    "write": write,
    "edit": edit,
    "grep": grep,
    "find": find,
    "ls": ls,
}

TOOL_SPECS = """
TOOLS (call one per turn):
- bash  {"command": "<shell>"}                  run a shell command
- read  {"path": "<file>"}                      read a file
- write {"path": "<file>", "content": "<text>"} write a file (creates it)
- edit  {"path": "<file>", "old": "<t>", "new": "<t>"}  replace text once
- grep  {"pattern": "<regex>", "path": "<dir>"} search file contents
- find  {"pattern": "<glob>", "path": "<dir>"}  list matching files
- ls    {"path": "<dir>"}                       list a directory

Tool call format — use EXACTLY this shape, one call per turn, in a
```tool fence, args nested under "args":

```tool
{"name": "bash", "args": {"command": "who"}}
```

```tool
{"name": "write", "args": {"path": "users.txt", "content": "lukas tty2"}}
```

Any other shape (XML tags, plain JSON) is not a tool call and your work
will not run. One call per turn, nothing else in the reply.
"""

CALL_PATTERN = re.compile(
    r"```tool\s*\n(\{.*?\})\s*\n?```",
    re.DOTALL,
)

# Small models never pick one tool syntax: the same call appears as
# fenced JSON, bare JSON, name-as-key JSON, flattened args, or XML
# (<function name=…><param name=…>…</param></function>). All of these
# were seen from a MiniCPM-family 2B in a single objective — dropping
# the odd ones means dropping real work (see plan.md B12).
FN_TAG_PATTERN = re.compile(
    r"<function\b([^>]*)>(.*?)</function>",
    re.DOTALL | re.IGNORECASE,
)
PARAM_PATTERN = re.compile(
    r"<param\b[^>]*name\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</param>",
    re.DOTALL | re.IGNORECASE,
)
ATTR_PATTERN = re.compile(r"(\w+)\s*=\s*[\"']([^\"']*)[\"']")


def _balanced_json(text: str, start: int):
    """The {...} object starting at `start`, nested braces respected."""
    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]

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
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return text[start:]


def _json_candidates(raw: str):
    """Parse candidates for a model-emitted call object.

    As-is, comma-trimmed, repaired (close open strings/brackets), then
    progressively truncated back to the last complete key/value pair —
    models get cut off mid-key (max_tokens) and that call is still real
    work: `{"name": "grep", "args": {"pattern": "foo", "path": ` is a
    grep for "foo".
    """
    trimmed = raw.rstrip().rstrip(",")
    yield trimmed
    yield _repair_json(trimmed)

    cut = trimmed

    for _ in range(6):
        index = max(cut.rfind(","), cut.rfind("{"))
        if index == -1:
            return
        cut = cut[:index] if cut[index] == "," else cut[:index + 1]
        yield _repair_json(cut)


def _json_call(raw: str):
    """(name, args) from one JSON blob, in any shape models emit.

    Seen from a single model on a single objective: canonical
    {"name":…, "args":{…}}, OpenAI-ish {"name":…, "arguments":{…}},
    flattened {"name":"write","path":…,"content":…}, and name-as-key
    {"edit":{…}}. Picking one shape loses real work.
    """
    for attempt in _json_candidates(raw):
        try:
            import json

            call = json.loads(attempt)
        except Exception:
            continue

        if not isinstance(call, dict):
            continue

        name = call.get("name")

        if isinstance(name, str) and name in TOOLS:
            args = call.get("args")
            if not isinstance(args, dict):
                args = call.get("arguments")

            if isinstance(args, dict) and args:
                return name, args

            # Flattened: everything except the name/args keys is an arg.
            flat = {
                k: v for k, v in call.items()
                if k not in ("name", "args", "arguments", "tool", "function")
            }
            return name, flat

        # Name-as-key: {"edit": {"path": …, "old": …, "new": …}}
        for key, value in call.items():
            if key in TOOLS and isinstance(value, dict):
                return key, value

    return None


def _xml_call(text: str):
    """(name, args) from a <function …><param …>…</param></function> call.

    MiniCPM-family models answer in this XML syntax even when the prompt
    asks for fenced JSON — the call is real work, so parse it.
    """
    for match in FN_TAG_PATTERN.finditer(text):
        attrs, body = match.group(1), match.group(2)

        name = None
        for key, value in ATTR_PATTERN.findall(attrs):
            if key.lower() in ("name", "tool", "function"):
                name = value.strip()
                break

        args = {}
        for key, value in PARAM_PATTERN.findall(body):
            args[key.strip()] = value.strip()

        if not args:
            # <function name="bash">{"command": "who"}</function>
            body = body.strip()
            if body.startswith("{"):
                call = _json_call(_balanced_json(body, 0))
                if call is not None:
                    name, args = call

        if name in TOOLS:
            return name, args

    return None


def parse_tool_call(text: str):
    """One tool call from the model's reply.

    Preferred shape is a ```tool fenced JSON block, but the parser is
    deliberately shape-blind: XML <function> calls, bare JSON, flattened
    or name-as-key JSON are all accepted (small models mix them freely —
    see plan.md B12). Returns (name, args) or None. Tolerates prose
    around the block and truncated JSON on the last key.
    """
    text = text or ""

    call = _xml_call(text)
    if call is not None:
        return call

    match = CALL_PATTERN.search(text)

    if match:
        call = _json_call(match.group(1))
        if call is not None:
            return call

    # Bare JSON in prose or in a ```json fence: try every balanced object
    # until one names a known tool. A report object {"status": …} names
    # none, so it can never be mistaken for a tool call.
    for match in re.finditer(r"\{", text):
        call = _json_call(_balanced_json(text, match.start()))
        if call is not None:
            return call

    return None


def run_tool(name: str, args: dict) -> tuple:
    """Execute one tool. Returns (result_text, ok)."""
    tool = TOOLS.get(name)

    if tool is None:
        return f"ERROR: unknown tool {name}", False

    # Models wrap argument values in CDATA/markup noise; strip it before
    # anything reaches a shell or a file.
    args = {
        key: _unwrap_cdata(value) if isinstance(value, str) else value
        for key, value in (args or {}).items()
    }

    try:
        result = tool(**args)
    except TypeError as error:
        return f"ERROR: bad arguments for {name}: {error}", False
    except Exception as error:
        return f"ERROR: {name} failed: {error}", False

    return result, True
