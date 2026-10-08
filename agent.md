# agent.md — How AI agents built this, and how to reproduce it with one

> Looking for the **instructions the swarm's own agents receive** —
> personas, tool protocol, report JSON? That is
> [agents/planner.md](agents/planner.md) and [agents/cpm.md](agents/cpm.md).
> This file is about how humans + AI agents *developed* the system.

Two audiences:

1. **A developer directing an AI coding agent** — how we worked, what
   produced this codebase, and a copy-paste runbook to rebuild it in a
   fresh environment.
2. **The AI agent itself** — the task list T1–T12 below. Work through
   them in order; every gate must pass before the next task. Report each
   gate with the **exact command and its output**.

Companion files: [scaffold.md](scaffold.md) (what to rebuild),
[plan.md](plan.md) (bug ledger — read B8 before touching `llm.py`),
[playbook.md](playbook.md) (debugging manual + case files).

---

## 1. The collaboration model

```text
HUMAN                    AI AGENT
─────                    ────────
symptom, outcome   →     diagnosis + fix
"it gets stuck"    →     evidence chain (process, router, GPU, logs)
constraints        →     experiments + measurements
judgment calls     ←     "here is the failing run" / "here is the fixed one"
```

Rules that made this work:

- The human reports **raw symptoms** ("models is loading but getting
  stuck"), never hypotheses. The agent turns each symptom into a
  root-cause hunt.
- The agent never ends a turn with "should now work". It shows the
  **command and its output** from the real system.
- Disagreements are settled by experiment, not argument.
- One root cause per fix; a full re-run after every fix.

## 2. Methodology (what the agent must do)

1. **Recon before code.** Verify every external dependency end to end
   once: router health, real model IDs from `GET /models`, one real
   completion per model. Hard-code nothing guessed.
2. **Read the state, not the story.** When a run misbehaves, look at
   `state/swarm.json`, `tasks/current.json`, the process (`/proc/<pid>/wchan`,
   open fds), the router (`/models` statuses), and the GPU (`nvidia-smi`)
   before touching code. See playbook.md §6.
3. **Instrument, don't guess.** Wall time per role, `[tool]` per step,
   `[llm]` per load. If the data is missing, add the probe first.
4. **One root cause per fix.** Hypothesis → smallest experiment → fix →
   full re-run. Batched "improvements" hide regressions.
5. **Disk is truth.** Acceptance is a file on disk with the right
   content, a state file with the right status, a transcript line with a
   real timing — never a model's claim.
6. **Encode every lesson.** Every defensive line traces to a bug number
   in plan.md. Delete the line or document the failure.

## 3. Small-model failure taxonomy (memorize this)

Both roles are ~2B local models. These failure modes are *normal* at this
scale; the harness exists to absorb them. The pattern: **never fight a
failure mode with more prose — beat it with structure** (formats, gates,
bounds, code checks).

| Failure | Seen as | Harness defense |
|---|---|---|
| Plan shape drift | code fences with trailing chatter, markdown tables, bold checkboxes, template scaffolds (`[Analyze the problem…]`) | continuation few-shot (`<PLAN>\n1. ` tail) + tolerant `extract_plan` (`_plan_score` picks the fence when the plan is in it); language-tagged fences and scaffold blocks rejected |
| Filler plans | "Research the current state of the problem…", 3 tasks, zero content | `plan_matches_goal` content-word gate → retry → fallback plan |
| **Tool-syntax drift** | one model, four call shapes in one objective: XML `<function name=x><param name=k>`, fenced JSON, `{"name":…,"path":…}` flattened, `{"edit":{…}}` name-as-key | shape-blind `parse_tool_call` (all shapes + truncated JSON), filled `TOOL_SPECS` examples, one corrective `NUDGE` turn, `[tool?]` warning |
| Markup noise in args | `<![CDATA[w && who]]>` run literally; CDATA ended up inside a written file | `_unwrap_cdata` on every string arg in `run_tool` and in `write` |
| Narrative instead of work | "I will now check…" whole turn, no tool call | one turn = one tool call or the report; corrective turn on drift; bare JSON calls accepted |
| Missing final report | tool work done, then rambling/cut off | summarize-from-trace **only when the trace is non-empty** (never blind-retry the work), then deterministic synthesis |
| Fabricated summarize output | the report copied the few-shot example ("up 3:55") as evidence | example labelled FORMAT SAMPLE, copy forbidden, empty trace never reaches this path |
| Hallucinated completion | "file written", zero tool activity | ground-truth gate: `completed` + empty trace → `blocked` |
| Thinking-channel answers | `content` empty, everything in `reasoning_content` | `chat()` falls back to `reasoning_content` |
| Dead turns | empty reply after a mid-swap 500 | `chat()` retries with re-warm; orchestrator re-runs once |
| Evidence spam / dumps | one command re-run per task; raw console output | `dedupe_evidence` (digit-normalized), trace backfill, answer dedupe vs prose |
| Empty results as facts | `who` empty ⇒ "no users"; empty file written as "the result" | `(no output)` is explicit in tool output; cross-check reminder attached **only when a result is empty**; persona: `w` first, "an empty file is a failed action" |
| Tag drift in reviews | unclosed `<DECISION>`, `DECISION: DONE` as a bare line | tolerant `extract_decision`; missing marker ⇒ decide from the report's structure |
| Template filler as the answer | "[Complete, well-structured plan…]" + "Brief justification…" | `_strip_scaffold` on answer prose; facts from the report always appended |
| Context overflow | rambling turns, truncation mid-JSON | `max_tokens` 2048, tool output clipped to 2000 chars, `_repair_json` for truncation |
| Failure rationalization | "the command isn't available here" | "a failed run is a problem, never a pass" (persona) |

## 4. Prompt patterns that worked

- **Contracts in the orchestrator, identity in the prompt files.** The
  execution contract (objective, plan, stop conditions) is injected per
  call; the persona + tool protocol live in `agents/*.md`.
- **Continuation few-shot over instructions.** The plan prompt ends with
  `<PLAN>\n1. ` after two filled examples. Plain instructions made the
  distill emit scaffolds instead of plans.
- **Closed output formats:** `<PLAN>`, `<DECISION>DONE|REPLAN</DECISION>`,
  `<ANSWER>`, and the single JSON report
  (`status/tasks_done/findings/evidence/blocked/needs_replanning`) — all
  machine-parsed, all tolerant of one missing piece.
- **Autonomous executor, chaperone planner.** The worker owns the whole
  plan in one call; the planner only plans and reviews. Never pay a model
  swap for a micro-step.
- **Evidence everywhere.** Findings cite real output; the answer may only
  restate the report's facts.
- **Stateless prompts.** Every prompt contains everything needed; nothing
  depends on earlier turns.
- **Fallbacks over failures.** Missing decision ⇒ derive from the
  report's status + trace; unparseable JSON ⇒ repair ⇒ salvage scalars;
  no plan ⇒ minimal honest plan; never crash on model wobble.

## 5. Reproduction runbook (for an AI agent, fresh environment)

Paste this file together with `scaffold.md` and `playbook.md` to the
agent, then:

```text
Rebuild the project documented in scaffold.md in this fresh environment.
Work through tasks T1–T12 in order. Do not start a task until the
previous gate passes. Report each gate with the exact command and its
output. Judge everything by running programs, never by transcripts.
When a gate fails, diagnose the root cause before changing code
(playbook.md §6 has the decision tree).
```

### T1 — Environment
Install the llama CLI (`curl -LsSf https://llama.app/install.sh | sh`),
Python 3.10+, and — for the `tui.py` front end — the rich package
(`pip install rich`). Note the GPU
(`nvidia-smi`).
**Gate:** `llama version`, `python3 --version`, `python3 -c "import rich"`,
`nvidia-smi` all run. Record the exact output — model sizes and VRAM
decide the router flags.

### T2 — Models
Download both GGUFs (`llama download -hf <repo>:<quant>`), matching the
table in README §Requirements to your VRAM (see playbook.md §2).
**Gate:** `curl -s http://127.0.0.1:8080/models` (after T3) lists both
IDs **exactly**; `du -sh` on the cache entries shows ~2GB each.

### T3 — Router
Start the router exactly as in README §Requirements.
**Gate:** `curl -s $LLAMA_BASE_URL/health` → `{"status":"ok"}` and
`curl -s $LLAMA_BASE_URL/models | jq -r '.data[].id'` prints the two IDs.
Copy the IDs from this output into `swarm/orchestrator.py` — never
guess them.

### T4 — Router API + per-model smoke (before any swarm code)
Verify the endpoints and the reasoning-channel behavior:

```bash
curl -s -X POST $LLAMA_BASE_URL/models/load   -H 'Content-Type: application/json' -d '{"model":"<id>"}'
curl -s $LLAMA_BASE_URL/models | jq '.data[] | {id, s:.status.value}'
curl -s $LLAMA_BASE_URL/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"<id>","messages":[{"role":"user","content":"Say OK and nothing else."}],"max_tokens":64}'
```

**Gate:** load answers `{"success":true}` **and** the status reaches
`loaded` (watch it); the completion returns text in `content` or
`reasoning_content`. Repeat per model. If a load never reaches `loaded`,
read playbook.md §3–§4 before continuing — do not build the client on
top of an unverified router.

### T5 — Scaffold
Create the tree from scaffold.md (all five `swarm/` modules, both
personas, `run.py`, `tui.py`, `state/`, `tasks/`).
**Gate:** `python3 -m py_compile run.py tui.py swarm/*.py` passes.

### T6 — First full loop
`./run.py "check who is logged in"`.
**Gate:** transcript shows `[llm] loading … ready in Ns`, `[PLAN — Ns]`,
`[tool] …`, `[CPM — Ns]` with a JSON report, `[REVIEW — Ns]` with
`<DECISION>`; `state/swarm.json` ends `completed` with 3 history entries
(`plan`, `execution`, `review`); `tasks/current.json` is `completed`.
If the review says `REPLAN` twice, that is acceptable for now (T7 checks
the loop bounds).

### T7 — Tool execution is real
`./run.py "write the word hello to /tmp/a.txt and read it back"`.
**Gate:** `cat /tmp/a.txt` prints `hello` **and** the report's evidence
quotes the write + the read-back. A "completed" claim without disk
changes is a B4-class failure: find why the gate let it through.

### T8 — Ground-truth gate
Simulate a hallucinated completion: make a run where the worker cannot
use tools (temporarily point `CPM_MODEL` at a model id that answers
without tools, or stub `run_tool` to return `ERROR: tools disabled`) and
use the same goal.
**Gate:** the run does **not** end `completed` on tool-less claims — the
report comes back `blocked`/`needs_replanning` or the review answers
`REPLAN`. Restore afterwards.

### T9 — Model swap endurance
Watch statuses in a second shell
(`watch -n2 'curl -s $LLAMA_BASE_URL/models | jq -r ".data[]|select(.status.value!=\"unloaded\")|.id+\" \"+.status.value"'`)
while running `./run.py "check the uptime and the logged in users"`.
**Gate:** exactly one model is ever `loaded`; swaps take 3–10s; no state
stays `loading` for more than ~30s. Any freeze here is B8: read
plan.md B8 and playbook.md §7.

### T10 — Failure path is fast and loud
Stub eviction and ask for a model that cannot load
(playbook.md §8 has the 6-line test).
**Gate:** the failure surfaces in **seconds** with
`[llm] … load was dropped by the router` and an actionable `ERROR:` line
— never a silent multi-minute wait and never a raw traceback.

### T11 — Performance
Read the `[PLAN — Ns]` / `[CPM — Ns]` / `[REVIEW — Ns]` timings.
**Gate:** planning and review under ~30s warm, a tool-heavy cycle under
~2 minutes, a full run under ~5 minutes on comparable hardware
(GTX 1050 Ti). Slower means the model is loading per call: check the
`[llm]` lines.

### T12 — Documentation sync
Update README/scaffold numbers if reality differs (model IDs, sizes,
timings, flags).
**Gate:** a second agent, given only README + scaffold + playbook,
passes T1–T7 without asking questions.

## 6. Directing the swarm itself

Good goal texts state the outcome, the acceptance behavior, and the
current wrong state if known:

```text
Good: "check the uptime and who is logged in, and write both into
       /tmp/report.txt. The file must exist and contain the real output."

Bad:  "look at the system"        (no acceptance behavior)
Bad:  "fix the run"               (invents context the executor will parrot)
```

Expect noisy transcripts; judge outcomes on disk and in `state/`.
Use `--iterations N` for multi-step goals. Keep goals under ~3 sentences.

## 7. Anti-patterns (things that wasted time here)

- Trusting a transcript instead of running the program.
- Treating `{"success": true}` from `/models/load` as proof of a load.
- Waiting longer on a state that cannot change (the 10-minute freeze was
  a *bounded* wait that felt infinite because nothing printed).
- Patching the symptom (retry the empty turn) before finding *why* it was
  empty (500 mid-swap vs reasoning channel — two different causes).
- Arguing about model behavior instead of running one controlled test.
- Making prompts longer to fix discipline that a gate fixes better.
- Crashing a run on a protocol wobble where a fallback keeps the loop.
