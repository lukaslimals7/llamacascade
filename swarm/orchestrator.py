"""Swarm orchestrator (no pi): plan once, execute autonomously, review once.

Same loop as local-swarm/swarm/orchestrator.py, but the roles are plain
HTTP chat calls against the llama.cpp router and the worker's tools are
Python functions (swarm/tools.py) — no agent runtime involved.

Planner and executor share the router, so each role warms its own model
before it starts: a request that hits the router mid-swap gets a 500 and
turns into a silently empty turn otherwise.
"""

import json
from pathlib import Path

from .agent import NO_REPORT, LlamaAgent
from .config import (
    DEFAULT_EXECUTOR,
    DEFAULT_PLANNER,
    effective_models,
)
from .parsing import (
    _content_keys,
    _strip_scaffold,
    dedupe_evidence,
    extract_answer,
    extract_decision,
    parse_report,
    tasks_for,
)


ROOT = Path(__file__).resolve().parent.parent

# Model selection lives in swarm/config.py: shipped defaults, the user's
# persisted choice (state/models.json), and SWARM_PLANNER_MODEL /
# SWARM_EXECUTOR_MODEL env overrides. Pick models from the TUI menu
# ("Choose models") or with ./run.py --planner/--executor. These aliases
# keep the documented names working; run_swarm resolves the effective
# pair per run.
PLANNER_MODEL = DEFAULT_PLANNER   # Qwen3.8 2B Distill — plan + review
CPM_MODEL = DEFAULT_EXECUTOR      # MiniCPM5 2B — executor (CPM)

PLANNER_PROMPT = str(ROOT / "agents/planner.md")
CPM_PROMPT = str(ROOT / "agents/cpm.md")

STATE_FILE = ROOT / "state/swarm.json"
TASK_FILE = ROOT / "tasks/current.json"
LOG_DIR = ROOT / "state/logs"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "goal": None,
            "history": [],
            "iteration": 0,
            "status": "idle",
        }

    return json.loads(STATE_FILE.read_text())


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False)
    )


def save_plan(goal: str, tasks: list, iteration: int, status: str = "running"):
    """Record the plan the worker is executing in tasks/current.json."""
    TASK_FILE.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "id": f"plan-{iteration + 1:03d}",
        "parent": None,
        "owner": "cpm",
        "status": status,
        "objective": goal,
        "success_criteria": [],
        "tasks": tasks,
        "dependencies": [],
    }

    TASK_FILE.write_text(
        json.dumps(record, indent=2, ensure_ascii=False)
    )


def update_task_status(status: str):
    """Record how the current plan ended in tasks/current.json."""
    if not TASK_FILE.exists():
        return

    record = json.loads(TASK_FILE.read_text())
    record["status"] = status

    TASK_FILE.write_text(
        json.dumps(record, indent=2, ensure_ascii=False)
    )


def _answer_from_report(report, prose: str = "") -> str:
    """Deterministic answer when the review returns no text.

    Evidence is deduplicated and truncated: models re-run one command
    once per plan task, and raw console dumps are not an answer. Facts
    the prose already states are not repeated — the reviewer's answer is
    the answer, the report is only its safety net.
    """
    if not isinstance(report, dict):
        return ""

    prose_keys = _content_keys(prose)

    parts = []
    for finding in report.get("findings") or []:
        if finding and not _content_keys(finding) <= prose_keys:
            parts.append(f"- {finding}")

    for item in dedupe_evidence(report.get("evidence") or [], limit=3):
        if _content_keys(item) <= prose_keys:
            continue
        first = " / ".join(
            line.strip() for line in item.splitlines() if line.strip()
        )[:200]
        parts.append(f"  evidence: {first}")

    return "\n".join(parts)


