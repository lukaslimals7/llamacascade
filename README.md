# Llamacascade

A plan → autonomous execution → review swarm for local models, in pure
Python. Give it an objective: a planner turns it into numbered steps, an
executor works through them with real tools (bash, read, write, edit,
grep, find, ls), and a reviewer decides DONE or REPLAN.

There is **no agent runtime**  roles are plain HTTP chat calls against
the llama.cpp router's OpenAI-compatible API, and the tools are Python
functions. Python standard library only: no Node, no framework, no pip
installs (`rich` is optional, only for the TUI).

## Layout

```
run.py                     CLI: ./run.py "objective" [--iterations N]
tui.py                     interactive TUI (rich): input dialog, menu,
                           live progress, rendered answer, history
swarm/
  llm.py                   router client: model warm-up + /v1/chat/completions
  tools.py                 bash/read/write/edit/grep/find/ls in-process
  agent.py                 tool loop: reply -> tool call -> result -> …
  parsing.py               tolerant <PLAN>/<DECISION>/JSON report parsers
  orchestrator.py          plan -> execute -> review loop, state files
agents/
  planner.md               planner/reviewer persona
  cpm.md                   executor persona (tool protocol included)
state/, tasks/             run state (goal, history, current plan)
```

## Requirements

- Linux, Python 3.10+, the `llama` CLI (llama.cpp router mode)
- Two GGUFs (~2 GB each) — one for planning/review, one for execution
- `rich` — optional, only for the `./tui.py` front end (`pip install rich`)
- Tested on a **4 GB GTX 1050 Ti** (Vulkan). CPU-only works.

Install the CLI and models:

```bash
curl -LsSf https://llama.app/install.sh | sh
llama download -hf empero-ai/Qwen3.8-2B-Distill-GGUF:Q8_0
llama download -hf openbmb/MiniCPM5-2B-GGUF:Q8_0
```

Start the router:

```bash
llama server --models-max 1 --models-autoload --jinja \
  --host 127.0.0.1 --port 8080 -ngl 999 -c 16278 -np 1
```

Models are referenced by router ID (see `swarm/orchestrator.py`; get the
exact strings from `curl -s http://127.0.0.1:8080/models`):

| Role | Model |
|---|---|
| planner/reviewer | `empero-ai/Qwen3.8-2B-Distill-GGUF:Q8_0` |
| executor (CPM) | `openbmb/MiniCPM5-2B-GGUF:Q8_0` |

Router not on 8080? `export LLAMA_BASE_URL=http://host:port`.

## Run

### Interactive TUI (recommended)

```bash
./tui.py
```

A `rich`-powered front end (pure Python) — no `./run.py ""` typing. The
first thing on screen is the input dialog box:

```
╭─ ✎  NEW OBJECTIVE ────────────────────────────────────╮
│  What should the swarm do?                            │
│  Enter runs it   ·   leave empty and press Enter for the menu │
│ ▸ check who is logged in and write it to users.txt▌   │
╰───────────────────────────────────────────────────────╯
```

- **type the objective, press Enter** → pick replan cycles → confirm →
  the run streams live (plan → tool steps → report → review)
- **result** — color-coded status box + the answer rendered as markdown
- **empty line opens the menu** — Continue the last objective, Choose
  models, Browse the run history, Router status, Quit
- banner always shows the active planner/executor models and where the
  choice came from (`default` / `state/models.json` / `env`)

Same scriptable contract as the CLI: `./tui.py "objective"` runs one
objective and exits (`--iterations N` works too). Needs `rich`; without
it the script prints install hints and `run.py` still works.

### CLI

```bash
./run.py "write hello to a.txt"
./run.py --iterations 4 "check the uptime and who is logged in"
```

One cycle = plan → autonomous execution → review. `--iterations N`
bounds replan cycles (default 2: most goals need one cycle).

### Choosing the models

Any router model can play either role — pick from the TUI menu
(**Choose models**) or from the CLI (the choice persists in
`state/models.json`):

```bash
./run.py --list-models                       # catalog + current selection
./run.py --planner bartowski/granite-4.2-3b-GGUF:Q4_K_M "objective"
./run.py --executor openbmb/MiniCPM5-2B-GGUF:Q8_0 "objective"
./run.py --reset-models                      # back to the shipped defaults
```

One-off overrides without touching the saved choice:

