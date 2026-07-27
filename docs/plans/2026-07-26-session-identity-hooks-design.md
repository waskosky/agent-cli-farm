# Exact Session Identity Hooks Design

## Goal

Make every managed tmux pane retain the exact coding-agent conversation it is
running, so `codex-save` and `codex-restore` cannot silently collapse several
windows onto one provider's most recent session.

The implementation covers Codex, Claude, and Gemini manifests. Codex additionally
gets a lifecycle hook because its hook payload exposes the active conversation's
`session_id`.

## Observed Failure Modes

- Older installed helpers can differ from the repository and save blank commands.
- Native CLI window titles can change to generic values such as `node`, making
  different panes indistinguishable by display name.
- A missing provider session ID currently becomes `resume --last`,
  `--continue`, or `--resume latest`; several restored windows can therefore
  resume the same newest conversation.
- Restore treats the first matching display name as the whole window identity,
  so duplicate names are skipped or replaced incorrectly.
- Process-file inspection finds exact IDs when provider internals cooperate, but
  it is an indirect fallback rather than an authoritative session signal.

## Identity Model

Each pane created by `codex-add` receives invisible tmux pane options:

- `@codexfarm_name`: the stable logical farm name supplied to `codex-add`.
- `@codexfarm_provider`: `codex`, `claude`, or `gemini`.
- `@codexfarm_session_id`: the exact provider session ID when known.
- `@codexfarm_session_source`: where that ID came from.
- `@codexfarm_session_seen_at`: hook observation time.
- `@codexfarm_session_pid`: provider process that owned the observed ID.

It also receives environment markers used by hooks:

- `CODEXFARM_MANAGED=1`
- `CODEXFARM_PROVIDER=<provider>`
- `CODEXFARM_WINDOW_NAME=<stable-name>`

The logical name remains independent of native title rewriting, which stays off
by default.

## Codex Hook

`codex-session-hook.py` is registered for Codex `SessionStart` and
`UserPromptSubmit` events. Codex supplies `session_id` in the hook JSON and
passes the pane's inherited `TMUX_PANE` environment variable.

The hook:

1. No-ops unless it is running inside a managed farm pane.
2. validates the pane target and UUID-shaped session ID;
3. identifies the owning Codex process;
4. stores metadata as tmux pane user options, writing the session ID last; and
5. fails open without displaying IDs or disrupting Codex.

Setup merges the hook into `${CODEX_HOME:-$HOME/.codex}/hooks.json`
idempotently and preserves unrelated user hooks. Users may opt out. Codex still
controls hook trust and can require approval through `/hooks`.

Claude and Gemini continue to use provider process/session-file discovery until
they expose an equivalent structured lifecycle signal.

## Save Semantics

`codex-save` prefers valid hook metadata whose recorded provider PID still owns
the pane. If metadata is stale or absent, it falls back to the existing
provider-specific process and file discovery.

For a recognized provider, exact identity is the default safety boundary:

- With an exact ID, save writes the provider's exact resume command.
- Without an exact ID, save fails before replacing the existing manifest.
- `--allow-fallback` explicitly restores the previous latest-session behavior.

Generic shell panes remain saveable without a provider resume ID.

## Restore Semantics

Manifest rows are identities in their own right. Restore tracks the occurrence
number of each logical name, so two rows named `api` create or match two separate
windows. Re-running restore is idempotent by `(logical name, occurrence)`.

Force restore removes every pre-existing occurrence for each affected logical
name before adding all manifest rows. Exact session IDs from manifest commands
are placed into pane metadata immediately; the Codex hook refreshes ownership
metadata after startup.

## Diagnostics and Security

`codex-doctor` reports:

- missing or fallback resume commands;
- duplicate logical names in manifests; and
- stale installed helper copies through the existing source/install comparison.

Session IDs remain in owner-only manifest files and invisible pane options.
Commands and normal hook output never print them. Hook configuration updates use
atomic owner-only writes and reject malformed or symlinked targets.

## Verification

Automated coverage exercises:

- hook parsing, filtering, metadata writes, and idempotent config merging;
- hook-metadata preference and stale-metadata fallback;
- fail-closed save plus explicit fallback opt-in;
- two distinct sessions per provider;
- stable names despite changing native titles;
- duplicate-name restore and force restore;
- doctor diagnostics and setup freshness; and
- an isolated tmux save/restore round trip with exact session commands.

Before publication, reinstall the helpers locally, inspect a real managed farm,
run the full validation suite, push a pull request, merge it, and confirm local
and remote `main` identify the same commit.
