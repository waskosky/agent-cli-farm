#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp_root="$(mktemp -d)"

cleanup() {
    set +e
    if [ -n "${main_session:-}" ]; then
        tmux kill-session -t "$main_session" >/dev/null 2>&1 || true
    fi
    if [ -n "${board_session:-}" ]; then
        tmux kill-session -t "$board_session" >/dev/null 2>&1 || true
    fi
    if [ -n "${TMUX_TMPDIR:-}" ] && [ -d "$TMUX_TMPDIR" ] && command -v tmux >/dev/null 2>&1; then
        tmux kill-server >/dev/null 2>&1 || true
    fi
    rm -rf "$tmp_root"
}
trap cleanup EXIT HUP INT TERM

home_dir="$tmp_root/home"
config_dir="$tmp_root/config"
state_dir="$tmp_root/state"
private_bin="$tmp_root/bin"
projects_dir="$tmp_root/projects"
tmux_tmp="$tmp_root/tmux"
mkdir -p "$home_dir" "$config_dir" "$state_dir" "$private_bin" "$projects_dir" "$tmux_tmp"

export HOME="$home_dir"
export XDG_CONFIG_HOME="$config_dir"
export XDG_STATE_HOME="$state_dir"
export TMUX_TMPDIR="$tmux_tmp"
export PATH="$repo_root/bin:$repo_root/examples:$private_bin:$PATH"
export PYTHONDONTWRITEBYTECODE=1
export CODEX_TIPS_PROMPT=0
export CODEX_AUTOSERVICE_CHOICE=no
export CODEX_ANNOTATOR_AUTOSTART=0
unset TMUX

main_session="codexfarm-validate-$$"
board_session="${main_session}-board"
state_basename="codexfarm-validate-$$"

echo "=== Codex CLI Farm Validation ==="
echo "repo: $repo_root"
echo "temp: $tmp_root"
echo ""

check_script_syntax() {
    local script shebang
    echo "Checking script syntax..."
    while IFS= read -r script; do
        [ -f "$script" ] || continue
        shebang="$(head -n 1 "$script" || true)"
        case "$shebang" in
            *"env bash"*|*"bash"*)
                bash -n "$script"
                echo "[OK] $script bash syntax"
                ;;
            *"python"*)
                python3 -m py_compile "$script"
                echo "[OK] $script python syntax"
                ;;
        esac
    done < <(
        cd "$repo_root"
        find bin examples -type f -print
        printf '%s\n' setup.sh validate.sh
    )
}

require_output() {
    local description="$1"
    local pattern="$2"
    shift 2
    local output
    output="$("$@" 2>&1)"
    if grep -q "$pattern" <<< "$output"; then
        echo "[OK] $description"
    else
        echo "[FAIL] $description" >&2
        printf '%s\n' "$output" >&2
        return 1
    fi
}

require_failure_output() {
    local description="$1"
    local pattern="$2"
    shift 2
    local output status
    set +e
    output="$("$@" 2>&1)"
    status=$?
    set -e
    if [ "$status" -ne 0 ] && grep -q "$pattern" <<< "$output"; then
        echo "[OK] $description"
    else
        echo "[FAIL] $description" >&2
        printf '%s\n' "$output" >&2
        return 1
    fi
}