```bash
SWARM_PLANNER_MODEL=<id> SWARM_EXECUTOR_MODEL=<id> ./run.py "objective"
```

Precedence: env var → `state/models.json` → shipped defaults. Every run
prints what it uses (`[models] planner: …` / `[models] executor: …`).
A model that is not in the router catalog fails fast with
`ERROR: Model not in router catalog: …`.

Output legend:

| Marker | Meaning |
|---|---|
| `[llm] loading … ready in Ns` | model warmed (one model is resident at a time) |
| `[PLAN — Ns]` | the planner's numbered plan |
| `[tool] bash {…}` | one executed tool step |
| `[CPM — Ns]` | the executor's JSON report |
| `[REVIEW — Ns]` | the reviewer's `<DECISION>` + `<ANSWER>` |
| `Status: completed` | outcome of the run |

State lives in `state/swarm.json` (goal, history, status) and
`tasks/current.json` (the current plan) — `state/swarm.json` is the
first thing to read when a run misbehaves. Exit code 0 = run finished;
`ERROR: …` + exit 1 = router/model problem.

## How execution works

The worker loops up to 12 tool steps per cycle. A turn is either one tool
call in a fenced block —

````
```tool
{"name": "bash", "args": {"command": "who"}}
```
````

— or the final JSON report. Ground truth is enforced: a report that
claims `completed` with **zero tool activity** is downgraded to
`blocked`, because a small model asserting "file written" without a
write in the trace is hallucinating.

## Model loading (why runs used to freeze)

Both models are ~2 GB and the GPU has 4 GB VRAM, so **only one can be
resident**. Asking the router for the second model while the first is
loaded fails inside the spawned model server
(`ggml_vulkan: … ErrorOutOfDeviceMemory`) — yet `/models/load` already
answered `{"success": true}` and the model silently drops back to
`unloaded`. A naive client then waits 10 minutes for a `loaded` that
never comes, and the run looks stuck.

`swarm/llm.py` warms every model explicitly before each role:

- unloads the other resident model first (`POST /models/unload`) so the
  load can actually fit in VRAM;
- trusts the status poll, not the load response: `loading → unloaded`
  is a failed load and gets retried (3 attempts), not waited on;
- prints its progress (`[llm] loading …`, `[tool] …`) so a slow load or
  a long tool step never looks like a freeze;
- gives an actionable error instead of a hang when the router really
  cannot load a model.

Note `-ngl 999` disables llama.cpp's automatic "fit layers to free VRAM"
fallback (`n_gpu_layers already set by user … abort`), so a model that
does not fit fails instead of falling back to CPU layers.

## Small-model handling

Small models drift, so the harness is deliberately forgiving:

- **Tool calls**: the parser is shape-blind — XML `<function …><param …>`,
  fenced or bare JSON, flattened `{"name":"write","path":…}`, name-as-key
  `{"edit":{…}}` and calls truncated mid-key all execute. A reply with
  no parseable call and no report gets **one corrective turn** before the
  loop gives up.
- **Plan**: continuation-style few-shot prompt, code-fenced plans
  recovered, template scaffolds rejected, and a parsed plan must share a
  content word with the objective. Persistent failure falls back to a
  minimal plan instead of aborting the run.
- **Review**: two filled examples + "start with `<DECISION>`"; template
  filler in the review is stripped from the final answer.
- **Parsers** tolerate unclosed tags, markdown tables, code fences,
  bold/checkbox bullets, bare `DECISION: DONE` lines, truncated JSON.
- **Dead turns** (no text, no tool call — e.g. router answered 500
  mid-swap) are retried after re-warming the model.
- **Empty tool results are not facts**: `bash` reports `(no output)`
  explicitly, and a cross-check reminder is attached to empty results
  before the worker may report "none".

## Troubleshooting

| Symptom | Go to |
|---|---|
| run hangs / `ERROR: Router never loaded …` | is the router up? `curl $LLAMA_BASE_URL/health` — then see §Model loading |
| model id not found | copy ids from `curl -s $LLAMA_BASE_URL/models` |
| executor runs zero tools, no file created | check the tool-call trace in `state/swarm.json` |
| `completed` but nothing on disk | the ground-truth gate downgrades it — read `state/swarm.json` |
| slow turns | 4 GB GPU: only one model resident — see Model loading |
