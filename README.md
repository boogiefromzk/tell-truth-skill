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

### Option 1: paste into AI's settings

Works on every Claude.ai plan including free and Chat GPT.

1. In Claude.ai, click your initials in the bottom-left corner
2. Go to settings
    * Claude: Go to **Settings** → **Instructions for Claude**
    * Chat GPT: Go to **Settings** → **Personalization** → **Custom instructions**
3. Paste the contents of [`SKILL.md`](./tell-truth/SKILL.md) content section

It will load automatically into every new conversation.

### Option 2: Project Instructions

If you only want this behavior for specific work (e.g. research, journalism, fact-checking), create a Claude Project and paste the skill into the project's instructions instead. It will apply only within that project.

### Option 3: as a Claude Code / Cursor Skill

If you're using Claude Code, Cursor, Cowork, or other tool with skill support, drop the `tell-truth/` directory into your skills folder. The YAML frontmatter handles triggering.

## License

MIT. Use it, fork it, modify it.

## Credits

Thanks to Claude team for making this possible.
