#!/usr/bin/env python3
"""tell-truth experiment driver — isolated, deterministic per-cell runner (run 3 harness).

Replaces the in-IDE "Method A" orchestrator (folder surgery + E1-E4 probe gate +
.cursorignore layers + eyeballing the answer out of a streaming pane) with a plain
script that shells out to the Cursor Agent CLI once per cell. This makes the two
isolation guarantees STRUCTURAL instead of probe-verified, and kills the run-2
capture bug by construction:

  * one CURSOR_API_KEY reaches all seven models (no six-provider key juggling);
  * each cell runs in a FRESH empty temp dir  -> ground truth is unreachable, no
    project rules load  (isolation = the cwd, not a folder-surgery dance);
  * the prompt is built verbatim in code as  promptText (A)  or  skill + prompt (B)
    and NOTHING else  -> the real control ("verbatim construction"), 2 auditable lines;
  * `cursor-agent -p` returns only the FINAL assistant message with thinking
    suppressed, captured as typed stream-json  -> a reasoning trace can no longer be
    mistaken for the answer (the 19-cell / 12% loss in run 2);
  * a per-cell validation gate (non-empty final answer, required turns present)
    auto-retries, then writes an explicit CAPTURE_FAILED sentinel -- never a silent
    blank, never reasoning text.

Prompts + the SKILL block are read VERBATIM from ../PROMPTS.md (the single source: the
skill in one fenced block, then the 30 prompts). Only ```-fenced blocks are read, so the
grading notes in that file's prose never reach a model. Transcripts land in ../results/.

Usage:
  cp .env.example .env && edit CURSOR_API_KEY
  python run_experiment.py --list-models          # see exactly what your account can use
  python run_experiment.py --dry-run              # print the built cells, spend nothing
  python run_experiment.py --smoke                # one probe cell: confirm isolation + search
  python run_experiment.py                        # the full sweep (all models x 30 prompts x A/B)
  python run_experiment.py --models opus,kimi --prompts N4,D6   # a subset

Requires: the `cursor-agent` CLI on PATH and a Cursor API key. No Python deps (stdlib only).
"""
from __future__ import annotations
import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent            # experiment/harness/
PROMPTS_FILE = HERE.parent / "PROMPTS.md"         # single source: skill block + 23 prompts
RESULTS = HERE.parent / "results"                 # assembled <model>.md land here (overwritten)
PARTS = RESULTS / ".parts"                         # per-cell answer parts + jsonl sidecars

# ----------------------------------------------------------------------------- config
def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency): KEY=VALUE lines, does not override real env."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)

_load_dotenv(HERE / ".env")

def _env(k, default=None):
    return os.environ.get(k, default)

API_KEY = _env("CURSOR_API_KEY")
CONCURRENCY = int(_env("CONCURRENCY", "6"))
CELL_TIMEOUT = int(_env("CELL_TIMEOUT", "900"))       # seconds, per turn. 420 was too tight:
# in run 5 gpt-5.6's heaviest CAPTURED cell took 409.5s — 10s under the old cap — and the one
# cell that blew through it (030_X7_B) burned 3 x timeout of billed generation for zero data.
CAPTURE_RETRIES = int(_env("CAPTURE_RETRIES", "3"))
# A timeout is NOT an intermittent glitch — it means this model/prompt pair does not fit the
# budget, so retrying it just re-buys the same wall-clock. Capped separately and low.
TIMEOUT_RETRIES = int(_env("TIMEOUT_RETRIES", "1"))
# Hard stop: abort the sweep once this many MILLION tokens have moved (0 = off). Counts all
# four classes incl. cache, which are ~86% of the real volume. Run 5 moved 59.4M in total.
BUDGET_MTOK = float(_env("BUDGET_MTOK", "0"))
SANDBOX = _env("SANDBOX", "enabled")                  # enabled | disabled
AGENT_BIN = _env("CURSOR_AGENT_BIN", "cursor-agent")
USAGE_FILE = None                                     # set in main(): RESULTS/.usage.json

# Model roster: run-5 slugs, verified against `--list-models` 2026-07-20 -- the intent
# is MAX REASONING + NEWEST version per family (gemini/kimi expose a single tier;
# composer's "-fast" sibling is the speed-serving variant, so plain composer-2.5 here).
# Slugs drift between runs; re-check with `--list-models` and override via MODELS in .env.
DEFAULT_MODELS = {
    "fable":    "claude-fable-5-thinking-max",
    "opus":     "claude-opus-4-8-thinking-max",
    "gpt":      "gpt-5.6-sol-max",
    "gemini":   "gemini-3.1-pro",
    "grok":     "cursor-grok-4.5-high",   # Cursor self-hosts Grok; high = top reasoning tier
    "kimi":     "kimi-k2.7-code",
    "composer": "composer-2.5",
    "glm":      "glm-5.2-max",     # Zhipu GLM 5.2, max-reasoning variant (glm-5.2-high also available)
}
MODEL_NAMES = {
    "fable": "Claude Fable 5", "opus": "Claude Opus 4.8", "gpt": "GPT 5.6 Sol",
    "gemini": "Gemini 3.1 Pro", "grok": "Grok 4.5", "kimi": "Kimi K2.7 Code",
    "composer": "Composer 2.5", "glm": "GLM 5.2",
}

def roster() -> dict[str, str]:
    m = dict(DEFAULT_MODELS)
    ov = _env("MODELS")
    if ov:
        for pair in ov.split(","):
            k, _, v = pair.partition("=")
            if k.strip() and v.strip():
                m[k.strip()] = v.strip()
    return m

