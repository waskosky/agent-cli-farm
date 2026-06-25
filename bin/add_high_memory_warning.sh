#!/usr/bin/env bash
set -euo pipefail

DEFAULT_THRESHOLD_MB=200
PROGRAM_NAME="${CODEX_MEMORYFLAG_PROGRAM_NAME:-$(basename "$0")}"

usage() {
  cat <<EOF
Usage: $PROGRAM_NAME [THRESHOLD_MB]
       $PROGRAM_NAME --threshold THRESHOLD_MB [--dry-run]

Prefix tmux window titles whose pane process trees use at least the threshold RSS.
The default threshold is 200 MiB, which adds a marker like *200+MB**.

Options:
  -t, --threshold VALUE  Threshold in MiB, or an integer with M/MB/G/GB suffix
  -n, --dry-run          Print changes without renaming windows
  -h, --help             Show this help
EOF
}

die() {
  echo "$PROGRAM_NAME: $*" >&2
  exit 1
}

parse_threshold_mb() {
  local value="$1"
  local number

  if [[ "$value" =~ ^([0-9]+)([mM][bB]?)?$ ]]; then
    number="${BASH_REMATCH[1]}"
    [ "$number" -gt 0 ] || die "threshold must be greater than zero"
    printf '%s\n' "$number"
    return
  fi

  if [[ "$value" =~ ^([0-9]+)([gG][bB]?)?$ ]]; then
    number="${BASH_REMATCH[1]}"
    [ "$number" -gt 0 ] || die "threshold must be greater than zero"
    printf '%s\n' "$((number * 1024))"
    return
  fi

  die "invalid threshold '$value'; use an integer MiB value, e.g. 200 or 1G"
}

threshold_arg="$DEFAULT_THRESHOLD_MB"
threshold_set=0
dry_run=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    -n|--dry-run)
      dry_run=1
      shift
      ;;
    -t|--threshold)
      [ "$#" -ge 2 ] || die "$1 requires a value"
      threshold_arg="$2"
      threshold_set=1
      shift 2
      ;;
    --threshold=*)
      threshold_arg="${1#*=}"
      threshold_set=1
      shift
      ;;
    -*)
      die "unknown option '$1'"
      ;;
    *)
      [ "$threshold_set" -eq 0 ] || die "threshold specified more than once"
      threshold_arg="$1"
      threshold_set=1
      shift
      ;;
  esac
done

for required_command in tmux awk ps sed stat id; do
  command -v "$required_command" >/dev/null 2>&1 || die "$required_command is required"
done

threshold_mb="$(parse_threshold_mb "$threshold_arg")"
threshold_kb=$((threshold_mb * 1024))
marker="*${threshold_mb}+MB**"
current_user="$(id -un)"
current_uid="$(id -u)"

run_tmux() {
  local socket="$1"
  shift

  tmux -S "$socket" "$@"
}

socket_uid() {
  if stat -c '%u' "$1" >/dev/null 2>&1; then
    stat -c '%u' "$1"
  elif stat -f '%u' "$1" >/dev/null 2>&1; then
    stat -f '%u' "$1"
  else
    return 1
  fi
}

trim_spaces() {
  sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//'
}

normalize_title() {
  local title="$1"
  local should_mark="$2"
  local status=""
  local current

  title="$(printf '%s' "$title" | sed -E 's/\*[0-9]+(\.[0-9]+)?\+MB\*\*[[:space:]]*//g' | trim_spaces)"

  while [[ "$title" =~ ^\*([Rr][Ee][Aa][Dd][Yy]|[Rr][Uu][Nn]|[Ee][Rr][Rr])\*[[:space:]]*(.*)$ ]]; do
    current="$(printf '%s' "${BASH_REMATCH[1]}" | tr '[:lower:]' '[:upper:]')"
    case "$current" in
      RUN) status="RUN" ;;
      ERR)
        [ "$status" = "RUN" ] || status="ERR"
        ;;
      READY)
        [ -n "$status" ] || status="READY"
        ;;
    esac
    title="${BASH_REMATCH[2]}"
    title="$(printf '%s' "$title" | trim_spaces)"
  done

  if [ -n "$status" ]; then
    title="*$status* $title"
    title="$(printf '%s' "$title" | trim_spaces)"
  fi

  if [ "$should_mark" -eq 1 ]; then
    printf '%s\n' "$(printf '%s %s' "$marker" "$title" | trim_spaces)"
  else
    printf '%s\n' "$title"
  fi
}

rss_for_roots() {
  local roots="$1"

  awk -v roots="$roots" '
    BEGIN {
      n = split(roots, root_list, /[[:space:]]+/)
      for (i = 1; i <= n; i++) {
        if (root_list[i] ~ /^[0-9]+$/) {
          desc[root_list[i]] = 1
        }
      }
    }
    {
      ppid[$1] = $2
      rss[$1] = $3
    }
    END {
      changed = 1
      while (changed) {
        changed = 0
        for (pid in ppid) {
          if (!(pid in desc) && (ppid[pid] in desc)) {
            desc[pid] = 1
            changed = 1
          }
        }
      }
      for (pid in desc) {
        sum += rss[pid]
      }
      print sum + 0
    }
  ' "$ps_file"
}

