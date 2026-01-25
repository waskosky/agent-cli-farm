# Ready Status Detection Design

## Goal
Improve tmux window readiness detection for Codex and Claude CLIs so READY reflects actual prompt availability (including approval/selection prompts), aligning with the heuristics used in cli-agent-orchestrator. Keep existing RUN/READY/ERR labels and avoid new statuses.

## Approach
Enhance `bin/codex-annotator` to inspect tmux pane output when the active pane command is `codex` or `claude`. Use prompt-aware regexes modeled after cli-agent-orchestrator to decide if the CLI is idle/ready, waiting for user input, or still processing. For non-Codex/Claude panes, retain the current command-based heuristic. Window state is derived from pane states: any RUN wins, otherwise ERR wins, otherwise READY.

## Codex Detection Rules
- READY when an idle prompt appears at the end of output (Codex arrow prompt or `codex>`), or when approval prompts like `Approve/Allow ... y/n` are present.
- RUN when no idle prompt is present and output suggests activity.
- ERR only if output is empty or a clear error marker appears without an idle prompt (e.g., `Error:`, `Traceback`, `panic:`).

## Claude Detection Rules
- READY when the `>` prompt appears at the end of output, or when the selection prompt pattern (arrow + numbered options) is present.
- RUN when the spinner/"esc to interrupt" pattern appears or there is no prompt yet.
- ERR only if output is empty or capture fails.

## Performance & Config
Use `tmux capture-pane` with a bounded line count (default 200) to limit overhead. Make line count configurable via an environment variable (e.g., `CODEX_ANNOTATOR_CAPTURE_LINES`). Keep existing annotator flags and session scoping behavior.

## Testing
- Run `./validate.sh` to ensure syntax and basic flows still pass.
- Spot-check a live Codex and Claude session to confirm READY toggles only when the prompt appears or approval/selection prompts are shown.
