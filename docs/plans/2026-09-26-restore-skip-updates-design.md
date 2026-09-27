# Restore without startup update prompts

The user reports that accepting a Codex update during restore interrupts recovery
and loses the requested resume reference. Restore currently launches a normal
`codex resume <exact-id>`, allowing that startup prompt to intercept recovery.

Use Codex's documented `-c check_for_update_on_startup=false` override for each
Codex launch made by `codex-restore`, after exact-ID validation and writer checks.
Keep the original manifest, exact identity metadata, and global user configuration
unchanged. The shared restore path covers registered farms and reboot recovery,
including Codex rows in mixed-provider manifests. Other providers and ordinary
Codex launches retain their existing behavior.

A launch override is scoped to recovery. Changing global configuration would
affect every launch; sending keys to a detected update dialog would depend on UI
text and timing. Neither is needed when Codex exposes a configuration switch.

Verification: first reproduce the missing override in the restore regression
test, then verify exact-ID propagation, unchanged manifests, provider isolation,
and the real tmux save/restore integration. Check the installed Codex separately
with a disposable configuration and a simulated newer-version cache, without
running an update or touching live conversations.

## Verification results

- The launch regression failed before the fix and passed afterward; all 392 unit
  tests passed, including exact metadata, child-to-root repair, provider isolation,
  and unchanged-manifest checks.
- Codex CLI 0.152.1 showed the update dialog with a simulated newer-version cache
  and startup checks enabled. The per-launch false override suppressed that dialog
  and advanced to sign-in in the disposable, unauthenticated configuration.
- Ruff, ShellCheck at the repository's warning threshold, Bash syntax, and isolated
  validation passed. Independent review reported no findings.
- The exact six-conversation tmux integration passed against both the repository
  and refreshed installed helpers, including repeated restore and autosave
  preservation. Previous installed helpers were backed up before replacement.
