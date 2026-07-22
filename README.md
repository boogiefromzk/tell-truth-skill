# Tell Truth — an AI skill

An AI tools skill that makes AI when answering to stop being a victim of propaganda but apply at least some critical thinking. It pushes to verify before asserting, refuse false balance between documented findings and interested-party denials, and resist the urge to apologize reflexively when corrected.

## What it does

LLMs way of answering questions:

- Answering present-day questions from training data instead of searching
- Hedging documented findings into "some believe" / "opinions differ"
- Treating an investigation and the implicated party's denial as symmetric perspectives
- Apologizing and capitulating when corrected, even if the correction is wrong
- Softening conclusions until they say nothing

This skill counteracts each of those explicitly. Six core rules:

1. **Verify before you assert** — search for anything that could have changed since training
2. **Don't blur established facts** — name investigations, reports, and findings plainly
3. **Distinguish types of disagreement** — contested assessment ≠ documented fact denied by interested party
4. **On correction: verify first, then respond** — no reflexive apology, no reflexive disagreement
5. **Calibrate uncertainty honestly** — confidence should track evidence in both directions
6. **Directness over comfort** — lead with yes/no, then explain

## How to use it

### Option 1 — Paste into the AI's personal settings (always on)

Copy the content of [`SKILL.md`](./tell-truth/SKILL.md) (everything below the YAML 
frontmatter) and paste it into:

- **Claude.ai** (every plan, including free):
  Click your initials (bottom-left) → **Settings** → **Instructions for Claude** field.
- **ChatGPT**: 
  Click your profile (bottom-left) → **Settings** → **Personalization** → 
  **Custom instructions** (or **Customize ChatGPT**, depending on rollout).
- **Gemini, Grok, and others**: look for "Custom instructions," "System prompt," 
  or "Personalization" in settings. Same idea.

It will load into every new conversation.

### Option 2 — Install as an Agent Skill (on-demand)

Drop the whole `tell-truth/` directory (including directory itself) into the right location for your tool:

| Tool | Location | Notes |
|---|---|---|
| **Claude Code** | `~/.claude/skills/` (personal) or `.claude/skills/` (project) | Auto-detected on session start |
| **Claude.ai / Claude apps / Cowork** | **Customize → Skills** (sidebar) | Upload through the UI; no filesystem |
| **Cursor** (2.4+) | `.cursor/skills/` in project root | Project-scoped (as of 2026). Reload window after adding (`Cmd/Ctrl+Shift+P → Developer: Reload Window`) |
| **OpenAI Codex CLI** | `~/.codex/skills/` or `.codex/skills/` (project); also reads `.agents/skills/` per the open standard | Some early versions required `codex --enable skills`; verify on your release |
| **VS Code + GitHub Copilot agent mode** | `.github/skills/` (project) or `~/.copilot/skills/` (personal) | Requires Copilot agent mode (April 2026+); also accepts `~/.agents/skills/` |
| **Cross-tool repo convention** | `.agents/skills/` | The open-standard portable path; supported by Codex and Copilot |

### Option 3 — Project-scoped instructions (always on, but only inside the project)

Copy the content of [`SKILL.md`](./tell-truth/SKILL.md) (everything below the YAML frontmatter) and paste it into:

- **Claude.ai**: create a Project → project's instructions.
- **ChatGPT**: create a Project (or a Custom GPT) → paste into its instructions.
- **Cursor**: add a file like `tell-truth.mdc` to `.cursor/rules/` with `alwaysApply: true` in the frontmatter.
- **Claude Code**: add the content to a `CLAUDE.md` file at the repo root.
- **OpenAI Codex CLI**: add the content to an `AGENTS.md` file at the repo root (or `~/.codex/AGENTS.md` for global).

### Option 4 — Global instructions for coding tools (always on, every project)

Copy the content of [`SKILL.md`](./tell-truth/SKILL.md) (everything below the YAML 
frontmatter) and paste it into:

- **Claude Code**: `~/.claude/CLAUDE.md`. Loaded automatically at every session 
  start; merges with any project-level `CLAUDE.md`.
- **Cursor**: Cursor Settings → **Rules** → **User Rules**. Plain text field, 
  always applied across all projects. (Note: User Rules are UI-only — there is 
  no filesystem path.)
- **OpenAI Codex CLI**: `~/.codex/AGENTS.md`. Loaded before any project-level 
  `AGENTS.md`.
- **Gemini CLI**: `~/.gemini/GEMINI.md`. Same hierarchical model as Codex.
- **GitHub Copilot** (VS Code agent mode, CLI, cloud agent): github.com → 
  **Settings** → **Copilot** → **Personal instructions**. Applies across all 
  repos where you use Copilot.

These layer with project-level files (Option 3) when both exist — global loads 
first, project-level overrides on conflict.

## How to run experiment

The skill is A/B tested: the same trick questions asked twice per model — once alone, once with the skill in front of them — then graded against a checklist published *before* scoring, so the marking can't drift to flatter the skill. Everything needed to repeat it is in [`experiment/`](experiment/):

| File | What it is |
|---|---|
| **[`PROMPTS.md`](experiment/PROMPTS.md)** | The prompt battery — everyday questions, professional tasks, and controls, each targeting one documented AI failure. Copy-paste to run it by hand. |
| **[`ASSESSMENT.md`](experiment/ASSESSMENT.md)** | The scoring rubric: one checklist per prompt, the automatic-fail tripwires, and the real-world incident each trap comes from. |
| **[`harness/`](experiment/harness/)** | A runner that does the whole sweep — one isolated session per model × prompt × condition, capturing each answer with its transcript. |

Two rules make the results worth anything:

1. **Tool use is read from the transcripts, never from what the answer claims.** "I verified this" proves nothing; the recorded search and code-execution events decide.
2. **The rubric is published before scoring.** Controls are graded for side effects only — the skill changing a poem or a settled fact is a cost, never a win.

Run it:

```bash
python3 experiment/harness/run_experiment.py     # resumes; only missing answers are generated
```

Needs a `CURSOR_API_KEY` in `experiment/harness/.env` (one key bills every model). `--dry-run` and `--usage` spend nothing; `--models` / `--prompts` limit the sweep; `--fresh` re-buys everything, so it asks first.

Results move with every run, so no scores are quoted here. The strongest check isn't anyone's published numbers — it's running the battery on your own models and reporting where the skill fails.

## License

MIT. Use it, fork it, modify it.

## Credits

Thanks to Claude team for making this possible.
