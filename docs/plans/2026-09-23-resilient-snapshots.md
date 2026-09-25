# Resilient snapshots implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Save and restore exact conversations without allowing idle or incomplete autosaves to destroy a useful recovery point.

**Architecture:** Keep the TSV interface and existing provider discovery. Add a shared Python manifest helper for strict validation, serialized operations, atomic publication, and immutable snapshot history. Autosave only promotes snapshots retaining every previously saved conversation; manual save explicitly changes the restore point and archives the previous contents.

**Tech Stack:** Bash, Python 3.10+ standard library, tmux, unittest.

## Decisions

- Remove latest/continue fallbacks from save, restore, and reboot, including explicit opt-in. Ambiguity must leave the previous manifest intact.
- Prefer verified lifecycle metadata, then unambiguous process-owned session files. Support executable wrappers and reject multiple unrelated candidate sessions.
- Do not use filesystem recency, working directory, or window title as conversation identity.
- Preserve distinct snapshots beside the manifest in `<manifest>.history/`, deduplicated by content. No automatic history deletion.
- Autosave preserves the default restore point if it would lose any saved conversation, even when row counts match. Archive the new exact candidate for explicit recovery. Empty captures never replace a snapshot.
- Serialize collection/publication and restore for the same manifest. Validate every row before creating or killing any windows; parse empty TSV fields without shifting columns.
- Keep provider writer-lock checks and explicit-ID restoration. Generic shell commands remain generic commands.

## Tasks and verification

- [x] Add failing save tests in `tests/test_add_scripts.py`: removed fallback, ambiguous descriptors, wrapper descendants, failed listing, empty capture, history, unchanged captures, autosave shrink and identity replacement.
- [x] Implement identity fixes in `bin/codex-save`; run focused save and restore tests and inspect failures.
- [x] Add `bin/codex-manifest.py` and focused `tests/test_manifest.py` covering strict manifest validation, history publication, locking, and failed writes. Integrate save/autosave and verify focused tests pass.
- [x] Add failing restore tests for unsafe legacy rows and preflight before force; integrate shared validation and stable manifest input in `bin/codex-restore`.
- [x] Remove fallback from `bin/codex-farm-reboot`, update its tests, and order autosave after autorestore in `bin/codex-add` with corresponding unit assertions.
- [x] Update README and validation expectations. Run Ruff, ShellCheck, Bash syntax, all unit tests, isolated validation, and exact session integration.
- [x] Review the final diff and address actionable defects. Verify installation path before reporting which helpers are active.

The user has authorized implementation; proceed in the shared clean checkout without an additional approval or execution handoff.

## Verification results

- All 351 unit tests passed, including failing-first reproductions for the save, restore, and locking defects.
- Ruff formatting/lint, ShellCheck, Bash syntax, and `git diff --check` passed.
- `validate.sh` and `examples/demo.sh` passed on isolated tmux servers.
- `tests/integration/session_resume_smoke.sh` saved and restored six exact provider conversations, verified repeated restore did not launch duplicates, and proved that shrinking to one idle conversation retained the six-session restore point plus a separate one-session history snapshot.
- Independent review findings were reproduced and fixed: lock release before attach, inherited lock-marker ownership, quoted provider-command validation, and child-to-root duplicate detection before force restore. Follow-up review found no remaining issue in those fixes.
- Installed helpers in `~/bin` were stale and lacked `codex-session-meta.py`. Seven affected helpers/dependencies were refreshed with previous files backed up in `~/.local/state/codexfarm/helper-backups/20260923T235108.570921Z`.
- The installed save command captured the live Codex conversation into a temporary snapshot, which passed exact-ID validation. The removed fallback flag was rejected. Live windows and the default restore point were not changed.

## Integration with current main (2026-09-25)

- Rebased onto `5162285`, preserving the upstream chat backup service, restore memory checks and pacing, and memory labels.
- Added failing-first regressions for primary-provider writer identity taking precedence over incidental child transcript reads, and restore avoiding recursive service starts while retaining autosave registration for newly restored farms.
- All 392 combined unit tests passed. Ruff, ShellCheck, Bash syntax, isolated validation/demo, and the exact six-conversation save/restore integration passed.
- Independent follow-up review verified the integration corrections and reported no remaining findings.
