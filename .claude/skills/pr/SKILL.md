---
name: pr
description: Open or rewrite a pull request for the current branch with a description that argues for the change — what it is for, what somebody notices, how it works layer by layer, the decisions and their why, what was measured, what was not verified, and the follow-ups. Use when asked to create a PR, write a PR description, or update one.
---

# Pull request description

A PR body is the case for the change, written for somebody who was not in the
session. It reads top to bottom as prose, not as a changelog. Bullet lists are
for decisions and tests, never for the opening.

Before writing, read `git log main..HEAD` and `git diff main --stat`, and the
tests the branch added: the body must be grounded in what is actually there.

## Shape

Use these headings, in this order. Drop a section only when it would be empty.

### `## What this is for`
Two to four short paragraphs. Open with the cost the user was paying before —
the concrete annoyance, in their terms — then what changes for them now. Never
open with the mechanism.

### `### What somebody notices`
A short list of the visible surface: the command, the key, the setting, the
line that now prints. Name each setting added to `config.SETTINGS`.

### `### Measured` *(when there are numbers)*
A table with real runs: wall time, CPU, whatever the change was about. State
the conditions (input length, cores, model). Follow it with one sentence saying
what the numbers taught — the surprising part, not the expected part.

### `## How it works`
One paragraph on the flow, then a table with one row per layer touched,
following the repo's one-directional rule (`cli`/`tui` → `services` →
`gateways` / `transforms` / `storage`). Each row names the new functions or
classes, not the file's whole diff.

### `### Decisions worth knowing about`
A bold-led list. Each item is one decision and *why* — the invariant, the
tradeoff, the failure mode it prevents. Include what was deliberately left
alone and why (e.g. "`vtn ask` stays a one-shot").

### `## Testing`
How many tests were added and what they exercise, the full-suite count,
`ruff` and `mypy` clean, and any end-to-end run with the real tools.

### `## Not verified here, and worth a first run on a Mac`
Say plainly what this environment could not exercise — the recorder, a real
backend, the TUI on a terminal — and what would break, or merely lag, if the
assumption is wrong.

### `## Follow-ups, on the roadmap rather than here`
What was consciously left out. Each item one line with the reason it can wait.

## Mechanics

- Title: conventional prefix plus a lowercase human sentence, the same rule as
  commits (`feat: a meeting is transcribed while it is still being recorded`).
- End the body with the Claude Code attribution and session link, as the
  harness requires.
- Creating: `gh pr create --base main --title ... --body-file <path>`.
  Rewriting: `gh pr edit <n> --body-file <path>`. Write the body to
  `$CLAUDE_JOB_DIR/tmp` first; a heredoc through `--body` mangles backticks
  inside tables.
