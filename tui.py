#!/usr/bin/env python3
"""Interactive TUI for the local AI swarm — rich (pure Python).

run.py stays the scriptable one-shot CLI; this is the front end to sit
in: type the objective, watch plan -> execute -> review stream live,
read the rendered answer, browse history, pick the models.

Why rich and not gum: a bubbletea-style text widget re-owns the
terminal, and users got an input box that showed no typed text
(plan.md B18). This TUI types in line mode — the terminal itself echoes
every keystroke — and its arrow-key selector falls back to a numbered
prompt, so input can never silently disappear.

Usage:
  ./tui.py                     interactive (input dialog + menu)
  ./tui.py "objective"         run one objective, show the result, exit
  ./tui.py --iterations 3 "…"  run with N replan cycles

Needs the `rich` package (pip install rich); run.py needs nothing.
"""

import json
import os
import select
import signal
import sys
import time
import urllib.request

try:
    from rich import box
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("This TUI needs the rich package:")
    print("    pip install rich")
    print()
    print("run.py works without it.")
    raise SystemExit(1)

from swarm.config import (
    clear_selection,
    describe,
    effective_models,
    save_selection,
)
from swarm.llm import ROUTER_URL, model_catalog
from swarm.orchestrator import load_state, run_swarm

console = Console()

# No stray ":" after the "│ ▸" prompt (rich appends one by default).
Prompt.suffix = " "

ACCENT = "#ff5fd7"   # pink, same spirit as the old 212
OK = "#00d75f"
WARN = "#ffaf00"
ERR = "#ff005f"
DIM = "#6c6c6c"

MENU = [
    "New objective",
    "Continue the last objective",
    "Choose models",
    "Show the last answer",
    "Browse the run history",
    "Router status",
    "Quit",
]


# ------------------------------------------------------------------ views

def heading(text: str):
    console.print()
    console.rule(f"[bold {ACCENT}]{text}[/]")


def note(text: str):
    console.print(Text(f"  {text}", style=DIM))


def panel(title: str, body, color: str = ACCENT):
    console.print(Panel(
        body, title=f"[bold]{title}[/]", title_align="left",
        border_style=color, box=box.ROUNDED, padding=(1, 2),
    ))


def status_color(status: str) -> str:
    return {
        "completed": OK,
        "needs_next_iteration": WARN,
        "running": ACCENT,
    }.get(status, DIM)


def banner(state: dict):
    goal = state.get("goal") or "(none yet — type one below)"
    status = state.get("status", "idle")

    selection = describe()
    planner, planner_from = selection["planner"]
    executor, executor_from = selection["executor"]

    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style=DIM)
    grid.add_column()

    grid.add_row("objective", Text(str(goal)))
    grid.add_row("status", Text(status, style=status_color(status)))
    grid.add_row("", Text(f"cycles {state.get('iteration', 0)}", style=DIM))
    grid.add_row("planner", Text(f"{planner}   [{planner_from}]"))
    grid.add_row("executor", Text(f"{executor}   [{executor_from}]"))

    console.print(Panel(
        grid,
        title="[bold]Llamacascade[/] · plan → execute → review",
        border_style=ACCENT, box=box.ROUNDED, padding=(1, 2),
    ))


def ask_objective() -> str:
    """The input dialog box. Line-mode input: the terminal echoes what
    you type, so the text can never be invisible (plan.md B18)."""
    panel(
        "✎  NEW OBJECTIVE",
        Text("What should the swarm do?", style="bold")
        + Text("\n\nEnter runs it   ·   leave empty and press Enter for the menu",
               style=DIM),
    )

    try:
        goal = Prompt.ask(
            f"[bold {ACCENT}]│ ▸[/]", default="", show_default=False,
        )
    except (KeyboardInterrupt, EOFError):
        return ""

    return goal.strip()


def pick(title: str, options: list, initial: int = 0):
    """Choose one option. Arrows/j-k/enter when possible, numbered
    prompt otherwise. None = cancelled."""
    if not options:
        return None

    if len(options) == 1:
        return options[0]

    initial = max(0, min(initial, len(options) - 1))

    try:
        return _pick_keys(title, options, initial)
    except Exception:
        return _pick_numbered(title, options)


def _pick_panel(title: str, options: list, selected: int) -> Panel:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=2)
    grid.add_column()

    for i, option in enumerate(options):
        if i == selected:
            grid.add_row(
                Text("❯", style=ACCENT),
                Text(option, style="bold white"),
            )
        else:
            grid.add_row(Text(" "), Text(option, style=DIM))

    return Panel(
        grid, title=f"[bold]{title}[/]", title_align="left",
        border_style=ACCENT, box=box.ROUNDED, padding=(1, 2),
    )