# Targeted replication (design item 6): the binary signal lands almost entirely on
# these prompts x these models. Default 1 (breadth); set DISCRIMINATOR_REPS=5 for depth.
DISCRIMINATORS = {"D6", "W3", "W4", "N3", "N4"}
MOVERS = {"grok", "kimi", "opus"}
def reps_for(model_key: str, prompt_id: str) -> int:
    k = int(_env("DISCRIMINATOR_REPS", "1"))
    return k if (k > 1 and prompt_id in DISCRIMINATORS and model_key in MOVERS) else 1

EXPECTED_IDS = ([f"D{i}" for i in range(1, 9)] + [f"W{i}" for i in range(1, 7)]
                + ["C1", "C2", "C3"] + [f"N{i}" for i in range(1, 7)]
                + [f"X{i}" for i in range(1, 8)])   # V3 additions (run 5)

def require_agent() -> str:
    """Resolve the cursor-agent binary or exit with install instructions."""
    candidates = [AGENT_BIN, "cursor-agent", "agent",
                  str(Path.home() / ".local/bin/cursor-agent"),
                  str(Path.home() / ".cursor/bin/cursor-agent")]
    for c in candidates:
        found = shutil.which(c) or (c if Path(c).is_file() and os.access(c, os.X_OK) else None)
        if found:
            return found
    sys.exit(
        "ERROR: the Cursor Agent CLI ('cursor-agent') is not installed or not on PATH.\n\n"
        "  Install it:   curl https://cursor.com/install -fsS | bash\n"
        "  Then open a new terminal (or `source ~/.bashrc`) and check:  cursor-agent --version\n\n"
        "If it is installed under a different name/path, set CURSOR_AGENT_BIN in .env.\n"
        "(Nothing was run; no cells were touched.)"
    )

# ----------------------------------------------------------------------------- run state
# Out-of-credit / usage-limit handling. Cursor's Ultra plan caps monthly usage and returns e.g.
# "ActionRequiredError: You've hit your usage limit ... set a Spend Limit ...". The cap is
# account-wide and INTERMITTENT (some concurrent calls slip through), so we do NOT blacklist a
# model on one hit — each cell retries with backoff, keeps whatever succeeds, and a cell that
# stays limited is marked USAGE_LIMIT (no part written -> a later run resumes and refills it).
# _QUOTA_MODELS just records which models were hit, for the end-of-run report.
_QUOTA_MODELS: set[str] = set()
_QUOTA_MSG: str | None = None
_STATE_LOCK = threading.Lock()
QUOTA_RE = re.compile(
    r"action[_ ]?required|hit your (?:usage|monthly|spend) limit|usage limit|spend(?:ing)? limit|"
    r"insufficient (?:credit|fund|balance|quota)|out of (?:credit|token|quota)|"
    r"no (?:credits?|tokens?)\b|payment required|\b402\b|billing (?:issue|error|required)|"
    r"monthly limit|credits? (?:exhausted|used up)|not enough (?:credit|balance)|"
    r"exceeded your (?:usage|monthly|plan)|upgrade your plan|quota (?:exceeded|exhausted)|"
    r"hard limit reached|set a spend limit", re.I)
def is_quota_error(text: str) -> bool:
    return bool(text) and bool(QUOTA_RE.search(text))

# ----------------------------------------------------------------------- token accounting
# Run 5's token line undercounted real volume by 7.4x overall and 96.8x for gpt (it printed
# 209,020 for gpt against 20,229,701 actually moved). Two causes, both fixed here:
#   1. it summed ONLY inputTokens + outputTokens, ignoring cacheReadTokens / cacheWriteTokens
#      — which are 86% of every token that moves;
#   2. the counter was a per-PROCESS global, but the sweep is resumed across many invocations,
#      so the last run printed only its own handful of cells (in run 5's final resume: zero).
# Now: four token classes, per model, accumulated into RESULTS/.usage.json so a resumed sweep
# reports the whole battery. Retried and quota-aborted attempts count too — they were billed.
TOKCLASSES = ("in", "out", "cread", "cwrite")
_TOK_LOCK = threading.Lock()
_TOK: dict[str, dict] = {}
_ABORT = threading.Event()          # set when BUDGET_MTOK is crossed

def _blank() -> dict:
    d = {k: 0 for k in TOKCLASSES}
    d.update(turns=0, calls=0, tools=0, ms=0, reported=False, out_chars=0)
    return d

def _add_usage(mkey: str, pr: dict) -> None:
    """Fold one turn's usage into the per-model ledger. Called for EVERY turn that reached the
    provider, including ones whose transcript we then discard — those were still billed."""
    with _TOK_LOCK:
        d = _TOK.setdefault(mkey, _blank())
        for k, src in zip(TOKCLASSES, ("tin", "tout", "cread", "cwrite")):
            d[k] += pr.get(src, 0)
        d["turns"] += 1
        d["tools"] += len(pr.get("tools", ()))
        d["ms"] += pr.get("ms", 0)
        d["reported"] = d["reported"] or pr.get("reported", False)
        d["out_chars"] += len(pr.get("answer", "") or "")
    if BUDGET_MTOK and total_tokens() / 1e6 > BUDGET_MTOK and not _ABORT.is_set():
        _ABORT.set()
        print(f"\n!! BUDGET_MTOK={BUDGET_MTOK} exceeded — no further cells will be started. "
              f"Already-running cells finish; re-run to resume.\n", file=sys.stderr)

