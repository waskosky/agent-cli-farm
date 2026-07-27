# Exact Session Identity Hooks Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Capture exact Codex conversation IDs at the tmux pane, save exact provider sessions by default, and restore every manifest row without duplicate-name collapse.

**Architecture:** `codex-add` establishes stable pane identity, a small Codex lifecycle hook records authoritative session metadata, and `codex-save` validates that metadata before falling back to provider process discovery. Save fails closed for recognized agents unless fallback is explicitly requested. Restore treats repeated names by occurrence rather than as a unique key.

**Tech Stack:** Bash, tmux user options and pane environments, Python 3.10 standard library, `unittest`, ShellCheck, Ruff.

---

### Task 1: Specify hook behavior with failing tests

**Files:**
- Create: `tests/test_session_hook.py`
- Create: `bin/codex-session-hook.py`
- Create: `bin/codex-session-hook-install.py`

**Step 1: Write failing hook runtime tests**

Cover managed-pane filtering, malformed JSON, pane and UUID validation, provider
PID metadata, write ordering, and quiet fail-open behavior.

**Step 2: Verify the tests fail for missing executables**

Run: `python3 -m unittest -v tests.test_session_hook`

Expected: FAIL because the hook programs do not exist.

**Step 3: Implement the smallest hook runtime**

Read one JSON object, validate the managed pane context, and use argument-safe
tmux calls to set provider, source, timestamp, PID, then session ID.

**Step 4: Verify runtime tests pass**

Run: `python3 -m unittest -v tests.test_session_hook`

Expected: PASS for runtime cases, with installer cases still pending.

**Step 5: Add failing installer tests**

Cover a new hooks file, preservation of unrelated handlers, replacement of a
previous farm handler, idempotence, `--check`, malformed JSON, and symlink
refusal.

**Step 6: Verify installer tests fail for missing behavior**

Run: `python3 -m unittest -v tests.test_session_hook`

Expected: FAIL in installer cases.

**Step 7: Implement atomic idempotent hook configuration**

Merge `SessionStart` and `UserPromptSubmit` command handlers into `hooks.json`
with mode `0600`, preserving unrelated configuration.

**Step 8: Verify the hook suite passes**

Run: `python3 -m unittest -v tests.test_session_hook`

Expected: PASS.

### Task 2: Establish stable pane identity

**Files:**
- Modify: `tests/test_add_scripts.py`
- Modify: `bin/codex-add`

**Step 1: Add a failing `codex-add` identity test**

Assert that a new pane receives managed/provider/name environment variables and
stable `@codexfarm_name` and `@codexfarm_provider` options.

**Step 2: Verify the test fails**

Run: `python3 -m unittest -v tests.test_add_scripts.AddScriptTests`

Expected: FAIL because `codex-add` does not yet establish pane metadata.

**Step 3: Add pane environment and options**

Pass environment variables to `tmux new-window`, then set the stable pane
options after creation.

**Step 4: Verify add tests pass**

Run: `python3 -m unittest -v tests.test_add_scripts.AddScriptTests`

Expected: PASS.

### Task 3: Make exact save identity the default

**Files:**
- Modify: `tests/test_add_scripts.py`
- Modify: `bin/codex-save`

**Step 1: Add failing save tests**

Cover stable option names, fresh hook metadata, stale-PID metadata, two distinct
sessions for each provider, default refusal when an ID is missing, explicit
`--allow-fallback`, and preservation of an existing manifest on refusal.

**Step 2: Verify the new tests fail for the intended reasons**

Run: `python3 -m unittest -v tests.test_add_scripts.SaveScriptTests`

Expected: FAIL on option lookup and fail-closed expectations.

**Step 3: Implement metadata resolution and safe CLI parsing**

Prefer matching hook metadata, validate UUIDs, fall back to provider process
discovery, preserve generic panes, and propagate `--allow-fallback` through
registered-session saves.

**Step 4: Implement atomic fail-closed save**

Write a temporary manifest, report unresolved provider windows without IDs, and
replace the destination only if every required exact identity was resolved.

**Step 5: Verify save tests pass**

