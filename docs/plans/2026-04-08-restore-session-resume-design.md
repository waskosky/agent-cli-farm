# Restore Session Resume Design

## Goal
Ensure `codex-restore` recreates each saved window and immediately resumes the last CLI session inside that restored pane, with equivalent behavior for the Claude and Gemini wrappers.

## Approach
Keep the change scoped to `bin/codex-restore`. After each window is created through `codex-add`, send one tool-specific follow-up command into the new tmux pane so the CLI resumes its last conversation state. Tool selection continues to come from the wrapper mechanism via `CODEX_TOOL_NAME`, so `codex-restore`, `claude-restore`, and `gemini-restore` all share the same implementation.

## Resume Commands
- `codex`: `codex resume --last`
- `claude`: `claude --continue`
- `gemini`: `gemini --resume latest`

## Window Handling
- Apply the resume command only to windows that were created during the current restore run.
- If a window already exists and restore skips it because `-f` was not used, do not inject any command into that existing pane.
- If `-f` is used, recreate the window first, then inject the resume command into the replacement window.

## Testing
- Add restore-focused tests that stub tmux and assert `send-keys` is issued after `new-window`.
- Cover all three tool modes by setting `CODEX_TOOL_NAME` in the test environment.
- Run the targeted test file after implementation to confirm the new behavior without regressing existing add/resume coverage.
