#!/usr/bin/env python3
"""Model selection: which router models the swarm roles use.

Defaults live here; the user's choice persists in state/models.json and
environment variables can override either — precedence:

    SWARM_PLANNER_MODEL / SWARM_EXECUTOR_MODEL   (per-invocation)
    state/models.json                            (what the user picked)
    DEFAULT_PLANNER / DEFAULT_EXECUTOR           (shipped defaults)

Pick models with:
    ./tui.py            ->  "Choose models" in the menu
    ./run.py --planner <id> --executor <id> "objective"   (persists)
    ./run.py --list-models / --reset-models
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MODELS_FILE = ROOT / "state" / "models.json"

ENV_PLANNER = "SWARM_PLANNER_MODEL"
ENV_EXECUTOR = "SWARM_EXECUTOR_MODEL"

DEFAULT_PLANNER = "empero-ai/Qwen3.8-2B-Distill-GGUF:Q8_0"     # plan + review
DEFAULT_EXECUTOR = "openbmb/MiniCPM5-2B-GGUF:Q8_0"             # worker (CPM)

# Shipped alternates, kept for rollback/experiments (as before):
#   planner:  bartowski/granite-4.2-3b-GGUF:Q4_K_M
#   executor: peculiar-ragdoll/Sharp-MiniCPM5-2B-GGUF:Q6_K_XL  (removed)


def load_selection() -> dict:
    """The persisted choice: {"planner": …, "executor": …}, Nones default."""
    if not MODELS_FILE.exists():
        return {"planner": None, "executor": None}

    try:
        data = json.loads(MODELS_FILE.read_text())
    except Exception:
        return {"planner": None, "executor": None}

    return {
        "planner": data.get("planner") or None,
        "executor": data.get("executor") or None,
    }


def save_selection(planner: str = None, executor: str = None):
    """Persist the chosen models (None keeps whatever is already saved)."""
    current = load_selection()

    if planner is not None:
        current["planner"] = planner or None
    if executor is not None:
        current["executor"] = executor or None

    MODELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODELS_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=False))

    return current


def clear_selection():
    """Back to the shipped defaults."""
    if MODELS_FILE.exists():
        MODELS_FILE.unlink()


def effective_models() -> tuple:
    """(planner_id, executor_id) actually used for the next run."""
    saved = load_selection()

    planner = os.environ.get(ENV_PLANNER) or saved["planner"] or DEFAULT_PLANNER
    executor = os.environ.get(ENV_EXECUTOR) or saved["executor"] or DEFAULT_EXECUTOR

    return planner, executor


def describe() -> dict:
    """Where each effective model comes from — for --list-models."""
    saved = load_selection()
    out = {}

    for role, env, saved_id, default in (
        ("planner", ENV_PLANNER, saved["planner"], DEFAULT_PLANNER),
        ("executor", ENV_EXECUTOR, saved["executor"], DEFAULT_EXECUTOR),
    ):
        if os.environ.get(env):
            out[role] = (os.environ[env], f"env {env}")
        elif saved_id:
            out[role] = (saved_id, "state/models.json")
        else:
            out[role] = (default, "default")

    return out
