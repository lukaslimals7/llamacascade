# Local AI Swarm — no pi edition

Same swarm as `local-swarm/` (plan → autonomous execution → review), but
**no agent runtime at all**: roles are plain HTTP chat calls against the
llama.cpp router's OpenAI-compatible API, and the worker's tools are
Python functions. Python standard library only — no `pi`, no Node, no
pip installs.

## Documentation map

| File | Read it for |
|---|---|
| **README.md** | usage: install, run, interpret output |
| [playbook.md](playbook.md) | reproduce in a new environment step by step; debugging manual; case files |
| [scaffold.md](scaffold.md) | code surface, protocols, state shapes, config knobs |
| [plan.md](plan.md) | how it was built and every bug we fixed (ledger) |
| [agent.md](agent.md) | methodology + gated runbook T1–T12 for rebuilding with an AI agent |

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
state/, tasks/             run state, same layout as local-swarm/
```

## Requirements

- Linux, Python 3.10+, the `llama` CLI (llama.cpp router mode)
- Two GGUFs (~2 GB each) — one for planning/review, one for execution
- `rich` — optional, only for the `./tui.py` front end (`pip install rich`)
- Tested on a **4 GB GTX 1050 Ti** (Vulkan). CPU-only works (see
  playbook.md §4)

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
Full setup with expected output at every step: [playbook.md](playbook.md) §2.

## Run

### Interactive TUI (recommended)

```bash
./tui.py
```

A `rich`-powered front end (pure Python) — no `./run.py ""` typing. The
**first thing on screen is the input dialog box**:

```
╭─ ✎  NEW OBJECTIVE ────────────────────────────────────╮
│  What should the swarm do?                            │
│  Enter runs it   ·   Esc opens the menu               │
│ ▸ check who is logged in and write it to users.txt▌   │
╰───────────────────────────────────────────────────────╯
```

- **type the objective, press Enter** → replan cycles → confirm → the
  run streams live (plan → tool steps → report → review) between styled
  headers
- **result** — color-coded status box + the answer rendered as markdown
  (paged when long)
- **Esc opens the menu** — Continue the last objective (more cycles),
  **Choose models**, Browse the run history (fuzzy filter + pager),
  Router status (health + per-model load state), Quit
- banner always shows the active planner/executor models and where the
  choice came from (`default` / `state/models.json` / `env`)

Same scriptable contract as the CLI: `./tui.py "objective"` runs one
objective and exits (`--iterations N` works too). Needs the `rich`
package (`pip install rich`); without it the script prints install
hints and `run.py` still works. Input is line-mode — the terminal
itself echoes every keystroke, so what you type is always visible — and
the arrow-key menus fall back to numbered prompts if raw keys are
unavailable.

### CLI

```bash
./run.py "write hello to a.txt"
./run.py --iterations 4 "check the uptime and who is logged in"
```

One cycle = plan → autonomous execution → review. `--iterations N`
bounds replan cycles (default 2: most goals need one cycle).

### Choosing the models

Any router model can play either role — pick from the TUI menu
(**Choose models**, fuzzy search over `curl $LLAMA_BASE_URL/models`) or
from the CLI (the choice persists in `state/models.json`):

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
prints what it uses (`[models] planner: …` / `[models] executor: …`);
keep both models around ~2 GB for a 4 GB GPU (playbook.md §4). A model
that is not in the router catalog fails fast with
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
`ERROR: …` + exit 1 = router/model problem (playbook.md §6).

## How execution works

The worker loops up to 12 tool steps per cycle. A turn is either one tool
call in a fenced block —

````
```tool
{"name": "bash", "args": {"command": "who"}}
```
````

— or the final JSON report. The orchestrator holds the same ground-truth
line as the pi edition: a report that claims `completed` with **zero tool
activity** is downgraded to `blocked`, because a small model asserting
"file written" without a write in the trace is hallucinating.

## Model loading (why runs used to freeze)

Both models are ~2GB and the GPU has 4GB VRAM, so **only one can be
resident**. Asking the router for the second model while the first is
loaded fails inside the spawned model server
(`ggml_vulkan: … ErrorOutOfDeviceMemory`) — yet `/models/load` already
answered `{"success": true}` and the model silently drops back to
`unloaded`. The old client waited 10 minutes for a `loaded` that never
came, with no output: the run looked stuck.

`swarm/llm.py` now warms every model explicitly before each role:

- unloads the other resident model first (`POST /models/unload`) so the
  load can actually fit in VRAM;
- trusts the status poll, not the load response: `loading → unloaded`
  is a failed load and gets retried (3 attempts), not waited on;
- prints its progress (`[llm] loading …`, `[tool] …`) so a slow load or
  a long tool step never looks like a freeze, and `run.py` line-buffers
  the output;
- gives an actionable error instead of a hang when the router really
  cannot load a model.

Note `-ngl 999` disables llama.cpp's automatic "fit layers to free VRAM"
fallback (`n_gpu_layers already set by user … abort`), so a model that
does not fit fails instead of falling back to CPU layers. VRAM budgeting
table: [playbook.md](playbook.md) §4.

## Small-model handling

- **Tool calls**: the parser is shape-blind — XML `<function …><param …>`,
  fenced or bare JSON, flattened `{"name":"write","path":…}`, name-as-key
  `{"edit":{…}}` and calls truncated mid-key all execute (one model used
  all four on one objective; accepting one shape meant doing no work).
  A reply with no parseable call and no report gets **one corrective
  turn** before the loop gives up.
- **Markup noise**: `<![CDATA[…]]>` wrappers are stripped from tool
  arguments and file content; writing an empty file returns a loud
  warning ("an empty file is a failed action").
- **Plan**: continuation-style few-shot (`<PLAN>\n1. ` prompt tail), on the
  retry too. Plain instructions make the distill emit scaffolds/bashes/
  tables instead. A plan inside a code fence is recovered even when the
  model appends chatter ("Brief explanation of each step."); template
  scaffolds (`[Analyze the problem…]`) are rejected. A parsed plan must
  also share a content word with the objective ("Research the current
  state of the problem…" filler is rejected), and a persistent failure
  falls back to a minimal plan instead of aborting the run.
- **Review**: two filled examples + "start with `<DECISION>`"; verified to
  answer DONE/REPLAN correctly including half-done work. Template filler
  in the review is stripped from the final answer.
- **Summarize**: few-shot raw-trace → short-facts example, so the report
  carries findings like "user lukas is logged in", not console dumps.
  Only runs on a **non-empty** trace — a trace-less summarize turn just
  copies the example and invents evidence.
- **Evidence**: deduplicated with digit normalization — re-running one
  command per task ('up 3:55' vs 'up 3:56') collapses to one fact. The
  final answer repeats nothing the review's prose already states.
- **Parsers** tolerate unclosed tags, markdown tables, code fences,
  bold/checkbox bullets, bare `DECISION: DONE` lines, truncated JSON,
  fenced or bare tool-call JSON.
- **Dead turns** (no text, no tool call — e.g. router answered 500
  mid-swap) are retried after re-warming the model.
- **Empty tool results are not facts**: `bash` reports `(no output)`
  explicitly, and a cross-check reminder is attached to empty results
  (`who` misses graphical sessions — `w` sees them) before the worker
  may report "none".

## Troubleshooting

| Symptom | Go to |
|---|---|
| run hangs / `ERROR: Router never loaded …` | playbook.md §6.3 + case C1 |
| model id not found | copy ids from `curl -s $LLAMA_BASE_URL/models` |
| executor runs zero tools, no file created | plan.md B12 (tool-call syntax drift) |
| file created but empty / "no users" | plan.md B16 (empty results) |
| answer is template filler ("[Complete, well-structured plan…]") | plan.md B14/B16, `_strip_scaffold` |
| reports missing / plan malformed | README §Small-model handling, plan.md B1/B5/B13/B14 |
| `completed` but nothing on disk | plan.md B4 (ground-truth gate) + B12 |
| slow turns | playbook.md §4 (VRAM) and §6.3 |

## Differences from `local-swarm/`

| | local-swarm (pi) | local-swarm-nopi |
|---|---|---|
| agent runtime | `pi --mode rpc` subprocess | none — HTTP + Python |
| tool execution | pi's tools | `swarm/tools.py` |
| model calls | pi → llama.cpp | `swarm/llm.py` → llama.cpp |
| model warm-up | hidden in pi | explicit: evict → load → poll (see above) |
| dependency | Node + pi | Python only |
