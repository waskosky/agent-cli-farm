#!/usr/bin/env bash
set -euo pipefail

# Adopt all running Codex-like processes into tmux, non-attaching.
# Caution: review the process list before adopting.

mapfile -t lines < <(pgrep -fa "\bcodex\b|\bcursor\b" || true)

if [ ${#lines[@]} -eq 0 ]; then
  echo "No matching processes found (codex/cursor)."
  exit 0
fi

printf "Found %d process(es):\n" "${#lines[@]}"
printf '  %s\n' "${lines[@]}"

for l in "${lines[@]}"; do
  pid=${l%% *}
  echo "Adopting PID $pid ..."
  codex-adopt -d "$pid" || echo "Failed to adopt $pid" >&2
done

echo "Adoption attempt complete. Attach with: tmux attach -t ${CODEX_SESSION:-codexfarm}"