def _load_usage() -> None:
    """Merge the prior invocations' ledger so a resumed sweep reports the whole battery."""
    if USAGE_FILE and USAGE_FILE.exists():
        try:
            for mk, d in json.loads(USAGE_FILE.read_text()).get("models", {}).items():
                cur = _TOK.setdefault(mk, _blank())
                for k, v in d.items():
                    cur[k] = (cur[k] or v) if isinstance(v, bool) else cur.get(k, 0) + v
        except (json.JSONDecodeError, OSError, TypeError):
            pass                                   # a corrupt ledger must never block a run

def _save_usage() -> None:
    if not USAGE_FILE:
        return
    with _TOK_LOCK:
        USAGE_FILE.write_text(json.dumps({"models": _TOK}, indent=2))

def rebuild_usage_from_parts() -> None:
    """Reconstruct the ledger by re-reading every .jsonl sidecar on disk. Used by --usage so a
    battery captured before this ledger existed can still be measured. It is a FLOOR: the
    harness only ever persisted the LAST attempt's stream, so retried and quota-aborted
    attempts left no transcript to count."""
    if not PARTS.is_dir():
        return
    for mdir in sorted(p for p in PARTS.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for f in sorted(mdir.glob("*.jsonl")):
            try:
                rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
            except (json.JSONDecodeError, OSError):
                continue
            for r in rows:
                _add_usage(mdir.name, parse_stream(r.get("stdout", "")))

def model_tokens(d: dict) -> int:
    return sum(d.get(k, 0) for k in TOKCLASSES)

def total_tokens() -> int:
    with _TOK_LOCK:
        return sum(model_tokens(d) for d in _TOK.values())

def print_usage_report(before: int | None = None) -> None:
    """Cumulative across every invocation that wrote RESULTS/.usage.json — the sweep is normally
    resumed, so a per-process count is noise. Convert to money in the Cursor dashboard."""
    with _TOK_LOCK:
        snap = {mk: dict(d) for mk, d in _TOK.items()}
    if not snap:
        print("\nNo usage recorded yet.")
        return
    print(f"\n{'model':10s}{'turns':>6s}{'wall_min':>9s}{'output':>10s}{'cacheR':>12s}"
          f"{'cacheW':>11s}{'input':>10s}{'tools':>7s}{'total_tok':>12s}")
    print("-" * 87)
    agg = _blank()
    for mk in sorted(snap, key=lambda k: -model_tokens(snap[k])):
        d = snap[mk]
        print(f"{mk:10s}{d['turns']:6d}{d['ms']/60000:9.1f}{d['out']:10,d}{d['cread']:12,d}"
              f"{d['cwrite']:11,d}{d['in']:10,d}{d['tools']:7d}{model_tokens(d):12,d}")
        for k in list(TOKCLASSES) + ["turns", "tools", "ms"]:
            agg[k] += d[k]
    print("-" * 87)
    print(f"{'TOTAL':10s}{agg['turns']:6d}{agg['ms']/60000:9.1f}{agg['out']:10,d}"
          f"{agg['cread']:12,d}{agg['cwrite']:11,d}{agg['in']:10,d}{agg['tools']:7d}"
          f"{model_tokens(agg):12,d}")
    if before is not None:
        print(f"\nThis invocation added {model_tokens(agg) - before:,} tokens. "
              f"Ledger: {USAGE_FILE}")
    print("Cache reads + writes are ~86% of the volume — the columns run 5 never counted.")

# ----------------------------------------------------------------------------- battery
def parse_battery() -> tuple[str, list[dict]]:
    """Return (skill_block, prompts) read VERBATIM from PROMPTS.md — the single source.
    Skill = the first ```-fenced block before the first '### ' prompt heading.
    Prompts = the fenced block(s) under each '### <ID> ...' heading (two-turn if 'Turn 1:').
    Only fenced blocks are read, so the grading-note prose in PROMPTS.md never leaves here."""
    text = PROMPTS_FILE.read_text()
    chunks = re.split(r"\n### ", text)                    # chunks[0] = intro + skill section
    m = re.search(r"```[^\n]*\n(.*?)\n```", chunks[0], re.S)
    if not m:
        sys.exit("ERROR: no SKILL fenced block found before the first '### ' prompt in "
                 f"{PROMPTS_FILE}. Add a '## The tell-truth skill' section with the skill in "
                 "a ``` block.")
    skill = m.group(1).strip()

    prompts = []
    for chunk in chunks[1:]:
        header, _, body = chunk.partition("\n")
        pid_m = re.match(r"([A-Za-z]+\d+)", header.strip())
        if not pid_m:
            continue
        blocks = [b.strip() for b in re.findall(r"```[^\n]*\n(.*?)\n```", body, re.S)]
        if not blocks:
            continue
        two = ("Turn 1:" in body) and len(blocks) >= 2
        prompts.append({"id": pid_m.group(1), "turns": blocks[:2] if two else blocks[:1],
                        "two_turn": two})

    ids = [p["id"] for p in prompts]
    if ids != EXPECTED_IDS:
        print(f"WARNING: parsed prompt ids {ids}\n         expected            {EXPECTED_IDS}",
              file=sys.stderr)
    return skill, prompts

# ----------------------------------------------------------------------------- CLI call
def _tool_name(ev: dict) -> str:
    """Extract the real tool name from a stream-json tool event. Cursor's stream-json carries
    it as the '<name>ToolCall' key inside ev['tool_call'] (webSearchToolCall, shellToolCall,
    grepToolCall, ...); other CLI builds may use name/tool fields. Never fall back to the
    whole event blob — payload text (a fetched page, the answer) must not name a tool."""
    tc = ev.get("tool_call")
    if isinstance(tc, dict):
        for k in tc:
            if k.endswith("ToolCall"):
                return k[: -len("ToolCall")]
    for k in ("name", "tool"):
        if isinstance(ev.get(k), str) and ev[k]:
            return ev[k]
    msg = ev.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("name"), str):
        return msg["name"]
    return ""

