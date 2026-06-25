# Quality Review

This document records the safety and correctness issues addressed by the
developer update.

## Corrected Defects

- Direct provider arguments are treated as data, while environment `*_ARGS`
  values remain an explicit trusted shell-fragment boundary.
- State files and executable restore manifests are written atomically in their
  destination directories with owner-only permissions.
- Validation, demo, and unit tests isolate `HOME`, XDG directories, and tmux
  sockets from the developer's normal environment.
- Runtime commands reject invalid options, duplicate operands, malformed
  manifests, invalid regular expressions, non-finite numbers, missing
  executables, and inaccessible directories with concise messages.
- Annotator classification accounts for both current and start commands so
  Node-launched providers are not misclassified as generic running panes.
- Looper process output keeps the current chunked reader, oversized JSON Lines
  handling, reader-failure handling, and split-pane transcript routing.

## Completed Architectural Follow-Ups

- The looper implementation is split into focused package modules for command
  construction, CLI parsing, farm launch, first-run initialization, run-loop
  orchestration, config, prompts, retries, git safety, process execution, and
  tmux integration. The executable remains a compatibility facade.

## Documentation Parity

Public docs and help text should describe:

- Bash/Python/platform requirements.
- Installation behavior and setup limits.
- Agent launch trust boundaries.
- Logging and watch limitations.
- Save/restore persistence, scope, and trust model.
- Heuristic status classification and memory-marker side effects.
- Looper prompt-mode inference, strict config validation, backup limits, and
  split-pane transcript behavior.

## Remaining Priorities

- Reduce duplicated Bash command surfaces.
- Design a versioned structured manifest format.
- Add tmux integration coverage across Linux and macOS.
- Add optional per-worktree/config looper run locks.