def _read_key(fd: int) -> str:
    """One key, reading the RAW fd.

    Reading sys.stdin (buffered) defeats select(): the arrow's tail
    bytes sit in Python's buffer, select() reports "no data", and ESC
    was misread as a cancel — arrows silently did nothing (plan.md B19).
    """
    data = os.read(fd, 1)

    if data != b"\x1b":
        return data.decode("utf-8", "ignore")

    seq = b""
    deadline = time.time() + 0.1

    while len(seq) < 2 and time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.time()))
        if not ready:
            break
        seq += os.read(fd, 2 - len(seq))

    return {
        b"[A": "up", b"[B": "down", b"[C": "down", b"[D": "up",
        b"OA": "up", b"OB": "down", b"OC": "down", b"OD": "up",
    }.get(seq, "esc")


def _pick_keys(title: str, options: list, initial: int = 0):
    """Arrow-key selector on a live panel (falls back on any problem)."""
    import termios
    import tty

    from rich.live import Live

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    selected = initial

    try:
        tty.setcbreak(fd)

        with Live(_pick_panel(title, options, selected),
                  console=console, refresh_per_second=12) as live:
            while True:
                key = _read_key(fd)

                if key in ("up", "k"):
                    selected = (selected - 1) % len(options)
                elif key in ("down", "j", "\t"):
                    selected = (selected + 1) % len(options)
                elif key in ("\r", "\n"):
                    return options[selected]
                elif key in ("esc", "q", "\x03"):
                    return None
                elif key.isdigit() and 1 <= int(key) <= len(options):
                    return options[int(key) - 1]

                live.update(_pick_panel(title, options, selected))

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _pick_numbered(title: str, options: list):
    """Numbered fallback — the terminal's own line editing."""
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="right", style=ACCENT, width=3)
    table.add_column()

    for i, option in enumerate(options, 1):
        table.add_row(str(i), Text(option))

    panel(title, table)

    try:
        answer = Prompt.ask(
            f"[bold {ACCENT}]│ ▸ number (enter cancels)[/]",
            default="", show_default=False,
        )
    except (KeyboardInterrupt, EOFError):
        return None

    if answer.strip().isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]

    return None


def router_status():
    heading(f"ROUTER  ·  {ROUTER_URL}")

    table = Table(box=box.SIMPLE, padding=(0, 2))
    table.add_column("status", justify="right")
    table.add_column("model")

    try:
        with urllib.request.urlopen(f"{ROUTER_URL}/health", timeout=5) as r:
            health = json.loads(r.read() or b"{}").get("status", "?")
        table.add_row(Text("ok", style=OK if health == "ok" else ERR), Text("health"))
    except Exception as error:
        table.add_row(Text("down", style=ERR), Text(f"health — {error}"))
        console.print(table)
        return

    try:
        for model in model_catalog():
            style = {"loaded": OK, "loading": WARN}.get(model["status"], DIM)
            table.add_row(Text(model["status"], style=style), Text(model["id"]))
    except Exception as error:
        table.add_row(Text("error", style=ERR), Text(str(error)))

    console.print(table)


def choose_models():
    heading("CHOOSE MODELS")

    try:
        catalog = [m["id"] for m in model_catalog()]
    except Exception as error:
        panel("models", Text(f"router unreachable — {error}"), ERR)
        return

    if not catalog:
        panel("models", Text("the router reports no models"), ERR)
        return

    selection = describe()
    picked = {}

    for role in ("planner", "executor"):
        current, source = selection[role]
        keep = f"↩  keep {current}   [{source}]"
        options = [keep] + [mid for mid in catalog if mid != current]

        choice = pick(f"{role} model — enter picks · arrows move · esc keeps it", options)

        if not choice or choice == keep:
            continue

        picked[role] = choice

    if picked:
        saved = save_selection(**picked)
        panel("models", Text(
            f"planner   {saved['planner']}\n"
            f"executor  {saved['executor']}\n\n"
            f"saved to state/models.json", style="bold"), OK)
    else:
        note("kept the current models.")


def browse_history():
    state = load_state()
    history = state.get("history") or []

    if not history:
        note("No history yet — run an objective first.")
        return

    options = [
        f"{i + 1:03d}  {e.get('agent', '?'):<8s} {e.get('type', '?'):<9s} "
        f"{' '.join((e.get('output') or '').split())[:52]}"
        for i, e in enumerate(history)
    ]

    choice = pick("history — enter reads · esc cancels", options)

    if not choice:
        return

    entry = history[int(choice.split()[0]) - 1]
    panel(
        f"{entry.get('agent')} · {entry.get('type')}",
        Text(entry.get("output") or "(empty)"),
    )