def _classify_tool(name: str) -> tuple[bool, bool]:
    """Classify a tool NAME as web-search and/or code-exec. Names only, never event/result
    text: a web page mentioning 'bash' or a payload line like 'Size: 101.9 KB, 402 lines'
    must not flip provenance (or, upstream, quota) flags — that bug shelved a healthy cell.
    Web search = external verification (webSearch/webFetch); grep/read/glob are local."""
    n = (name or "").lower()
    search = bool(re.search(r"websearch|web_search|webfetch|web_fetch|read_web|browse", n))
    exc = bool(re.search(r"shell|terminal|run_command|execute|interpreter|bash", n))
    return search, exc

_ISO_LOCK = threading.Lock()
_ISO_HOME: str | None = None

# Written into the isolated HOME so the agent can use the web + shell tools without an
# interactive approval prompt (impossible in headless mode). WebFetch is domain-gated by
# this allowlist; isolating HOME removed the user's ~/.cursor/cli-config.json, so we recreate
# a permissive one here. This grants tools, NOT skills (skills load from skills/ dirs, absent).
_CLI_CONFIG = {
    "version": 1,
    "editor": {"vimMode": False},
    "permissions": {
        "allow": ["WebFetch(*)", "WebSearch(*)", "Shell(*)", "Read(**)", "Write(**)"],
        "deny": [],
    },
}

def _isolated_env() -> dict:
    """Env for each cursor-agent call. With ISOLATE_HOME=1 (default) HOME points at a fresh
    empty dir, so NONE of the global skill/rule locations load -- ~/.cursor, ~/.claude,
    ~/.codex, ~/.agents all live under $HOME. This is the reliable way to run "no skills"
    (deleting the folders doesn't stick: they re-sync, and the CLI reads 8 locations incl.
    ~/.claude/skills where tell-truth also lives). Auth still comes from CURSOR_API_KEY.
    Into that fresh HOME we write a permissive ~/.cursor/cli-config.json so the web-search /
    web-fetch and shell tools auto-approve headlessly (otherwise they block on a prompt).
    The fresh cwd already blocks project-level rules. Set ISOLATE_HOME=0 to disable (e.g. if
    you authenticate via `cursor-agent login` whose session lives under the real ~/.cursor)."""
    env = dict(os.environ)
    if _env("ISOLATE_HOME", "1").lower() not in ("0", "false", "no", ""):
        global _ISO_HOME
        with _ISO_LOCK:
            if _ISO_HOME is None or not Path(_ISO_HOME).is_dir():
                _ISO_HOME = tempfile.mkdtemp(prefix="tt_home_")
                cfg = Path(_ISO_HOME) / ".cursor"
                cfg.mkdir(parents=True, exist_ok=True)
                (cfg / "cli-config.json").write_text(json.dumps(_CLI_CONFIG, indent=2))
        env["HOME"] = _ISO_HOME
    return env

def run_turn(model_slug: str, prompt_text: str, cwd: str, resume: str | None):
    """Invoke cursor-agent for one turn. Returns (raw_stdout, stderr, returncode)."""
    cmd = [AGENT_BIN, "-p", "--output-format", "stream-json",
           "--force", "--trust", "--sandbox", SANDBOX, "--model", model_slug]
    if resume:
        cmd += ["--resume", resume]
    cmd += [prompt_text]                       # positional prompt; API key comes from env
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=CELL_TIMEOUT, env=_isolated_env())
        return proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1

def _pick_int(d: dict, *keys) -> int:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0

def parse_stream(stdout: str) -> dict:
    """Extract the final answer, session id, tool provenance, and token usage from stream-json."""
    answer_parts, result_text, session = [], None, None
    search = exc = res_err = saw_result = False
    tools: list[str] = []
    seen_calls: set = set()          # dedup key: the call_id shared by started/completed
    usage: dict | None = None
    duration_ms = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        session = ev.get("session_id") or ev.get("chatId") or session
        u = ev.get("usage") or (ev.get("message", {}) or {}).get("usage")
        if isinstance(u, dict):
            usage = u                                     # last one wins (cumulative on result)
        if t == "assistant":
            for c in (ev.get("message", {}) or {}).get("content", []) or []:
                if c.get("type", "text") == "text" and c.get("text"):
                    answer_parts.append(c["text"])
        elif "tool" in (t or ""):
            # ONE logical invocation emits TWO events (subtype started + completed) sharing a
            # call_id. Counting both doubled every tool figure in run 5 (gpt read as 1832 calls;
            # the real number is 916). Dedup on call_id, and only when the event actually has
            # one — a few streams emit an orphan 'completed' with no matching 'started'.
            name = _tool_name(ev)
            cid = ev.get("call_id") or ev.get("toolCallId") or (ev.get("tool_call") or {}).get("call_id")
            fresh = True
            if cid is not None:
                fresh = cid not in seen_calls
                seen_calls.add(cid)
            if name and fresh:
                tools.append(name)
            s, e = _classify_tool(name)
            search, exc = search or s, exc or e
        elif t == "result":
            saw_result = True
            result_text = ev.get("result") or result_text
            duration_ms += ev.get("duration_ms") or 0
            res_err = res_err or bool(ev.get("is_error")) or (ev.get("subtype") not in (None, "success"))
    answer = (result_text if (result_text and result_text.strip())
              else "\n".join(answer_parts)).strip()
    g = (lambda *ks: _pick_int(usage, *ks) if usage else 0)
    return {"answer": answer, "session": session, "search": search, "exec": exc,
            "is_error": res_err, "saw_result": saw_result, "tools": tools, "ms": duration_ms,
            "tin": g("input_tokens", "prompt_tokens", "inputTokens"),
            "tout": g("output_tokens", "completion_tokens", "outputTokens"),
            "cread": g("cache_read_input_tokens", "cacheReadTokens", "cache_read_tokens"),
            "cwrite": g("cache_creation_input_tokens", "cacheWriteTokens", "cache_write_tokens"),
            "reported": usage is not None}

