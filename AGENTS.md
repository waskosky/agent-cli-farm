# Codex CLI Farm - Agent Status Maintenance

## Source of truth

Status classification lives in code and tests, not in notes copied from another project.

- `bin/codex-annotator.py`
  - `classify_codex_output()`
  - `classify_claude_output()`
  - `classify_pane()`
  - `aggregate_window_state()`
- `tests/test_codex_annotator.py`
- `tests/test_annotator_status.py`

Update those tests whenever prompt, spinner, provider, or tmux-pane behavior changes.

## Classification model

The annotator classifies each pane from a combination of:

- Captured terminal output.
- `pane_current_command`.
- `pane_start_command`.
- Dead-pane state.
- Provider-specific prompt, processing, waiting, and error patterns.

Provider-specific output classification must run before generic running-command
heuristics. This matters for Node-installed CLIs where `pane_current_command` is
often `node`, while `pane_start_command` identifies Codex or Claude.

## UI meaning

- READY means the pane appears available or is waiting for user action.
- RUN means the pane shows an explicit running/processing signal.
- ERR means the pane shows an actionable error signal.

These states are heuristic because Codex, Claude, Gemini, and generic shells do
not expose a structured status channel through tmux.

## Operational rules

- Title rewriting is off by default; native CLI window-title updates should be
  allowed unless the user opts into managed status titles.
- Memory markers must be preserved when status prefixes are added or removed.
- Stale window state must be pruned during annotation passes.
- Invalid user-supplied patterns, templates, or intervals must fail cleanly and
  must not crash a long-running annotator daemon.
