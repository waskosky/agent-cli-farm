# Codex CLI Farm - Agent Status Detection Notes

## Why these rules exist
Our tmux window READY/RUN/ERR detection mirrors the heuristics used by the **CLI Agent Orchestrator (CAO)** project so we can distinguish "running" vs "waiting for user" as reliably as possible from terminal output alone.

Because Codex/Claude CLIs do not emit a structured status channel, we rely on prompt patterns and spinner/selection cues. This document records the CAO source files used so we can re-sync if their logic changes.

## Reference sources (CAO)
We based our rules on these files in `~/repos/cli-agent-orchestrator`:

- **Codex provider**: `src/cli_agent_orchestrator/providers/codex.py`
  - Patterns: `IDLE_PROMPT_AT_END_PATTERN`, `WAITING_PROMPT_PATTERN`, `PROCESSING_PATTERN`, `ERROR_PATTERN`
  - Gating: `USER_PREFIX_PATTERN` + `ASSISTANT_PREFIX_PATTERN` (WAITING/ERROR only count if they appear *after* the last user message and are not part of an assistant response)
  - Status decisions: `get_status()` around lines ~55-120 (see in repo)

- **Claude provider**: `src/cli_agent_orchestrator/providers/claude_code.py`
  - Patterns: `PROCESSING_PATTERN`, `WAITING_USER_ANSWER_PATTERN`, `IDLE_PROMPT_PATTERN`
  - Status decisions: `get_status()` around lines ~84-110 (see in repo)

If CAO updates those patterns or ordering, re-check our implementation and update `bin/codex-annotator` accordingly.

## Our implementation (Codex CLI Farm)
Current rules live here:

- `bin/codex-annotator`
  - `classify_codex_output()`
  - `classify_claude_output()`
  - `classify_pane()` and `aggregate_window_state()`

Key behavior differences vs CAO (intentional):

- **RUN only on explicit processing markers.**
  - We do **not** mark RUN just because there is no idle prompt.
  - This was a deliberate choice to avoid false RUN when the CLI is quiet.

- **WAITING prompts map to READY.**
  - CAO returns `WAITING_USER_ANSWER`, but our UI uses READY to indicate “needs your reply.”

- **Codex waiting/error gating kept.**
  - We only treat WAITING/ERROR prompts as actionable if they appear after the last user message and not inside assistant output (same gating as CAO).

## When to revisit
If Codex/Claude CLI output changes (prompt symbols, spinner glyphs, approval prompt wording), re-open the CAO files above and re-copy the latest patterns and ordering.
