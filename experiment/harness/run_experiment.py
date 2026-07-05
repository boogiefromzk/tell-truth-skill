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
skill in one fenced block, then the 23 prompts). Only ```-fenced blocks are read, so the
grading notes in that file's prose never reach a model. Transcripts land in ../results/.

Usage:
  cp .env.example .env && edit CURSOR_API_KEY
  python run_experiment.py --list-models          # see exactly what your account can use
  python run_experiment.py --dry-run              # print the built cells, spend nothing
  python run_experiment.py --smoke                # one probe cell: confirm isolation + search
  python run_experiment.py                        # the full sweep (7 models x 23 prompts x A/B)
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
CELL_TIMEOUT = int(_env("CELL_TIMEOUT", "420"))       # seconds, per turn
CAPTURE_RETRIES = int(_env("CAPTURE_RETRIES", "3"))
SANDBOX = _env("SANDBOX", "enabled")                  # enabled | disabled
AGENT_BIN = _env("CURSOR_AGENT_BIN", "cursor-agent")

# Model roster. These are run-2's max-thinking / most-recent-known slugs -- the intent
# is MAX REASONING + NEWEST version per family. They may have moved on; the real list
# comes from `--list-models`, and you override any of them via the MODELS env var
# (see .env.example). gemini/composer below have no obvious "-thinking-max" suffix in
# run 2 -- check --list-models for a higher-reasoning variant and set it in MODELS.
DEFAULT_MODELS = {
    "fable":    "claude-fable-5-thinking-max",
    "opus":     "claude-opus-4-8-thinking-max",
    "gpt":      "gpt-5.5-extra-high",
    "gemini":   "gemini-3.1-pro",
    "grok":     "grok-4.3",
    "kimi":     "kimi-k2.7-code",
    "composer": "composer-2.5-fast",
}
MODEL_NAMES = {
    "fable": "Claude Fable 5", "opus": "Claude Opus 4.8", "gpt": "GPT 5.5",
    "gemini": "Gemini 3.1 Pro", "grok": "Grok 4.3", "kimi": "Kimi K2.5",
    "composer": "Composer 2.5",
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
                + ["C1", "C2", "C3"] + [f"N{i}" for i in range(1, 7)])

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

# Token accounting (best-effort: from the stream's usage field if the CLI reports it,
# else estimated from output length).
_TOK_LOCK = threading.Lock()
_TOK = {"in": 0, "out": 0, "reported": False, "out_chars": 0}
def _add_tokens(tin: int, tout: int, reported: bool, chars: int) -> None:
    with _TOK_LOCK:
        _TOK["in"] += tin
        _TOK["out"] += tout
        _TOK["reported"] = _TOK["reported"] or reported
        _TOK["out_chars"] += chars

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
def _classify_tool(text: str) -> tuple[bool, bool]:
    """Classify a tool event (name or whole-event JSON blob) as web-search and/or code-exec.
    Web search = external verification (web_search / web_fetch); NOT codebase_search (local).
    Patterns are tool-name-ish to avoid false hits on result text."""
    n = (text or "").lower()
    search = bool(re.search(r"web[_\- ]?search|web[_\- ]?fetch|websearch|webfetch|read_web|browse", n))
    exc = bool(re.search(r"run[_\- ]?terminal|terminal[_\- ]?cmd|run[_\- ]?command|"
                         r"execute[_\- ]?command|\bshell\b|\bbash\b|interpreter", n))
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
    search = exc = False
    tools: list[str] = []
    usage: dict | None = None
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
            # tool name can live in several fields; scan the whole event blob so we don't
            # miss it (Cursor puts subtype=started/completed and the name elsewhere).
            blob = json.dumps(ev).lower()
            name = (ev.get("name") or ev.get("tool")
                    or (ev.get("message", {}) or {}).get("name") or ev.get("subtype") or t)
            tools.append(str(name))
            s, e = _classify_tool(blob)
            search, exc = search or s, exc or e
        elif t == "result":
            result_text = ev.get("result") or result_text
    answer = (result_text if (result_text and result_text.strip())
              else "\n".join(answer_parts)).strip()
    tin = _pick_int(usage, "input_tokens", "prompt_tokens", "inputTokens") if usage else 0
    tout = _pick_int(usage, "output_tokens", "completion_tokens", "outputTokens") if usage else 0
    return {"answer": answer, "session": session, "search": search, "exec": exc,
            "tools": tools, "tin": tin, "tout": tout, "reported": usage is not None}

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

def _has_ok_part(cell: Cell) -> bool:
    """A cell is 'done' if its part-file exists and is a real answer (not CAPTURE_FAILED).
    This is what makes a re-run RESUME — already-captured cells are skipped, never re-burned."""
    p = PARTS / cell.mkey / f"{cell.tag}.md"
    return p.exists() and "CAPTURE_FAILED" not in p.read_text()

def run_cell(skill: str, cell: Cell) -> tuple[str, str]:
    """Returns (status, reason). status in {OK, SKIP, USAGE_LIMIT, CAPTURE_FAILED}."""
    if _has_ok_part(cell):
        return "SKIP", ""
    last_turns, last_raw, last_err, quota_raw = [], [], "", ""
    search = exc = hit_quota = False
    for attempt in range(1, CAPTURE_RETRIES + 1):
        cwd = tempfile.mkdtemp(prefix=f"tt_{cell.mkey}_{cell.prompt['id']}_{cell.cond}_")
        turns_out, raw, session, quota = [], [], None, False
        try:
            for i, tt in enumerate(cell.prompt["turns"]):
                ptext = build_prompt(skill, tt, cell.cond)
                out, err, rc = run_turn(cell.slug, ptext, cwd, resume=session if i else None)
                if is_quota_error(err) or is_quota_error(out):
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
                pr = parse_stream(out)
                session = pr["session"] or session
                search, exc = search or pr["search"], exc or pr["exec"]
                _add_tokens(pr["tin"], pr["tout"], pr["reported"], len(pr["answer"]))
                turns_out.append(pr)
                raw.append({"turn": i + 1, "rc": rc, "stderr": err[:2000],
                            "prompt": ptext, "stdout": out})
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        if not quota:
            last_turns, last_raw = turns_out, raw
            if turns_out and all(t["answer"] for t in turns_out):   # validation gate
                _write_cell(cell, turns_out, search, exc, raw, "OK", attempt, "")
                return "OK", ""
        time.sleep(min(2 ** attempt, 15))      # backoff (rides out an intermittent usage limit)
    if hit_quota:
        d = PARTS / cell.mkey
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cell.tag}.QUOTA.txt").write_text(quota_raw)   # full raw error, for inspection
        return "USAGE_LIMIT", (_short(quota_raw, 260) or "usage/rate limit (empty error body)")
    reason = last_err or "empty final answer (no text captured)"
    _write_cell(cell, last_turns, search, exc, last_raw, "CAPTURE_FAILED", CAPTURE_RETRIES, reason)
    return "CAPTURE_FAILED", reason

def _write_cell(cell, turns_out, search, exc, raw, status, attempt, reason=""):
    d = PARTS / cell.mkey
    d.mkdir(parents=True, exist_ok=True)
    # answer part (assembled later into <model>.md)
    lines = [cell.marker]
    if status == "CAPTURE_FAILED":
        lines.append(f"[CAPTURE_FAILED after {CAPTURE_RETRIES} attempts: {reason}]")
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
    (d / f"{cell.tag}.meta.json").write_text(json.dumps({
        "status": status, "attempt": attempt, "search": search, "exec": exc,
        "tools": sorted({t for r in raw for t in parse_stream(r["stdout"])["tools"]}),
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
def run_smoke(slug: str) -> bool:
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
    if is_quota_error(err) or is_quota_error(out):
        print("  --- OUT OF CREDITS / QUOTA ---")
        print("  Cursor returned: " + (err or out).strip()[:400].replace("\n", " "))
        print("\n[smoke] FAILED: no credits/quota. Add funds or raise your spending limit in the "
              "Cursor dashboard, then retry.")
        return False
    pr = parse_stream(out)
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
    ap.add_argument("--dry-run", action="store_true", help="print built cells, make no API calls")
    args = ap.parse_args()

    global AGENT_BIN
    if args.list_models:
        AGENT_BIN = require_agent()
        subprocess.run([AGENT_BIN, "--list-models"], env=os.environ)
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

    if args.smoke:
        AGENT_BIN = require_agent()
        sys.exit(0 if run_smoke(rmap.get("composer") or next(iter(rmap.values()))) else 1)

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
        probe_slug = rmap.get("composer") or next(iter(rmap.values()))
        if not run_smoke(probe_slug):
            sys.exit("Aborting: smoke gate failed. Fix isolation/search or pass --skip-smoke.")
        gate = "smoke PASS — no skills in context, web search fired"

    RESULTS.mkdir(parents=True, exist_ok=True)
    PARTS.mkdir(parents=True, exist_ok=True)
    if args.fresh:                      # --fresh: wipe prior parts for these models, redo all
        for mk in rmap:
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
            print(f"[{n}/{len(pending)}] {cell.mkey}/{cell.tag} -> {status}{tail}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        list(ex.map(work, pending))

    for mk, slug in rmap.items():
        assemble(mk, slug, gate)

    dt = int(time.time() - t0)
    captured = sum(1 for c in cells if _has_ok_part(c))
    ok = done.get("OK", 0); ql = done.get("USAGE_LIMIT", 0); cf = done.get("CAPTURE_FAILED", 0)
    print(f"\nDone in {dt // 60}m{dt % 60}s. This run: {ok} ok, {ql} usage-limited, "
          f"{cf} capture-failed.  Captured on disk: {captured}/{len(cells)}.")

    # token usage
    if _TOK["reported"]:
        tot = _TOK["in"] + _TOK["out"]
        print(f"Tokens: {_TOK['in']:,} in + {_TOK['out']:,} out = {tot:,} total (reported by the CLI).")
    else:
        est = _TOK["out_chars"] // 4
        print(f"Tokens: the CLI did not report usage; ~{est:,} output tokens estimated from output "
              f"length (rough). Check actual spend in the Cursor dashboard.")
    print(f"Per-model transcripts: {RESULTS}/<model>.md   (parts + .jsonl sidecars under {PARTS}/<model>/)")

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
        print("\n✓ Complete: every one of the 322 cells captured.")
    sys.exit(0 if missing == 0 else 2)


if __name__ == "__main__":
    main()