def run_swarm(goal: str, max_iterations: int = 2):
    """Run the planner/executor/reviewer loop.

    One cycle = one plan + one autonomous execution + one review.
    max_iterations bounds replan cycles (a REPLAN review produces a new
    plan and starts the next cycle). Default 2: most goals need one cycle;
    replanning is the exception, not the loop.
    """

    state = load_state()

    # A new objective starts its own iteration count.
    if state.get("goal") != goal:
        state["iteration"] = 0

    state["goal"] = goal
    state["status"] = "running"

    save_state(state)

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    planner_model, cpm_model = effective_models()

    print(f"\n[models] planner: {planner_model}")
    print(f"[models] executor: {cpm_model}")

    planner = LlamaAgent(
        model=planner_model,
        system_prompt=Path(PLANNER_PROMPT).read_text(encoding="utf-8"),
        tools=False,
    )

    cpm = LlamaAgent(
        model=cpm_model,
        system_prompt=Path(CPM_PROMPT).read_text(encoding="utf-8"),
        tools=True,
    )

    # ---- 1. PLAN (planner, one call) -------------------------------
    # Continuation-style few-shot: tiny distills imitate filled examples
    # far better than they follow format instructions (an instruction-only
    # prompt makes Qwen3.8-2B emit template scaffolds, bash code, or
    # markdown tables instead of a plan). The prompt ends mid-list so the
    # model just keeps writing.
    plan_prompt = f"""
Plan example:

Objective: count files in the current directory
<PLAN>
1. Run the ls command to list files
2. Count the number of files listed
3. Report the count
</PLAN>

Plan example:

Objective: check who is logged in and write it to users.txt
<PLAN>
1. Run the who command to list logged-in users
2. Write the user list to users.txt
3. Read users.txt back and report its content
</PLAN>

Plan:

Objective: {goal}
<PLAN>
1. """

    plan_text = planner.prompt(plan_prompt)

    print(f"\n[PLAN — {planner.last_duration:.0f}s]")
    print(plan_text)

    tasks = tasks_for(goal, plan_text)

    if not tasks:
        plan_text = planner.prompt(f"""
Plan example:

Objective: find the largest file in a directory
<PLAN>
1. Run the du command to list file sizes
2. Pick the largest file from the listing
3. Report the file name and its size
</PLAN>

Plan:

Objective: {goal}
<PLAN>
1. """)

        print(f"\n[PLAN — {planner.last_duration:.0f}s]")
        print(plan_text)

        tasks = tasks_for(goal, plan_text)

    if not tasks:
        # Never strand the run over plan formatting: the executor gets
        # the objective either way, so hand it a minimal honest plan.
        print("\n[PLANNER MALFORMED — using fallback plan]")
        tasks = [
            f"Carry out the objective with your tools: {goal.strip()}",
            "Verify the result on disk and quote the output",
            "Report findings with evidence",
        ]

    state["history"].append({
        "agent": "planner",
        "type": "plan",
        "output": plan_text,
    })

    save_plan(goal, tasks, state["iteration"])
    save_state(state)

    answer = ""
    report_text = ""

    # ---- 2. EXECUTE + 3. REVIEW, per cycle -------------------------
    for cycle in range(max_iterations):
        plan_block = "\n".join(
            f"{i + 1}. {task}" for i, task in enumerate(tasks)
        )

        cpm_prompt = f"""
OBJECTIVE:

{goal}

PLAN:

{plan_block}

You own execution of ALL tasks. Work through them with your tools and
reason about the results as you go. Do not stop between tasks and do not
ask the planner anything.

Stop only when all tasks are complete, a task cannot be completed,
evidence contradicts the plan, or additional expertise is required.

When finished, reply with ONLY the JSON report described in your
instructions — no other text. Every finding needs matching evidence.
"""

        report_text = cpm.prompt(cpm_prompt)
        report = parse_report(report_text)
        trace = list(getattr(cpm, "tool_trace", []))

        if report is None and not trace:
            # A turn with no text and no tool activity is a dead turn
            # (the request never reached the model). Run it once more.
            print("\n[Worker produced no tool activity — re-running execution]")
            report_text = cpm.prompt(cpm_prompt)
            report = parse_report(report_text)
            trace = list(getattr(cpm, "tool_trace", []))

        if report is None and trace:
            # The worker often finishes its tool work without the JSON
            # report. Summarize WITH the trace: a blind retry would redo
            # all the work. Never do this with an empty trace — a
            # trace-less summarize turn just copies the example below
            # (plan.md B13) and invents "evidence" that never ran.
            print("\n[Worker report missing/not JSON — summarizing from trace]")
            trace_block = "\n".join(
                f"- {item}"
                for item in dedupe_evidence(
                    [f"{e['tool']}: {e['output'][:200]}" for e in trace]
                )
            ) or "(no tool activity recorded)"

            report_text = cpm.prompt(f"""
You already executed a task with your tools. Do NOT redo any work.

Your tool trace:

{trace_block}

Summarize what the tool output SHOWS as short facts — not the raw output
itself. Example:

Tool trace: "bash: lukas / 13:09:45 up 3:55, 1 user"
Report:
{{"status": "completed", "tasks_done": ["listed logged-in users"], "findings": ["user lukas is logged in", "the computer is up 3 hours 55 minutes"], "evidence": ["who -> lukas", "uptime -> up 3:55"], "blocked": [], "needs_replanning": false}}

Now produce the JSON report for the tool trace above, based only on the
work shown. The example above is a FORMAT SAMPLE — never copy its
content; every finding and every evidence line must come from the tool
trace above or be an empty list. Reply with ONLY the JSON object,
starting with the opening brace.
""")

            report = parse_report(report_text)
            # The summarize turn runs no tools, so its trace is empty —
            # never let it replace the execution trace the review needs as
            # ground truth. Only take the new trace when the old one is
            # empty (i.e. the re-run case above).
            trace = list(getattr(cpm, "tool_trace", [])) or trace

        if report is None:
            # Last resort: synthesize from the trace so the review
            # judges real evidence instead of nothing.
            report_text = report_text.strip() or NO_REPORT
            report = {
                "status": "completed" if trace else "blocked",
                "tasks_done": [],
                "findings": [
                    "(no textual report; work inferred from the tool trace)"
                    if trace
                    else "(no textual report and no tool activity — "
                    "the worker did no verifiable work)"
                ],
                "evidence": dedupe_evidence(
                    [f"{e['tool']}: {e['output'][:200]}" for e in trace]
                ),
                "blocked": [] if trace else ["no tool activity recorded"],
                "needs_replanning": not bool(trace),
            }

        # Models skimp on the evidence field; the trace is ground truth,
        # so backfill rather than let a hollow report through.
        if report is not None and trace and not report.get("evidence"):
            report["evidence"] = dedupe_evidence(
                [f"{e['tool']}: {e['output'][:200]}" for e in trace]
            )

        if report is not None and not report.get("findings") and trace:
            report["findings"] = ["(work performed; see evidence)"]

        # Ground-truth gate: a "completed" claim with zero tool activity
        # is hallucinated work (small models assert files were written
        # that never existed). Such a report is blocked, whatever it says.
        if (
            report is not None
            and not trace
            and report.get("status") == "completed"
        ):
            report["status"] = "blocked"
            report["needs_replanning"] = True
            report.setdefault("blocked", []).append(
                "(orchestrator) no tool activity recorded — nothing "
                "was verified on disk"
            )

        print(f"\n[CPM — {cpm.last_duration:.0f}s]")
        print(report_text)

        state["history"].append({
            "agent": "cpm",
            "type": "execution",
            "output": report_text,
        })

        save_state(state)

        # The review gets the parsed report plus the raw tool trace as
        # ground truth: it can check claims against real outputs.
        trace_block = "\n".join(
            f"- {item}"
            for item in dedupe_evidence(
                [f"{e['tool']}: {e['output'][:200]}" for e in trace]
            )
        ) or "(no tool activity recorded)"

        report_block = json.dumps(report, ensure_ascii=False)

        # ---- REVIEW (planner, one call) ----------------------------
        # Same few-shot continuation trick as the plan prompt: filled
        # examples make the distill emit the decision marker reliably;
        # instructions alone made it copy examples or skip the marker.
        review_prompt = f"""
You are a reviewer. Two example reviews:

Review example (work done):
<DECISION>DONE</DECISION>
<ANSWER>
the file a.txt now contains hello
</ANSWER>

Review example (work not done):
<DECISION>REPLAN</DECISION>
<PLAN>
1. Create the file a.txt
2. Write hello into it
</PLAN>

Now review this case.

Objective: {goal}

CPM's plan was:

{plan_block}

CPM executed the plan and reported:

{report_block}

CPM's tool trace (ground truth — check claims against it):

{trace_block}

Review against the user's objective only — not invented requirements.
A finding without evidence in the report or trace is unverified. Never
invent evidence, commands, or file contents.

Is the objective complete? Start your reply with <DECISION>DONE</DECISION>
followed by an <ANSWER> block, or <DECISION>REPLAN</DECISION> followed by
a <PLAN> block with what CPM should do next.
"""

        review = planner.prompt(review_prompt)

        print(f"\n[REVIEW — {planner.last_duration:.0f}s]")
        print(review)

        state["history"].append({
            "agent": "planner",
            "type": "review",
            "output": review,
        })

        decision = extract_decision(review)

        if decision is None:
            # No marker: decide from the worker's own structured
            # report instead of guessing at rambling text. A hollow
            # "completed" with no evidence is NOT done, and neither
            # is one backed by zero tool activity.
            done_by_report = (
                isinstance(report, dict)
                and bool(trace)
                and report.get("status") == "completed"
                and not report.get("needs_replanning")
                and bool(report.get("evidence"))
            )
            decision = "DONE" if done_by_report else "REPLAN"

        if decision == "DONE":
            # A silent review must not produce a silent answer, and an
            # answer without the facts is thin: always ground it in the
            # report's findings and evidence. Template filler in the
            # review ("[Complete, well-structured plan…]") is not prose.
            prose = _strip_scaffold(extract_answer(review))
            facts = _answer_from_report(report, prose)
            answer = f"{prose}\n\n{facts}".strip() if facts else prose
            answer = answer or (
                "(work completed; see state/swarm.json for the report)"
            )
            state["status"] = "completed"
            update_task_status("completed")
            save_state(state)
            break

        # ---- REPLAN ------------------------------------------------
        state["status"] = "needs_next_iteration"
        state["iteration"] += 1
        update_task_status("replanned")
        save_state(state)

        if cycle + 1 >= max_iterations:
            break

        tasks = tasks_for(goal, review)

        if not tasks:
            plan_text = planner.prompt(f"""
Plan example:

Objective: count files in the current directory
<PLAN>
1. Run the ls command to list files
2. Count the number of files listed
3. Report the count
</PLAN>

Plan:

Objective: {goal}

Latest findings: {report_text[-600:]}

The objective is not complete. Plan what to do next.
<PLAN>
1. """)

            print(f"\n[PLAN — {planner.last_duration:.0f}s]")
            print(plan_text)

            state["history"].append({
                "agent": "planner",
                "type": "plan",
                "output": plan_text,
            })

            tasks = tasks_for(goal, plan_text)

            if not tasks:
                # Stop honestly: the state records what remains.
                state["status"] = "needs_next_iteration"
                save_state(state)
                break

        save_plan(goal, tasks, state["iteration"])
        save_state(state)

    # ---- ANSWER ----------------------------------------------------
    if state["status"] == "completed" and answer:
        print(f"\n[ANSWER]")
        print(answer)

    return answer
