#!/usr/bin/env python3

import signal
import sys

from swarm.config import (
    clear_selection,
    describe,
    effective_models,
    save_selection,
)
from swarm.llm import model_catalog
from swarm.orchestrator import load_state, run_swarm

USAGE = """Usage:
  ./run.py "your objective"
  ./run.py --iterations N "your objective"
  ./run.py --planner <id> "your objective"       pick the plan/review model
  ./run.py --executor <id> "your objective"      pick the worker model
  ./run.py --list-models                         catalog + current selection
  ./run.py --reset-models                        back to the shipped defaults

Runs plan -> autonomous execution -> review cycles
(--iterations bounds replan cycles, default 2).

Model choices persist in state/models.json; one-off overrides:
  SWARM_PLANNER_MODEL=<id> SWARM_EXECUTOR_MODEL=<id> ./run.py "objective"
"""


def list_models():
    selection = describe()

    print("MODEL SELECTION")
    for role in ("planner", "executor"):
        model, source = selection[role]
        print(f"  {role:<9} {model}   [{source}]")

    print("\nROUTER CATALOG  (curl $LLAMA_BASE_URL/models)")

    try:
        catalog = model_catalog()
    except Exception as error:
        print(f"  router unreachable: {error}")
        return

    chosen = {value for value, _ in selection.values()}

    for model in catalog:
        mark = "✓" if model["id"] in chosen else " "
        print(f"  {mark} {model['id']:<60s} {model['status']}")


def parse_flags(args):
    """Consume our flags; return (goal, max_iterations, configured)."""
    max_iterations = 2
    configured = False

    while args and args[0].startswith("--"):
        flag, args = args[0], args[1:]

        if flag == "--iterations":
            if not args:
                print("Missing value for --iterations")
                raise SystemExit(1)
            max_iterations = int(args[0])
            args = args[1:]

        elif flag in ("--planner", "--executor"):
            if not args:
                print(f"Missing value for {flag}")
                raise SystemExit(1)
            save_selection(**{flag[2:]: args[0]})
            configured = True
            print(f"Model selection saved (state/models.json):")
            print(f"  {flag[2:]:<9} {args[0]}")
            args = args[1:]

        elif flag == "--list-models":
            list_models()
            raise SystemExit(0)

        elif flag == "--reset-models":
            clear_selection()
            print("Model selection reset to the shipped defaults:")
            planner, executor = effective_models()
            print(f"  planner   {planner}")
            print(f"  executor  {executor}")
            raise SystemExit(0)

        else:
            print(f"Unknown option: {flag}\n")
            print(USAGE)
            raise SystemExit(1)

    return " ".join(args).strip(), max_iterations, configured


def main():
    # Die quietly on SIGPIPE like other CLI tools (| head, | less, …).
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    # Line-buffer the output: model loads and tool steps print progress,
    # and buffered it would all appear at once (or never, if a run dies).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    goal, max_iterations, configured = parse_flags(sys.argv[1:])

    if not goal:
        if configured:
            print('\nNow run:  ./run.py "your objective"')
            raise SystemExit(0)
        print(USAGE)
        raise SystemExit(1)

    print("=" * 60)
    print("Llamacascade")
    print("=" * 60)
    print(f"Goal: {goal}")
    print(f"Max iterations: {max_iterations}")

    state_before = load_state()
    # run_swarm restarts the count when the goal changes.
    iterations_before = (
        0 if state_before.get("goal") != goal else state_before["iteration"]
    )

    try:
        run_swarm(goal, max_iterations=max_iterations)
    except RuntimeError as error:
        # Model can't load / router refused: say it plainly, no traceback.
        print(f"\nERROR: {error}")
        raise SystemExit(1)

    state = load_state()

    print()
    print("=" * 60)
    print(
        f"Status: {state['status']}   "
        f"Iterations this run: {state['iteration'] - iterations_before}   "
        f"Total: {state['iteration']}"
    )

    if state["status"] != "completed":
        print("Objective not finished. Continue with:")
        print(f"  ./run.py --iterations N \"{goal}\"")


if __name__ == "__main__":
    main()