def show_answer(answer: str):
    if not answer:
        reviews = [
            e for e in load_state().get("history") or []
            if e.get("type") == "review"
        ]
        answer = reviews[-1].get("output") if reviews else ""
        note("no answer in this session — showing the last review instead")

    if not answer:
        note("nothing to show yet.")
        return

    heading("ANSWER")
    console.print(Markdown(answer))


# ------------------------------------------------------------------- run

def run_objective(goal: str, max_iterations: int) -> str:
    state_before = load_state()
    iterations_before = (
        0 if state_before.get("goal") != goal
        else state_before.get("iteration", 0)
    )

    heading(f"RUNNING  ·  {max_iterations} cycle(s)")
    note(f"objective: {goal}")

    error = ""
    answer = ""

    try:
        answer = run_swarm(goal, max_iterations=max_iterations) or ""
    except RuntimeError as exc:
        error = str(exc)

    state = load_state()
    status = state.get("status", "unknown")

    if error:
        panel("ERROR", Text(error, style=ERR), ERR)
        return status

    panel("RESULT", Text(
        f"status   {status}\n"
        f"cycles   {state.get('iteration', 0) - iterations_before} this run"
        f"  ·  {state.get('iteration', 0)} total",
        style=status_color(status),
    ), status_color(status))

    if answer:
        show_answer(answer)
    elif status != "completed":
        note("objective not finished — “Continue the last objective” runs more cycles.")

    return status


# ------------------------------------------------------------------ main

def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    args = sys.argv[1:]
    max_iterations = 2

    if args and args[0] in ("--help", "-h"):
        print(__doc__ or "")
        raise SystemExit(0)

    if args and args[0] == "--iterations":
        if len(args) < 2:
            print("Missing value for --iterations")
            raise SystemExit(1)
        max_iterations = int(args[1])
        args = args[2:]

    # Scriptable mode: same contract as run.py — run once, exit.
    if args:
        goal = " ".join(args).strip()
        status = run_objective(goal, max_iterations)
        raise SystemExit(0 if status == "completed" else 1)

    if not sys.stdin.isatty():
        print("No terminal attached — pass an objective as an argument,")
        print('e.g.  ./tui.py "check who is logged in"')
        raise SystemExit(1)

    last_answer = ""

    def remember_answer(status: str):
        nonlocal last_answer
        if status != "completed":
            return
        reviews = [
            e for e in load_state().get("history") or []
            if e.get("type") == "review"
        ]
        last_answer = reviews[-1].get("output", "") if reviews else ""

    while True:
        try:
            banner(load_state())

            # Home is the input dialog box; an empty line drops to the menu.
            goal = ask_objective()

            if goal:
                cycles = pick("replan cycles (one is enough for most goals)",
                              ["1", "2", "3", "4", "5"], initial=1)
                cycles = int(cycles) if cycles and cycles.isdigit() else max_iterations

                if Confirm.ask(
                    f"[bold {ACCENT}]run[/] {goal[:70]}  ·  {cycles} cycle(s)?",
                    default=True,
                ):
                    remember_answer(run_objective(goal, cycles))

            else:
                action = pick("what next?", MENU)

                if not action or action == "Quit":
                    note("bye.")
                    return

                if action == "New objective":
                    continue                      # back to the dialog

                if action == "Continue the last objective":
                    previous = load_state().get("goal")
                    if not previous:
                        note("no previous objective — type one instead.")
                    else:
                        cycles = pick(f"more cycles for: {previous[:60]}",
                                      ["1", "2", "3", "4", "5"], initial=1)
                        cycles = int(cycles) if cycles and cycles.isdigit() else max_iterations
                        remember_answer(run_objective(previous, cycles))

                elif action == "Choose models":
                    choose_models()

                elif action == "Show the last answer":
                    show_answer(last_answer)

                elif action == "Browse the run history":
                    browse_history()

                elif action == "Router status":
                    router_status()

            next_step = pick("done — what now?",
                             ["Back to the input",
                              "Show the last answer",
                              "Choose models",
                              "Quit"])

            if not next_step or next_step == "Quit":
                note("bye.")
                return
            if next_step == "Show the last answer":
                show_answer(last_answer)
            elif next_step == "Choose models":
                choose_models()

        except KeyboardInterrupt:
            console.print()
            note("interrupted — bye.")
            return


if __name__ == "__main__":
    main()
