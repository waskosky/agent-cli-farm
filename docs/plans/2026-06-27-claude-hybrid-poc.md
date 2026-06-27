# Claude Hybrid TTY Looper POC

## Goal

Prove that a future Claude hybrid looper can keep the real Claude Code TTY visible while still collecting enough structured state for loop decisions.

## Signals Covered

- TTY pane state remains the human-visible source of READY/RUN/ERR.
- Claude session JSONL files provide session identity and append-only event advancement.
- The controller can tail session files by byte offset so each loop only considers new events.
- Prompt injection can use tmux buffers and paste commands so prompt text is not shell-quoted into argv.

## Current Scope

This is proof-of-concept support code and tests only. It does not add a user-facing `--interface hybrid` mode yet.

## Next Implementation Step

Add a Claude TTY controller that creates or targets a Claude pane, pastes prompts into it, polls pane state plus `assess_claude_hybrid_signals()`, and writes the existing looper `state.json` / `events.jsonl` records from controller decisions.