check_static_behavior() {
    local looper_tmp logdir
    echo ""
    echo "Checking help and argument validation..."
    require_output "codex-add help" "Usage:" "$repo_root/bin/codex-add" --help
    require_output "codex-board help" "Usage:" "$repo_root/bin/codex-board" --help
    require_output "codex-farm-reboot help" "Usage:" "$repo_root/bin/codex-farm-reboot" --help
    require_output "codex-status help" "Usage:" "$repo_root/bin/codex-status" --help
    require_output "codex-looper help" "Tiny coding-agent looper" "$repo_root/bin/codex-looper" --help
    require_output "codex-watch help" "Usage:" "$repo_root/bin/codex-watch" --help

    require_failure_output "codex-board rejects invalid option" "Unknown option" \
        "$repo_root/bin/codex-board" --bogus
    require_failure_output "codex-status rejects invalid option" "Unknown option" \
        "$repo_root/bin/codex-status" --bogus
    require_failure_output "codex-watch rejects invalid mode" "Invalid mode" \
        "$repo_root/bin/codex-watch" --mode invalid

    echo ""
    echo "Checking looper first-run setup..."
    looper_tmp="$projects_dir/looper-first-run"
    mkdir -p "$looper_tmp"
    if (cd "$looper_tmp" && "$repo_root/bin/codex-looper" 2>&1 | grep -q "Initialized Agent Looper"); then
        echo "[OK] codex-looper first-run initializes starter files"
    else
        echo "[FAIL] codex-looper first-run setup failed" >&2
        return 1
    fi

    echo ""
    echo "Checking empty log handling..."
    logdir="$XDG_STATE_HOME/$state_basename/logs"
    rm -rf "${XDG_STATE_HOME:?}/${state_basename:?}"
    CODEX_STATE_BASENAME="$state_basename" "$repo_root/bin/codex-watch" 2>&1 | grep -q "No logs yet"
    [ -d "$logdir" ]
    echo "[OK] codex-watch handles empty log directory"
}

run_live_tmux_checks() {
    local mock_agent project manifest mode
    if ! command -v tmux >/dev/null 2>&1; then
        echo "tmux not found; live validation cannot run" >&2
        exit 127
    fi

    echo ""
    echo "Running isolated tmux lifecycle checks..."
    mock_agent="$private_bin/mock-agent"
    cat > "$mock_agent" <<'EOF'
#!/usr/bin/env bash
trap 'exit 0' TERM INT
while :; do sleep 1; done
EOF
    chmod +x "$mock_agent"

    project="$projects_dir/live-project"
    mkdir -p "$project"
    printf '# validate\n' > "$project/README.md"

    CODEX_SESSION="$main_session" \
      CODEX_STATE_BASENAME="$state_basename" \
      CODEX_NAME="validate-test" \
      CODEX_CMD="$mock_agent" \
      CODEX_ARGS="" \
      "$repo_root/bin/codex-add" -d "$project" >/dev/null

    tmux has-session -t "$main_session"
    tmux list-windows -t "$main_session" -F '#{window_name}' | grep -q "validate-test"
    CODEX_SESSION="$main_session" "$repo_root/bin/codex-status" sessions >/dev/null
    echo "[OK] farm session, window, and status listing"

    CODEX_SESSION="$main_session" "$repo_root/bin/codex-board" create >/dev/null
    CODEX_SESSION="$main_session" "$repo_root/bin/codex-board" link >/dev/null
    tmux list-windows -t "$board_session" -F '#{window_name}' | grep -q "validate-test"
    echo "[OK] board creation and linking"

    manifest="$config_dir/codexfarm/manifests/${main_session}.tsv"
    CODEX_SESSION="$main_session" "$repo_root/bin/codex-save" "$manifest" >/dev/null
    head -n 1 "$manifest" | grep -qx $'name\tdir\tcmd\targs'
    mode="$(stat -c '%a' "$manifest" 2>/dev/null || stat -f '%Lp' "$manifest")"
    [ "$mode" = "600" ]
    echo "[OK] manifest header and owner-only mode"

    CODEX_SESSION="$main_session" "$repo_root/bin/codex-farm-reboot" --detach >/dev/null
    tmux has-session -t "$main_session"
    tmux list-windows -t "$main_session" -F '#{window_name}' | grep -q "validate-test"
    tmux has-session -t "$board_session"
    tmux list-windows -t "$board_session" -F '#{window_name}' | grep -q "validate-test"
    echo "[OK] farm reboot rebuilds windows and relinks board"
}

check_script_syntax
check_static_behavior

if [ "${VALIDATE_SKIP_TMUX:-0}" = "1" ]; then
    echo ""
    echo "Skipping live tmux checks because VALIDATE_SKIP_TMUX=1"
else
    run_live_tmux_checks
fi

echo ""
echo "=== Validation Complete ==="
