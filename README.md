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

Copy the content of `tell-truth/SKILL.md` (everything below the YAML 
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

Copy the content of `tell-truth/SKILL.md` (everything below the YAML frontmatter) and paste it into:

- **Claude.ai**: create a Project → project's instructions.
- **ChatGPT**: create a Project (or a Custom GPT) → paste into its instructions.
- **Cursor**: add a file like `tell-truth.mdc` to `.cursor/rules/` with `alwaysApply: true` in the frontmatter.
- **Claude Code**: add the content to a `CLAUDE.md` file at the repo root.
- **OpenAI Codex CLI**: add the content to an `AGENTS.md` file at the repo root (or `~/.codex/AGENTS.md` for global).

## License

MIT. Use it, fork it, modify it.

## Credits

Thanks to Claude team for making this possible.