Run: `python3 -m unittest -v tests.test_add_scripts.SaveScriptTests`

Expected: PASS.

### Task 4: Restore duplicate logical names independently

**Files:**
- Modify: `tests/test_add_scripts.py`
- Modify: `bin/codex-restore`

**Step 1: Add failing duplicate restore tests**

Cover two fresh rows with the same name, repeated restore against two existing
occurrences, force replacement, and immediate exact-ID pane metadata.

**Step 2: Verify the tests fail**

Run: `python3 -m unittest -v tests.test_add_scripts.RestoreScriptTests`

Expected: FAIL because restore selects only the first matching name.

**Step 3: Implement occurrence-aware matching**

Resolve the Nth existing window for the Nth manifest row. For force mode, remove
all existing occurrences for each unique manifest name before creation.

**Step 4: Seed restored exact metadata**

After adding a row, set its exact manifest ID and source on the corresponding
new pane without printing it.

**Step 5: Verify restore tests pass**

Run: `python3 -m unittest -v tests.test_add_scripts.RestoreScriptTests`

Expected: PASS.

### Task 5: Install and diagnose the hook

**Files:**
- Modify: `tests/test_setup.py`
- Modify: `tests/test_doctor.py`
- Modify: `setup.sh`
- Modify: `bin/codex-doctor`

**Step 1: Add failing setup and doctor tests**

Assert default hook installation, explicit opt-out, idempotent preservation of
other hooks, and duplicate/fallback manifest warnings.

**Step 2: Verify targeted tests fail**

Run: `python3 -m unittest -v tests.test_setup tests.test_doctor`

Expected: FAIL on new hook and duplicate diagnostics.

**Step 3: Wire setup and diagnostics**

Install the hook by default with a documented opt-out and extend manifest
statistics without exposing session IDs.

**Step 4: Verify targeted tests pass**

Run: `python3 -m unittest -v tests.test_setup tests.test_doctor`

Expected: PASS.

### Task 6: Document and integration-test the workflow

**Files:**
- Modify: `README.md`
- Modify: `tests/integration/session_resume_smoke.sh`
- Modify: `validate.sh`

**Step 1: Expand the isolated resume smoke test**

Create two distinct sessions per supported provider and assert every saved row
contains its own exact resume ID after restore.

**Step 2: Run the integration test before changing fixtures**

Run: `bash tests/integration/session_resume_smoke.sh`

Expected: FAIL on the new duplicate/multi-session expectations.

**Step 3: Update integration fixtures, validation, and README**

Document hook trust, exact-by-default saves, fallback opt-in, stable names, and
duplicate rows. Ensure validation uses an explicit fallback only for its generic
mock agent if needed.

**Step 4: Run focused integration checks**

Run: `bash tests/integration/session_resume_smoke.sh`

Expected: PASS.

### Task 7: Verify, install, and publish

**Files:**
- Verify all modified files
- Update local installed copies through `./setup.sh`

**Step 1: Run static and unit verification**

Run:
- `python3 -m unittest discover -s tests -v`
- `bash -n setup.sh bin/codex-add bin/codex-save bin/codex-restore bin/codex-doctor`
- `shellcheck setup.sh bin/codex-add bin/codex-save bin/codex-restore bin/codex-doctor`
- `ruff check bin tests`
- `VALIDATE_SKIP_TMUX=1 ./validate.sh`

Expected: all checks PASS.

**Step 2: Run live isolated verification**

Run:
- `./validate.sh`
- `bash tests/integration/session_resume_smoke.sh`

Expected: all checks PASS without affecting the user's normal tmux server.

**Step 3: Review the diff and install**

Review `git diff --check`, the complete diff, and test evidence. Run
`./setup.sh`, confirm installed helpers match the branch, and probe the live farm
read-only for stable metadata and distinct IDs.

**Step 4: Commit and push the branch**

Create an intentional commit and push `fix/session-identity-hooks` to origin.

**Step 5: Open, verify, and merge a pull request**

Open a PR against `main`, wait for required checks, address any failures, merge
the PR, update local `main`, and confirm local `main` and `origin/main` point to
the merge result.
