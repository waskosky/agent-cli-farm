# Contributing

Agent CLI Farm is a small tmux-focused utility repo. Keep changes boring,
portable, and directly covered by tests.

## Runtime Baseline

- Bash 3.2 or newer for shell entry points.
- Python 3.10 or newer for Python helpers.
- `tmux` for farm, board, status-window, and restore behavior.
- Provider CLIs such as `codex`, `claude`, or `gemini` are optional for tests.

Runtime scripts must stay compatible with Bash 3.2. Using newer Bash features is
acceptable only in CI-only workflow snippets.

## Required Checks

Run these before publishing changes:

```bash
python3 -m pip install --disable-pip-version-check -r requirements-dev.txt
ruff format .
ruff check .
while IFS= read -r file; do bash -n "$file"; done < <(git grep -l '^#!/usr/bin/env bash')
git grep -l -z '^#!/usr/bin/env bash' | xargs -0 shellcheck --severity=warning
CODEX_ANNOTATOR_AUTOSTART=0 python3 -m unittest discover -s tests -v
VALIDATE_SKIP_TMUX=1 ./validate.sh
./validate.sh
./examples/demo.sh
CODEXFARM_DEEP_HISTORY_BIN=/path/to/tmux-deep-history/bin/tmux-deep-history \
  ./tests/integration/deep_history_smoke.sh
```

If system Python blocks direct pip installs in an externally managed
environment, create a virtualenv outside the repo and run the Ruff commands from
that environment instead.

Validation and demo scripts must isolate `HOME`, XDG config/state directories,
and tmux sockets. They must not read, mutate, or destroy the developer's normal
tmux server or state directories.

## Shell Boundaries

- Direct command-line arguments after `--` are data and must be shell-quoted
  before embedding in a tmux command string.
- `CODEX_ARGS`, `CLAUDE_ARGS`, and `GEMINI_ARGS` are documented trusted shell
  fragments retained for backward compatibility.
- Do not introduce `eval`.
- Do not use process-group-wide cleanup such as `kill 0` in runtime scripts.
- Thin Claude and Gemini wrappers should delegate to the shared Codex core
  behavior through `CODEX_TOOL_NAME`.

## State and Manifests

- Write executable restore data atomically in its destination directory.
- Use owner-only permissions for manifests and registries.
- Treat restore manifests as trusted executable input even when they are mode
  `0600`.
- Preserve exact provider session recovery when available and keep documented
  fallback behavior explicit.

## Parsing and Validation

- Help must work without optional dependencies.
- Invalid options, duplicate operands, malformed manifests, invalid regular
  expressions, non-finite numbers, missing executables, and inaccessible
  directories should fail with concise messages.
- Python configuration helpers should validate real TOML types rather than
  coercing arbitrary strings or booleans.
- Subprocesses should use argument lists whenever possible.

## Documentation Parity

Whenever commands, defaults, paths, trust boundaries, or limits change, update
the README, command help, and detailed docs in the same change.