shopt -s nullglob

sockets=()
add_socket() {
  local socket="$1"
  local existing uid
  [ -S "$socket" ] || return 0
  uid="$(socket_uid "$socket" || true)"
  [ "$uid" = "$current_uid" ] || return 0
  if [ "${#sockets[@]}" -gt 0 ]; then
    for existing in "${sockets[@]}"; do
      [ "$existing" = "$socket" ] && return 0
    done
  fi
  sockets+=("$socket")
}

if [ -n "${TMUX:-}" ]; then
  add_socket "${TMUX%%,*}"
fi

for tmux_dir in "${TMUX_TMPDIR:-/tmp}"/tmux-*; do
  [ -d "$tmux_dir" ] || continue
  for socket in "$tmux_dir"/*; do
    add_socket "$socket"
  done
done

if [ "${#sockets[@]}" -eq 0 ]; then
  echo "No current-user tmux sockets found."
  exit 0
fi

ps_file="$(mktemp)"
trap 'rm -f "$ps_file"' EXIT
ps -eo pid=,ppid=,rss= > "$ps_file"

window_keys=()
window_owner=()
window_socket=()
window_session=()
window_id_by_key=()
window_index=()
window_name=()
window_pids=()
skipped_sockets=0

find_window_slot() {
  local wanted="$1"
  local i=0

  while [ "$i" -lt "${#window_keys[@]}" ]; do
    if [ "${window_keys[$i]}" = "$wanted" ]; then
      printf '%s\n' "$i"
      return 0
    fi
    i=$((i + 1))
  done

  return 1
}

for socket in "${sockets[@]}"; do
  owner="$current_user"
  if ! output="$(run_tmux "$socket" list-panes -a -F '#{session_name}	#{window_id}	#{window_index}	#{window_name}	#{pane_pid}' 2>/dev/null)"; then
    skipped_sockets=$((skipped_sockets + 1))
    continue
  fi

  while IFS=$'\t' read -r session window_id index name pane_pid; do
    [ -n "${window_id:-}" ] || continue
    key="${owner}|${socket}|${window_id}"
    if ! slot="$(find_window_slot "$key")"; then
      slot="${#window_keys[@]}"
      window_keys+=("$key")
      window_owner+=("$owner")
      window_socket+=("$socket")
      window_session+=("$session")
      window_id_by_key+=("$window_id")
      window_index+=("$index")
      window_name+=("$name")
      window_pids+=("")
    fi
    if [[ "${pane_pid:-}" =~ ^[0-9]+$ ]]; then
      window_pids[$slot]="${window_pids[$slot]} $pane_pid"
    fi
  done <<< "$output"
done

marked=0
cleared=0
unchanged=0
rename_failed=0

i=0
while [ "$i" -lt "${#window_keys[@]}" ]; do
  rss_kb="$(rss_for_roots "${window_pids[$i]}")"
  should_mark=0
  if [ "$rss_kb" -ge "$threshold_kb" ]; then
    should_mark=1
  fi

  old_name="${window_name[$i]}"
  new_name="$(normalize_title "$old_name" "$should_mark")"

  if [ "$old_name" = "$new_name" ]; then
    unchanged=$((unchanged + 1))
    i=$((i + 1))
    continue
  fi

  action="CLEAR"
  if [ "$should_mark" -eq 1 ]; then
    action="MARK"
  fi

  printf '%s\t%s\t%s:%s\t%.1f MiB\t%s -> %s\n' \
    "$action" \
    "${window_owner[$i]}" \
    "${window_session[$i]}" \
    "${window_index[$i]}" \
    "$(awk -v kb="$rss_kb" 'BEGIN { printf "%.1f", kb / 1024 }')" \
    "$old_name" \
    "$new_name"

  if [ "$dry_run" -eq 1 ]; then
    i=$((i + 1))
    continue
  fi

  window_target="${window_id_by_key[$i]}"
  run_tmux "${window_socket[$i]}" set-window-option -t "$window_target" automatic-rename off >/dev/null 2>&1 || true
  run_tmux "${window_socket[$i]}" set-window-option -t "$window_target" allow-rename off >/dev/null 2>&1 || true

  if run_tmux "${window_socket[$i]}" rename-window -t "$window_target" "$new_name" >/dev/null 2>&1; then
    if [ "$should_mark" -eq 1 ]; then
      marked=$((marked + 1))
    else
      cleared=$((cleared + 1))
    fi
  else
    rename_failed=$((rename_failed + 1))
  fi
  i=$((i + 1))
done

if [ "$dry_run" -eq 1 ]; then
  echo "Dry run complete: ${#window_keys[@]} windows scanned, threshold ${threshold_mb} MiB, ${skipped_sockets} sockets skipped."
else
  echo "Complete: ${marked} marked/updated, ${cleared} cleared, ${unchanged} unchanged, ${rename_failed} rename failures, ${skipped_sockets} sockets skipped."
fi
