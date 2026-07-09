# Claude Hybrid TTY Looper POC

## Goal

Prove that a future Claude hybrid looper can keep the real Claude Code TTY visible while still collecting enough structured state for loop decisions.

## Signals Covered

- TTY pane state remains the human-visible source of READY/RUN/ERR.
- Claude session JSONL files provide session identity and append-only event advancement.
- The controller can tail session files by byte offset so each loop only considers new events.
- Prompt injection can use tmux buffers and paste commands so prompt text is not shell-quoted into argv.

## Implemented Rollout

Claude now defaults to `interface = "hybrid"` for built-in looper runs. The production path creates a visible Claude tmux pane, pastes prompts through tmux buffers, tails Claude session JSONL files by byte offset, and feeds the same looper `state.json` / `events.jsonl` lifecycle records as the JSON subprocess path. The older stream-json subprocess mode remains available with `--interface json` or `[agents.claude].interface = "json"`.

Codex now has the same hybrid shape: `codex-looper` defaults to a visible `codex --no-alt-screen` tmux pane, prompt delivery goes through tmux buffers, Codex session JSONL files under `CODEX_HOME/sessions` are tailed by offset, and the previous `codex exec --json` path remains available with `--interface json` or `[agents.codex].interface = "json"`.

## Future Priorities

1. Add a live, opt-in smoke script that runs a disposable tmux/Claude hybrid session and verifies two consecutive prompts complete.
2. Add richer diagnostics to `events.jsonl` for hybrid confidence transitions: pane state, session path discovery source, and last event type.
3. Decide whether workspace trust prompts should support a documented manual recovery flow or a deliberately named opt-in automation flag.
4. Evaluate a central dashboard for `.agent-looper/runs/*/state.json` aggregation across VMs.
