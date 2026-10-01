# Codex shared-server recovery implementation plan

**Goal:** Restore reliable exact-session saves and restores on the installed host, including current Codex versions that default to a shared app server.

**Design:** Retain the existing exact-ID manifest, snapshot history, independent transcript backups, empty-field parser, and systemd health checks already on main. Prevent the shared daemon's inherited pane environment from changing another TUI's identity. Farm-managed interactive Codex commands use `--no-daemon` when supported; older versions retain their supported invocation. Do not add flags to custom commands, other providers, or remote/noninteractive invocations. Current running conversations remain available while verified metadata is repaired and services are upgraded.

**Tech stack:** Bash helpers, Python standard library, unittest, tmux, systemd user services.

## 1. Prevent shared-server hooks from corrupting pane identity

- [x] Add a regression in `tests/test_session_hook.py` launching a fake Codex provider with `app-server --managed-daemon` arguments. A valid hook payload must produce no tmux mutations. Existing direct provider hook tests must continue to record the session.
- [x] Run `python3 -m unittest discover -s tests -p test_session_hook.py`; confirm the new case fails on the current hook.
- [x] In `bin/codex-session-hook.py`, stop ancestor discovery at a Codex app-server boundary:

  ```python
  if provider == "codex" and "app-server" in tokens:
      return None
  ```

- [x] Re-run the hook tests and review the change against the reproduction: no shared-daemon PID may be recorded as the owning TUI PID.

## 2. Keep farm-launched Codex identities local to their TUI

- [x] Add launcher tests for a CLI that advertises `--no-daemon`, an older CLI without it, another provider, and remote/noninteractive commands. Assert the actual tmux launch command, preserving quoted arguments and exact resume UUIDs.
- [x] Run the new tests and observe the missing flag fail.
- [x] Update `bin/codex-add` to probe the selected executable's help without starting an agent turn, and include the supported global flag in interactive launches. Preserve commands whose operation is remote or noninteractive. Handle help failures without changing an older CLI's command.
- [x] Extend `bin/codex-recover-startup.py` and `tests/test_startup_recovery.py` to recognize the known launch grammar with the optional flag and preserve it on an exact SQLite startup retry. Arbitrary shell text remains rejected.
- [x] Run launcher, startup recovery, hook, and session metadata tests.
- [x] Document shared-server ownership limitations and the supported farm launch behavior in `README.md`.

## 3. Verify and install the complete recovery release

- [x] Run the full unit suite, Ruff, Bash syntax, ShellCheck, isolated validation, and exact-session resume integration. Review the patch for spec compliance, then correctness and maintainability.
- [x] Preserve the existing installed helpers, service units, manifest, and pane identity map in a private recovery directory.
- [x] Install the tested helpers. Restore verified existing pane identity using the live TUI PID rather than the shared daemon PID. Confirm a save publishes six unique original conversation IDs.
- [x] Refresh the autosave/autorestore units with the existing installer, including the NVM CLI path. Confirm restore is idempotent for the already-open conversations.
- [x] Run the user autosave service and verify both exact manifest capture and a completed transcript/database backup. Verify timer activity, service success, and backup health.
- Commit and publish the reviewed fix on a branch with a pull request; retain the tested checkout for installed-source freshness checks. Report the PR, checks, installation result, and any remaining limitation for pre-existing shared-server TUIs.

## Verification constraints

Never infer a conversation from the newest transcript, title, cwd, or the first lock held by a shared process. Do not restart the active recovery TUI or send prompts to restored conversations. Integration tests use private tmux sockets and synthetic providers. Current old shared-server TUIs can retain verified PID-bound metadata during this turn, but automatic detection after an in-TUI conversation switch requires relaunching through the corrected farm launcher.

## Verification evidence

439 unit tests passed. Pinned Ruff 0.12.2 formatting/lint, Bash syntax, ShellCheck, isolated validation/demo, and six-conversation resume integration passed. Independent review found no remaining issues. The affected host now saves six verified restored conversations; automatic restore and the autosave timer succeed. The first 1.8 GiB private archive passed SHA-256, inventory, and manifest checks, with 1,196 Codex rollouts and four SQLite snapshots. All 63 installed helper files match the tested source, and the doctor reports healthy.
