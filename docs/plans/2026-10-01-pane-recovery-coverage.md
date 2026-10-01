# Pane recovery coverage implementation plan

**Goal:** Capture every provider conversation in a farm, detect incomplete coverage and unsafe static shared-server identities, and recognize actual Codex app-server commands without confusing argument values.

**Design:** Keep the four-column manifest. Capture each provider pane using its exact conversation ID and working directory; additional panes restore as individually named windows. Include Codex running in the initial home window under a distinct restore name. Continue omitting plain home shells and mark history-picker utilities explicitly; a picker that becomes a conversation is captured when its authoritative identity is available. Generic first-pane shell/custom entries retain their previous behavior. Never guess IDs or kill running conversations.

**Identity rule:** A recovery/manual binding is a fixed historical assertion, not evidence that a shared-server TUI still displays that conversation. Prefer authoritative process writer evidence, and trust direct provider hook metadata bound to the same live provider PID. Return a distinct unverified-legacy result when only a static binding survives. Failed exact capture preserves the previous manifest, while the existing backup job continues archiving conversation history and reports the manifest fault.

**Audit contract:** `codex-save --inspect-provider PANE` returns the detected provider without requiring a conversation ID. `--inspect-pane PANE` returns the existing provider/UUID tuple only when verified, returns status 4 for a static Codex binding without current authoritative ownership, and returns status 5 only for an explicitly marked local `--no-daemon resume --all` picker with no current conversation or recorded session metadata. The read-only pane audit enumerates all live pane IDs and PIDs, excludes only confirmed local idle history pickers, compares verified provider/UUID pairs with the manifest, and reports unknown IDs, missing entries and legacy bindings without printing IDs. Doctor uses this audit rather than equating a successful job with complete coverage.

## Tasks

- [x] Add failing regressions for home providers, secondary provider panes, stable names, pane-list errors and concurrent pane changes. Capture every provider pane, flattening additional panes into stable restore names, and update restore's presence checks to inspect all panes in every farm.
- [x] Add failing regressions for static recovery metadata after an in-TUI session switch. Refuse unverifiable static identities; keep authoritative hooks, writers, dead restored panes and other providers working. Add migration instructions that require the exact current ID and a normal exit before relaunch with `codex-add`; no automatic interruption.
- [x] Add a read-only pane audit and doctor regressions for omitted home/secondary panes, missing or unknown IDs, static shared-server bindings and complete coverage. Preserve backup health warnings through the existing failed-manifest status.
- [x] Reproduce and fix app-server detection using command position and executable identity, preserving direct TUI hooks whose profile or prompt is named `app-server`. Keep both managed server variants and a server parented by a TUI blocked.
- [x] Run focused and full tests, pinned Ruff, Bash syntax, ShellCheck and isolated lifecycle/resume integration. Review spec compliance and code quality independently.
- [x] Preserve the installed release and current manifest privately. Install the tested changes, verify the history backup still succeeds when legacy pane coverage is flagged, and leave active conversations running. Keep archives local.

Release procedure: publish and merge after repository CI passes, update the clean main checkout and installation source, and report tests plus remaining migration work for already-running shared-server TUIs.

**Constraints:** Integration tests use private tmux sockets and synthetic providers. Do not send prompts to other conversations, perform forced restores, or restart the active Codex app server. Do not invalidate or overwrite existing history archives because current pane identity is unknown.
