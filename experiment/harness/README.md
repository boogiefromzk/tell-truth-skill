# tell-truth experiment harness (run 3)

A single script that runs the whole A/B battery in isolation — no folder surgery, no in-IDE
orchestrator, no eyeballing answers out of a streaming pane. It replaces protocol §4.1
"Method A" (and most of §4.2's ten hardening lessons) with `cursor-agent` one-shots.

## Why this is simpler *and* more robust

| Run-2 pain | What caused it | What this harness does |
|---|---|---|
| "a ton of pre-runs" | an LLM orchestrator guessing when a cell finished + folder-surgery isolation | a deterministic script; completion = the process exiting; isolation = a fresh temp cwd |
| 19 lost cells (12%) | the harness captured a model's *reasoning trace* as the answer | `cursor-agent -p` returns only the final message, thinking suppressed; a validation gate retries or writes `CAPTURE_FAILED` |
| 6 provider keys | direct-to-provider SDKs | **one** `CURSOR_API_KEY` reaches all 7 models |
| isolation ceremony | moving `~/.claude/skills` off the machine, `.cursorignore` layers, E1–E4 probe | fresh empty cwd per cell + verbatim-construction + one smoke probe |

The one control that matters — **verbatim construction** — is two lines of code
(`build_prompt`): condition A sends the prompt alone; condition B sends the SKILL block, a
blank line, then the prompt. Nothing else (no criteria, no framing) is ever appended.

**Single source in, `results/` out.** The skill block + all 23 prompts are read verbatim
from `../PROMPTS.md` — only the ```-fenced blocks, so the grading notes in that file's prose
never reach a model. Assembled transcripts are written to `../results/<model>.md`
(**overwriting** whatever is there).

## Setup

1. Install the Cursor CLI and confirm `cursor-agent` is on your PATH.
2. `cp .env.example .env` and paste your key into `CURSOR_API_KEY`
   (one key, from https://cursor.com/dashboard). All models bill to this one key.
3. Pick models — **most advanced thinking, newest versions:**
   ```bash
   python run_experiment.py --list-models      # see what your account can actually use
   ```
   The defaults are run-2's max-reasoning slugs; override the newest/highest-reasoning
   variant per family via `MODELS=` in `.env`.

## Run

```bash
python run_experiment.py --dry-run     # print the built cells + exact prompt text; spends nothing
python run_experiment.py --smoke       # one probe cell: confirms NO skills leak + web search fires
python run_experiment.py               # full sweep: 7 models x 23 prompts x {A,B}
```

Useful subsets while iterating:

```bash
python run_experiment.py --models opus,kimi --prompts N4,D6   # a few cells
python run_experiment.py --conditions B --prompts W3          # one condition
```

The run refuses to start unless the smoke gate passes (no skills in the agent's context +
web search works). Override only if you know why: `--skip-smoke`.

## What you get

```
../results/
  <model>.md                 # assembled transcript (scorer-ready), overwritten each run
  .parts/
    <model>/
      003_D3_A.md            # per-cell answer part (+ SEARCH/EXEC FIRED)
      003_D3_A.jsonl         # raw stream-json sidecar (reasoning + tool payloads, for audit)
      003_D3_A.meta.json     # {status, attempt, search, exec, tools}
```

- `SEARCH FIRED` / `EXEC FIRED` come from the tool-call events in the stream, **never** from
  what the answer claims — load-bearing for scoring the N1/N6 verification-theater probes.
- Any cell that can't produce a final answer after `CAPTURE_RETRIES` is written
  `[CAPTURE_FAILED …]` (never a silent blank, never reasoning text) and listed at the end so
  you re-run exactly those cells instead of discovering the hole weeks later at scoring.
- **Token use** is summed and printed at the end (from the stream's `usage`, else estimated
  from output length). **Out of credits/quota** aborts the whole run immediately with the
  exact billing message — not a wall of per-cell failures — and the smoke gate catches it too.
- Prompts + the SKILL block are read **verbatim** from `../PROMPTS.md` — the frozen v1
  wording is never re-transcribed, so it can't drift.

## Knobs (`.env`)

`CONCURRENCY` · `CELL_TIMEOUT` · `CAPTURE_RETRIES` · `SANDBOX` · `ISOLATE_HOME` (run with no
skills; default on) · `REQUIRE_SEARCH` (set 0 for a valid search-off run) · `DISCRIMINATOR_REPS`
(set to 5 for statistical depth on {D6,W3,W4,N3,N4}×{grok,kimi,opus}) · `MODELS` (slug overrides).

## Known caveats (verify before a scored run)

- **Model IDs move.** The defaults are run-2 slugs; always confirm with `--list-models`.
- **Global skills/rules.** The smoke probe is the tripwire — if it reports a skill leaked
  into condition A, remove `~/.cursor` skills for the run (or drive the TS SDK with
  `settingSources: []`). A fresh cwd blocks *project* rules, not *global* ones.
- **Harness change vs run 2.** These are SDK/CLI one-shots, not in-IDE agents — the within-run
  A→B delta is comparable, but absolute cell scores are a new baseline. Keep a small fixed
  anchor set if you need cross-run tracking.
- **Tool-event field names.** `SEARCH/EXEC FIRED` are matched by keyword on the tool name in
  the stream; the raw `.jsonl` sidecar is kept so any misclassification is auditable/fixable.