# ----------------------------------------------------------------------------- one cell
class Cell:
    def __init__(self, mkey, slug, prompt, cond, rep, seq):
        self.mkey, self.slug, self.prompt = mkey, slug, prompt
        self.cond, self.rep, self.seq = cond, rep, seq
    @property
    def tag(self):
        r = f"_r{self.rep}" if self.rep > 1 or reps_for(self.mkey, self.prompt["id"]) > 1 else ""
        return f"{self.seq:03d}_{self.prompt['id']}_{self.cond}{r}"
    @property
    def marker(self):
        base = f"--- {self.prompt['id']} {'WITH' if self.cond == 'B' else 'WITHOUT'} SKILL"
        return f"{base} — rep {self.rep} ---" if (reps_for(self.mkey, self.prompt['id']) > 1) else f"{base} ---"

def build_prompt(skill: str, turn_text: str, cond: str) -> str:
    """THE construction boundary. Nothing but the prompt (A) or skill+prompt (B) ever leaves."""
    return turn_text if cond == "A" else f"{skill}\n\n{turn_text}"

def _short(text: str, n: int = 200) -> str:
    line = (text or "").strip().splitlines()
    return (line[0][:n] if line else "").strip()

# Statuses whose part-file is a sentinel, not an answer: never assemble, never score, and
# re-run on the next invocation.
FAIL_STATUSES = ("CAPTURE_FAILED", "TIMEOUT")

def _has_ok_part(cell: Cell) -> bool:
    """A cell is 'done' if its part-file exists and is a real answer (not a failure sentinel).
    This is what makes a re-run RESUME — already-captured cells are skipped, never re-burned.
    Prefer meta.json's status over a substring scan of the answer text: the old scan asked
    whether the model happened to type the word 'CAPTURE_FAILED' in its answer."""
    p = PARTS / cell.mkey / f"{cell.tag}.md"
    if not p.exists():
        return False
    meta = PARTS / cell.mkey / f"{cell.tag}.meta.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text()).get("status") == "OK"
        except (json.JSONDecodeError, OSError):
            pass
    return not any(s in p.read_text() for s in FAIL_STATUSES)

