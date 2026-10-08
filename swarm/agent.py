#!/usr/bin/env python3
"""Tool-using agent loop over raw llama.cpp chat completions (no pi).

The worker model gets one task, then loops: reply -> tool call -> tool
result -> reply…, until it stops calling tools. The planner/reviewer uses
the same entry point with no tools at all.
"""

import json
import time

from .llm import chat, ensure_model_loaded
from .parsing import parse_report
from .tools import TOOL_SPECS, parse_tool_call, run_tool

# Marker for turns where the agent worked through tools but wrote no
# report text. The orchestrator retries once for a real report.
NO_REPORT = "(no textual report)"

# Corrective turn for format drift: the model replied in a shape we
# could not parse (or only thought out loud). One nudge turns that into
# real work far more often than silently ending the loop.
NUDGE = (
    "That reply was neither a tool call nor the JSON report, so no work "
    "was performed. Reply with EXACTLY one tool call in this format:\n\n"
    "```tool\n{\"name\": \"bash\", \"args\": {\"command\": \"who\"}}\n```\n\n"
    "or, when all tasks are complete, ONLY the JSON report. Nothing else."
)

MAX_TOOL_STEPS = 12


def _looks_empty(result: str) -> bool:
    """True when a tool result reports nothing (no output, no matches).

    These are the results a small model turns into a false "none" —
    the caller attaches a cross-check reminder right here (plan.md B3).
    """
    head = (result or "").strip()[:40].lower()

    return head.startswith(("(no output)", "(no matches)", "(empty)"))


class LlamaAgent:
    def __init__(
        self,
        model: str,
        system_prompt: str,
        tools: bool = False,
        max_tokens: int = 2048,
    ):
        self.model = model
        self.tools = tools
        self.max_tokens = max_tokens
        self.last_duration = 0.0
        self.tool_calls = []
        self.tool_trace = []

        self._system = system_prompt

        if tools:
            self._system += "\n" + TOOL_SPECS

    def prompt(self, message: str) -> str:
        started = time.time()

        # A request that hits the router mid-swap gets a 500 and returns
        # nothing — warm the model first so the first call is real.
        ensure_model_loaded(self.model)

        self.tool_calls = []
        self.tool_trace = []

        messages = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": message},
        ]

        text = ""
        nudged = False

        for _ in range(MAX_TOOL_STEPS + 1):
            text = chat(self.model, messages, max_tokens=self.max_tokens)

            if not self.tools:
                break

            call = parse_tool_call(text)

            if call is None:
                if parse_report(text) is not None:
                    break  # the final JSON report — done working

                # Format drift (XML, flattened JSON, thinking out loud).
                # Say so once and let the model correct itself; a
                # silently broken loop is how work never happens.
                if nudged:
                    break

                nudged = True

                if text:
                    print("[tool?] reply had no parseable tool call", flush=True)

                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": NUDGE})
                continue

            name, args = call

            if name not in self.tool_calls:
                self.tool_calls.append(name)

            # A step can take a while (bash timeout 120s) and the next
            # model call can take longer — show what is happening so the
            # run never looks frozen between the plan and the report.
            print(f"[tool] {name} {json.dumps(args)[:120]}", flush=True)

            result, ok = run_tool(name, args)

            self.tool_trace.append({
                "tool": name,
                "output": result[:400],
                "error": not ok,
            })

            messages.append({"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": (
                    f"TOOL RESULT ({name}):\n{result}\n\n"
                    + (
                        "The command returned nothing. An empty result is "
                        "NOT a fact — cross-check with another source "
                        "(`w`, `who -u`, `loginctl list-sessions`) before "
                        "reporting \"none\".\n\n"
                        if _looks_empty(result)
                        else ""
                    )
                    + "Continue. When all tasks are complete, reply with "
                    "ONLY the JSON report — no tool call, no commentary."
                ),
            })

        text = text.strip()

        if not text and self.tool_calls:
            text = NO_REPORT + " Tools executed: " + ", ".join(self.tool_calls)

        self.last_duration = time.time() - started

        return text

    def close(self):
        # Stateless HTTP: nothing to tear down. Kept so the orchestrator
        # can treat both backends the same.
        pass