def run_cell(skill: str, cell: Cell) -> tuple[str, str]:
    """Returns (status, reason).
    status in {OK, SKIP, USAGE_LIMIT, CAPTURE_FAILED, TIMEOUT, BUDGET_STOP}."""
    if _has_ok_part(cell):
        return "SKIP", ""
    if _ABORT.is_set():
        return "BUDGET_STOP", f"BUDGET_MTOK={BUDGET_MTOK} reached before this cell started"
    last_turns, last_raw, last_err, quota_raw = [], [], "", ""
    search = exc = hit_quota = False
    timeouts = 0
    attempt = 0
    for attempt in range(1, CAPTURE_RETRIES + 1):
        cwd = tempfile.mkdtemp(prefix=f"tt_{cell.mkey}_{cell.prompt['id']}_{cell.cond}_")
        turns_out, raw, session, quota = [], [], None, False
        try:
            for i, tt in enumerate(cell.prompt["turns"]):
                ptext = build_prompt(skill, tt, cell.cond)
                out, err, rc = run_turn(cell.slug, ptext, cwd, resume=session if i else None)
                pr = parse_stream(out)
                # Count usage FIRST, unconditionally. Every branch below can discard this
                # turn's transcript, but the provider already generated it and already billed
                # it. Run 5 counted only the clean path, so the ~3.0M cache-read tokens burned
                # by quota-aborted attempts (preserved in the .QUOTA.txt sentinels) and the
                # retried attempts appeared in no total anywhere.
                _add_usage(cell.mkey, pr)
                # Quota only counts on a FAILED turn (non-zero rc, error result, or no answer):
                # the stream body carries fetched web pages and the answer itself, where
                # QUOTA_RE patterns occur benignly — a page described as "402 lines" once
                # shelved a healthy max-thinking cell as USAGE_LIMIT and burned its retries.
                turn_failed = rc != 0 or pr["is_error"] or not pr["answer"]
                if turn_failed and (is_quota_error(err) or is_quota_error(out)):
                    quota = hit_quota = True
                    quota_raw = (err or out).strip()
                    global _QUOTA_MSG
                    with _STATE_LOCK:
                        _QUOTA_MODELS.add(cell.mkey)
                        if _QUOTA_MSG is None:
                            _QUOTA_MSG = _short(quota_raw, 400)
                    break                              # retry this cell (intermittent), don't skip model
                if err.strip():
                    last_err = _short(err)
                if err.strip() == "TIMEOUT":
                    timeouts += 1
                session = pr["session"] or session
                search, exc = search or pr["search"], exc or pr["exec"]
                turns_out.append(pr)
                raw.append({"turn": i + 1, "rc": rc, "stderr": err[:2000],
                            "prompt": ptext, "stdout": out})
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        if not quota:
            last_turns, last_raw = turns_out, raw
            # Validation gate. Run 5 accepted ANY non-empty text, so a stream killed mid-flight
            # (rc=-7, no 'result' event) had its opening preamble recorded as the model's final
            # answer and scored — 3 cells in run 5, which is a scoring error, not just a lost
            # cell. A real completion means: the process exited 0, the CLI emitted its terminal
            # 'result' event, and there is text.
            complete = (turns_out and len(turns_out) == len(cell.prompt["turns"])
                        and all(t["answer"] and t["saw_result"] for t in turns_out)
                        and all(r["rc"] == 0 for r in raw))
            if complete:
                _write_cell(cell, turns_out, search, exc, raw, "OK", attempt, "")
                return "OK", ""
        # A timeout is a budget verdict, not a glitch: the same model on the same prompt will
        # take the same wall clock next time. Run 5 spent 3 x CELL_TIMEOUT on gpt/030_X7_B for
        # zero captured data. Stop after TIMEOUT_RETRIES and say so.
        if timeouts > TIMEOUT_RETRIES:
            reason = (f"timed out after {timeouts} attempt(s) at CELL_TIMEOUT={CELL_TIMEOUT}s — "
                      f"not retried further (raise CELL_TIMEOUT or drop this model/prompt pair)")
            _write_cell(cell, last_turns, search, exc, last_raw, "TIMEOUT", attempt, reason)
            return "TIMEOUT", reason
        if _ABORT.is_set():
            return "BUDGET_STOP", f"BUDGET_MTOK={BUDGET_MTOK} reached mid-cell"
        if attempt < CAPTURE_RETRIES:
            time.sleep(min(2 ** attempt, 15))  # backoff (rides out an intermittent usage limit)
    if hit_quota:
        d = PARTS / cell.mkey
        d.mkdir(parents=True, exist_ok=True)
        # Cap it. quota_raw falls back to the whole stdout stream when stderr is empty, which
        # in run 5 wrote 1.59 MB "error" files containing entire transcripts.
        (d / f"{cell.tag}.QUOTA.txt").write_text(quota_raw[:8000])
        return "USAGE_LIMIT", (_short(quota_raw, 260) or "usage/rate limit (empty error body)")
    reason = last_err or "empty final answer or truncated stream (no terminal result event)"
    _write_cell(cell, last_turns, search, exc, last_raw, "CAPTURE_FAILED", attempt, reason)
    return "CAPTURE_FAILED", reason

def _write_cell(cell, turns_out, search, exc, raw, status, attempt, reason=""):
    d = PARTS / cell.mkey
    d.mkdir(parents=True, exist_ok=True)
    # answer part (assembled later into <model>.md)
    lines = [cell.marker]
    if status in FAIL_STATUSES:
        # The sentinel must be the ONLY thing in the body. Run 5 wrote the failed cell's
        # partial text here for CAPTURE_FAILED but not consistently, and a truncated preamble
        # that slipped through the gate got scored as a real answer.
        lines.append(f"[{status} after {attempt} attempt(s): {reason}]")
    elif cell.prompt["two_turn"]:
        for i, t in enumerate(turns_out, 1):
            lines += [f"Turn {i}:", t["answer"], ""]
    else:
        lines.append(turns_out[0]["answer"] if turns_out else "")
    lines += [f"SEARCH FIRED: {'yes' if search else 'no'}",
              f"EXEC FIRED: {'yes' if exc else 'no'}", ""]
    (d / f"{cell.tag}.md").write_text("\n".join(lines))
    # raw sidecar (audit: every event, reasoning + tool payloads)
    (d / f"{cell.tag}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in raw))
    # tools come from the already-parsed turns (run 5 re-parsed 62 MB of stdout here purely to
    # rebuild this list, and an older _tool_name made it write ["completed","started"] —
    # the subtype — instead of tool names, in 156 of 240 files).
    usage = {k: sum(t.get(src, 0) for t in turns_out)
             for k, src in zip(TOKCLASSES, ("tin", "tout", "cread", "cwrite"))}
    (d / f"{cell.tag}.meta.json").write_text(json.dumps({
        "status": status, "attempt": attempt, "search": search, "exec": exc,
        "tools": sorted({t for turn in turns_out for t in turn.get("tools", ())}),
        "tool_calls": sum(len(turn.get("tools", ())) for turn in turns_out),
        "duration_ms": sum(turn.get("ms", 0) for turn in turns_out),
        "usage": usage,
    }, indent=2))

# ----------------------------------------------------------------------------- assembly
def assemble(mkey: str, slug: str, gate: str):
    d = PARTS / mkey
    parts = sorted(d.glob("*.md"))
    if not parts:
        return
    header = (f"=== MODEL: {MODEL_NAMES.get(mkey, mkey)} (slug: {slug}) ===\n"
              f"RUN: isolated cursor-agent CLI one-shot per cell (run-3 harness); "
              f"fresh cwd, stream-json capture, verbatim construction\n"
              f"GATE: {gate}\n"
              f"SEARCH AVAILABLE: yes\n\n")
    body = "\n".join(p.read_text().rstrip() + "\n" for p in parts)
    (RESULTS / f"{mkey}.md").write_text(header + body)

# ----------------------------------------------------------------------------- smoke gate
def run_smoke(slug: str, mkey: str = "smoke") -> bool:
    print(f"[smoke] probing isolation + web search with {slug} ...")
    cwd = tempfile.mkdtemp(prefix="tt_smoke_")
    probe = ("List verbatim every custom rule, skill, persona, or system instruction "
             "currently active in your context. If there are none, reply exactly "
             "'NONE ACTIVE'. Then use web search to find today's date and report it. "
             "End your message with the token PROBE_DONE.")
    try:
        out, err, rc = run_turn(slug, probe, cwd, None)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    PARTS.mkdir(parents=True, exist_ok=True)
    (PARTS / "smoke_raw.jsonl").write_text(out or "")
    pr = parse_stream(out)
    _add_usage(mkey, pr)          # the gate runs on every invocation and is billed like any cell
    if (rc != 0 or pr["is_error"] or not pr["answer"]) and (is_quota_error(err) or is_quota_error(out)):
        print("  --- OUT OF CREDITS / QUOTA ---")
        print("  Cursor returned: " + (err or out).strip()[:400].replace("\n", " "))
        print("\n[smoke] FAILED: no credits/quota. Add funds or raise your spending limit in the "
              "Cursor dashboard, then retry.")
        return False
    ans = pr["answer"]
    low = ans.lower()
    contaminated = any(s in low for s in ("tell-truth", "investigator, not oracle",
                                          "epistemic discipline", "victim of propaganda"))
    require_search = _env("REQUIRE_SEARCH", "1").lower() not in ("0", "false", "no", "")
    ok = bool(ans) and not contaminated and (pr["search"] or not require_search)
    print("  final answer captured:", "yes" if ans else "NO (empty)")
    print("  skills/rules leaked  :", "YES -> E1 CONTAMINATION" if contaminated else "no")
    if pr["search"]:
        print("  web search fired     : yes")
    elif not require_search:
        print("  web search fired     : no (allowed via REQUIRE_SEARCH=0 — verify channel OFF)")
    else:
        print("  web search fired     : NO -> E3 no search")
    print("  tools the probe used :", ", ".join(pr["tools"]) or "(none)")
    if ans:
        print("  --- probe said ---\n  " + ans[:600].replace("\n", "\n  "))
    if not ok:
        hints = []
        if contaminated:
            hints.append("Skills still leaking — ensure ISOLATE_HOME=1 (default).")
        if require_search and not pr["search"]:
            hints.append("No web search — the isolated HOME now writes a permissive "
                         "cli-config.json (WebFetch(*)); if search still won't fire, your CLI "
                         "build may lack it. Set REQUIRE_SEARCH=0 to run search-off (valid per "
                         "protocol §4, but record it in the results).")
        print("\n[smoke] FAILED. " + " ".join(hints))
    else:
        print("[smoke] PASS.")
    return ok

# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="tell-truth isolated experiment driver")
    ap.add_argument("--models", help="comma list of model keys to run (default: all)")
    ap.add_argument("--prompts", help="comma list of prompt ids to run (default: all)")
    ap.add_argument("--conditions", default="A,B", help="A, B, or A,B")
    ap.add_argument("--list-models", action="store_true", help="run `cursor-agent --list-models`")
    ap.add_argument("--smoke", action="store_true", help="run only the isolation/search probe")
    ap.add_argument("--skip-smoke", action="store_true", help="skip the pre-run smoke gate")
    ap.add_argument("--fresh", action="store_true",
                    help="redo every cell (default resumes: skip cells already captured OK)")
    ap.add_argument("--yes", action="store_true", help="skip the --fresh confirmation prompt")
    ap.add_argument("--usage", action="store_true",
                    help="print the cumulative token ledger and exit (spends nothing)")
    ap.add_argument("--dry-run", action="store_true", help="print built cells, make no API calls")
    args = ap.parse_args()

    global AGENT_BIN, USAGE_FILE
    if args.list_models:
        AGENT_BIN = require_agent()
        subprocess.run([AGENT_BIN, "--list-models"], env=os.environ)
        return

    if args.usage:
        USAGE_FILE = RESULTS / ".usage.json"
        _load_usage()
        if not _TOK:      # no ledger yet (battery predates it) — recover a floor from the parts
            print("No ledger found; reconstructing from the .jsonl sidecars on disk.\n"
                  "NOTE: this is a FLOOR — only the last attempt of each cell was ever persisted,\n"
                  "so retried and quota-aborted attempts are missing from these numbers.")
            rebuild_usage_from_parts()
        print_usage_report()
        return

    rmap = roster()
    if args.models:
        rmap = {k: v for k, v in rmap.items() if k in args.models.split(",")}
    conds = [c.strip() for c in args.conditions.split(",") if c.strip() in ("A", "B")]

    skill, prompts = parse_battery()
    order = {p["id"]: i + 1 for i, p in enumerate(prompts)}
    want = set(args.prompts.split(",")) if args.prompts else None

    if not args.dry_run and not API_KEY:
        sys.exit("ERROR: CURSOR_API_KEY not set. Copy .env.example to .env and fill it in.")

    probe_key = "composer" if "composer" in rmap else next(iter(rmap))
    if args.smoke:
        AGENT_BIN = require_agent()
        sys.exit(0 if run_smoke(rmap[probe_key], probe_key) else 1)

    cells = [Cell(mk, slug, p, c, r, order[p["id"]])
             for mk, slug in rmap.items()
             for p in prompts if (want is None or p["id"] in want)
             for c in conds
             for r in range(1, reps_for(mk, p["id"]) + 1)]

    if args.dry_run:
        print(f"{len(cells)} cells across {len(rmap)} model(s):\n")
        for c in cells[:4] + (cells[-1:] if len(cells) > 4 else []):
            pt = build_prompt(skill, c.prompt["turns"][0], c.cond)
            print(f"  {c.mkey:9s} {c.tag:16s} two_turn={c.prompt['two_turn']}")
            print(f"    prompt sent -> {pt[:110]!r}{' ...' if len(pt) > 110 else ''}")
        print(f"\n(showing a sample; {len(cells)} total. Verbatim construction: "
              f"A = prompt only, B = SKILL + blank line + prompt, nothing else.)")
        return

    AGENT_BIN = require_agent()
    gate = "smoke skipped (--skip-smoke)"
    if not args.skip_smoke:
        if not run_smoke(rmap[probe_key], probe_key):
            sys.exit("Aborting: smoke gate failed. Fix isolation/search or pass --skip-smoke.")
        gate = "smoke PASS — no skills in context, web search fired"

    RESULTS.mkdir(parents=True, exist_ok=True)
    PARTS.mkdir(parents=True, exist_ok=True)
    USAGE_FILE = RESULTS / ".usage.json"
    _load_usage()                       # carry the ledger across resumed invocations
    tok_before = total_tokens()
    if args.fresh:                      # --fresh: wipe prior parts for these models, redo all
        victims = [mk for mk in rmap if (PARTS / mk).is_dir()]
        n = sum(len(list((PARTS / mk).glob("*.md"))) for mk in victims)
        if n and not args.yes:
            # --fresh re-buys every cell. At run-5 rates that is ~$0.60/cell for gpt, so a
            # reflexive --fresh on the full grid is a ~$100 keystroke. Make it say so.
            est = sum(model_tokens(_TOK.get(mk, _blank())) for mk in victims)
            print(f"--fresh will DELETE {n} captured cell(s) across {', '.join(victims)} and "
                  f"re-run them.\nPrior recorded volume on those models: {est:,} tokens. "
                  f"Re-running spends roughly that again.")
            if input("Type 'fresh' to confirm: ").strip() != "fresh":
                sys.exit("Aborted; nothing deleted.")
        for mk in victims:
            shutil.rmtree(PARTS / mk, ignore_errors=True)

    pending = cells if args.fresh else [c for c in cells if not _has_ok_part(c)]
    already = len(cells) - len(pending)
    if pending:
        print(f"\n{already} of {len(cells)} cells already captured (skipped); running {len(pending)}"
              f" — concurrency={CONCURRENCY}, timeout={CELL_TIMEOUT}s, retries={CAPTURE_RETRIES}\n")
    else:
        print(f"\nAll {len(cells)} cells already captured — nothing to run "
              f"(use --fresh to redo). Re-assembling transcripts.")
    done: dict[str, int] = {}
    failed = []
    lock = threading.Lock()
    t0 = time.time()

    def work(cell: Cell):
        status, reason = run_cell(skill, cell)
        with lock:
            done[status] = done.get(status, 0) + 1
            n = sum(done.values())
            if status not in ("OK", "SKIP"):
                failed.append((f"{cell.mkey}/{cell.tag}", status, reason))
            tail = f"  — {reason}" if (reason and status not in ("OK", "SKIP")) else ""
            print(f"[{n}/{len(pending)}] {cell.mkey}/{cell.tag} -> {status}"
                  f"  ({total_tokens()/1e6:.1f}M tok so far){tail}")
            _save_usage()      # checkpoint: a Ctrl-C must not lose the spend record

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        list(ex.map(work, pending))

    for mk, slug in rmap.items():
        assemble(mk, slug, gate)

    dt = int(time.time() - t0)
    _save_usage()
    captured = sum(1 for c in cells if _has_ok_part(c))
    ok = done.get("OK", 0); ql = done.get("USAGE_LIMIT", 0)
    cf = done.get("CAPTURE_FAILED", 0); to = done.get("TIMEOUT", 0); bs = done.get("BUDGET_STOP", 0)
    print(f"\nDone in {dt // 60}m{dt % 60}s. This run: {ok} ok, {ql} usage-limited, "
          f"{cf} capture-failed, {to} timed-out, {bs} budget-stopped.  "
          f"Captured on disk: {captured}/{len(cells)}.")

    print_usage_report(tok_before)
    print(f"\nPer-model transcripts: {RESULTS}/<model>.md   (parts + .jsonl sidecars under {PARTS}/<model>/)")

    missing = len(cells) - captured
    if _QUOTA_MSG:
        bar = "=" * 70
        print(f"\n{bar}\nUSAGE LIMIT / OUT OF CREDITS hit during the run (account-wide):\n"
              f"  {_QUOTA_MSG}\n\n"
              f"{missing} cell(s) still missing (models hit: {','.join(sorted(_QUOTA_MODELS))}).\n"
              f"Set a Spend Limit in the Cursor dashboard (or wait for the monthly reset), then\n"
              f"just re-run — it RESUMES and fills only the missing cells:\n"
              f"  python3 run_experiment.py\n{bar}")
    elif missing:
        print(f"\n{missing} cell(s) missing/failed — re-run to fill (resumes automatically):")
        for name, st, reason in failed:
            print(f"   {name}  [{st}]  {reason}")
    else:
        print(f"\n✓ Complete: every one of the {len(cells)} cells captured.")
    sys.exit(0 if missing == 0 else 2)


if __name__ == "__main__":
    main()
