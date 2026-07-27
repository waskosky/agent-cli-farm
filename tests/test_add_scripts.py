import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class AddScriptsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="add-script-test-"))
        self.tmux_log = self.tmpdir / "tmux.log"

        tmux_stub = """#!/usr/bin/env bash
set -euo pipefail
log="${TMUX_LOG}"
echo "$*" >> "$log"
case "$1" in
  has-session)
    # trigger new-session path
    exit 1
    ;;
  new-window)
    echo "1"
    exit 0
    ;;
  list-panes)
    if [[ -n "${TMUX_LIST_PANES_OUTPUT:-}" ]]; then
      printf '%b\n' "$TMUX_LIST_PANES_OUTPUT"
    fi
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""
        make_executable(self.tmpdir / "tmux", tmux_stub)
        make_executable(self.tmpdir / "codex", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.tmpdir / "claude", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.tmpdir / "gemini", "#!/usr/bin/env bash\nexit 0\n")

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.tmpdir}:{self.env.get('PATH', '')}"
        self.env["TMUX_LOG"] = str(self.tmux_log)
        self.env.pop("TMUX", None)
        self.env["XDG_STATE_HOME"] = str(self.tmpdir / "state")
        self.env["XDG_CONFIG_HOME"] = str(self.tmpdir / "config")
        self.env["HOME"] = str(self.tmpdir)

    def read_tmux_commands(self) -> list[list[str]]:
        lines = self.tmux_log.read_text(encoding="utf-8").splitlines()
        return [line.split() for line in lines]

    def test_help_does_not_require_tmux_or_agent(self):
        env = self.env.copy()
        sparse_path = self.tmpdir / "sparse-path"
        sparse_path.mkdir()
        for command in ["bash", "basename", "cat", "tr"]:
            system_path = shutil.which(command)
            if not system_path:
                raise RuntimeError(f"Missing required test command: {command}")
            (sparse_path / command).symlink_to(system_path)
        env["PATH"] = str(sparse_path)

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "--help"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)
        normalized_stdout = " ".join(result.stdout.split())
        self.assertIn("CODEX_ARGS, CLAUDE_ARGS, and GEMINI_ARGS", normalized_stdout)
        self.assertNotIn("not found", result.stderr)

    def test_claude_add_passthrough_and_shorthand(self):
        target_dir = self.tmpdir / "proj"
        target_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            [REPO_ROOT / "bin" / "claude-add", "-d", "-dsp", str(target_dir)],
            check=True,
            env=self.env,
        )

        commands = self.read_tmux_commands()
        new_window = [cmd for cmd in commands if cmd and cmd[0] == "new-window"]
        self.assertEqual(len(new_window), 1, f"Unexpected tmux commands: {commands}")
        self.assertIn("-t", new_window[0])
        self.assertIn("codexfarm", new_window[0], "Session should default to codexfarm")
        self.assertIn(
            "claude --dangerously-skip-permissions",
            " ".join(new_window[0]),
            "claude command should receive expanded -dsp flag",
        )

    def test_codex_add_passes_unknown_flags_to_tool(self):
        target_dir = self.tmpdir / "proj2"
        target_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", "--some-flag", str(target_dir)],
            check=True,
            env=self.env,
        )

        commands = self.read_tmux_commands()
        new_window = [cmd for cmd in commands if cmd and cmd[0] == "new-window"]
        self.assertEqual(len(new_window), 1)
        self.assertIn("codex --some-flag", " ".join(new_window[0]))

    def test_codex_add_allows_native_title_updates_by_default(self):
        target_dir = self.tmpdir / "proj-native-title"
        target_dir.mkdir(parents=True, exist_ok=True)
        env = self.env.copy()
        env["CODEX_ANNOTATOR_AUTOSTART"] = "0"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            check=True,
            env=env,
        )

        commands = self.read_tmux_commands()
        title_lock_commands = [
            cmd
            for cmd in commands
            if cmd
            and cmd[0] in {"set-option", "set-window-option", "rename-window"}
            and any(value in cmd for value in ["allow-rename", "automatic-rename"])
        ]
        self.assertEqual(title_lock_commands, [], f"Unexpected title locks: {commands}")

    def test_codex_add_sets_stable_managed_pane_identity(self):
        target_dir = self.tmpdir / "proj-stable-identity"
        target_dir.mkdir(parents=True, exist_ok=True)
        env = self.env.copy()
        env["CODEX_ANNOTATOR_AUTOSTART"] = "0"
        env["CODEX_NAME"] = "stable-api"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.read_tmux_commands()
        new_window = next(command for command in commands if command[0] == "new-window")
        self.assertIn("CODEXFARM_MANAGED=1", new_window)
        self.assertIn("CODEXFARM_PROVIDER=codex", new_window)
        self.assertIn("CODEXFARM_WINDOW_NAME=stable-api", new_window)
        self.assertIn(
            [
                "set-option",
                "-p",
                "-t",
                "codexfarm:1.0",
                "@codexfarm_name",
                "stable-api",
            ],
            commands,
        )
        self.assertIn(
            [
                "set-option",
                "-p",
                "-t",
                "codexfarm:1.0",
                "@codexfarm_provider",
                "codex",
            ],
            commands,
        )

    def test_codex_add_does_not_label_a_custom_command_as_a_provider(self):
        target_dir = self.tmpdir / "proj-custom-command"
        target_dir.mkdir(parents=True, exist_ok=True)
        make_executable(self.tmpdir / "custom-agent", "#!/usr/bin/env bash\nexit 0\n")
        env = self.env.copy()
        env["CODEX_ANNOTATOR_AUTOSTART"] = "0"
        env["CODEX_CMD"] = "custom-agent"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.read_tmux_commands()
        provider_options = [
            command
            for command in commands
            if command and command[0] == "set-option" and "@codexfarm_provider" in command
        ]
        self.assertEqual(provider_options, [])

    def test_codex_add_hook_environment_follows_an_explicit_provider_command(self):
        target_dir = self.tmpdir / "proj-explicit-claude"
        target_dir.mkdir(parents=True, exist_ok=True)
        env = self.env.copy()
        env["CODEX_ANNOTATOR_AUTOSTART"] = "0"
        env["CODEX_CMD"] = "claude"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.read_tmux_commands()
        new_window = next(command for command in commands if command[0] == "new-window")
        self.assertIn("CODEXFARM_PROVIDER=claude", new_window)
        self.assertIn(
            [
                "set-option",
                "-p",
                "-t",
                "codexfarm:1.0",
                "@codexfarm_provider",
                "claude",
            ],
            commands,
        )

    def test_codex_add_uses_unique_logs_for_same_name_and_second(self):
        target_dir = self.tmpdir / "same-name"
        target_dir.mkdir(parents=True, exist_ok=True)
        make_executable(
            self.tmpdir / "date",
            "#!/usr/bin/env bash\nprintf '20260715-120000\\n'\n",
        )
        env = self.env.copy()
        env["CODEX_ANNOTATOR_AUTOSTART"] = "0"

        for _ in range(2):
            subprocess.run(
                [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
                check=True,
                env=env,
            )

        log_files = list((Path(env["XDG_STATE_HOME"]) / "codexfarm" / "logs").glob("*.log"))
        self.assertEqual(len(log_files), 2)
        self.assertEqual(len({path.name for path in log_files}), 2)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in log_files))

    def test_codex_add_keeps_window_after_command_exit_by_default(self):
        target_dir = self.tmpdir / "proj-remain"
        target_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            check=True,
            env=self.env,
        )

        commands = self.read_tmux_commands()
        remain_commands = [
            cmd
            for cmd in commands
            if cmd and cmd[0] == "set-window-option" and "remain-on-exit" in cmd
        ]
        self.assertEqual(
            remain_commands,
            [["set-window-option", "-t", "codexfarm:1", "remain-on-exit", "on"]],
            f"Expected remain-on-exit to be enabled for new windows: {commands}",
        )

    def test_codex_add_can_disable_remain_on_exit(self):
        target_dir = self.tmpdir / "proj-close-on-exit"
        target_dir.mkdir(parents=True, exist_ok=True)
        env = self.env.copy()
        env["CODEX_REMAIN_ON_EXIT"] = "0"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            check=True,
            env=env,
        )

        commands = self.read_tmux_commands()
        joined_commands = [" ".join(cmd) for cmd in commands]
        self.assertFalse(
            any("remain-on-exit" in command for command in joined_commands),
            f"remain-on-exit should be disabled: {commands}",
        )

    def test_codex_add_survives_log_pipe_race_after_fast_exit(self):
        target_dir = self.tmpdir / "proj-fast-exit"
        target_dir.mkdir(parents=True, exist_ok=True)

        tmux_stub = """#!/usr/bin/env bash
set -euo pipefail
log="${TMUX_LOG}"
echo "$*" >> "$log"
case "$1" in
  has-session)
    exit 1
    ;;
  new-window)
    echo "1"
    exit 0
    ;;
  pipe-pane)
    echo "target pane has exited" >&2
    exit 1
    ;;
  *)
    exit 0
    ;;
esac
"""
        make_executable(self.tmpdir / "tmux", tmux_stub)

        env = self.env.copy()
        env["CODEX_ANNOTATOR_AUTOSTART"] = "0"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Started codex in codexfarm:1", result.stdout)
        self.assertIn("warning: unable to attach log pipe", result.stderr)

    def test_codex_add_auto_backend_reports_missing_deep_history(self):
        target_dir = self.tmpdir / "proj-auto-history-fallback"
        target_dir.mkdir(parents=True, exist_ok=True)
        env = self.env.copy()
        env["CODEX_ANNOTATOR_AUTOSTART"] = "0"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tmux-deep-history is not installed", result.stderr)
        self.assertIn("setup.sh --with-deep-history", result.stderr)
        self.assertIn("History backend: legacy", result.stdout)

    def test_codex_add_auto_uses_deep_history_as_single_pipe_owner(self):
        target_dir = self.tmpdir / "proj-deep-history"
        target_dir.mkdir(parents=True, exist_ok=True)
        deep_history_log = self.tmpdir / "deep-history.log"
        deep_history = self.tmpdir / "tmux-deep-history"
        make_executable(
            deep_history,
            "#!/usr/bin/env bash\n"
            f'printf \'python=%s command=%s\\n\' "${{TMUX_DEEP_HISTORY_PYTHON:-}}" "$*" >> {deep_history_log}\n'
            "exit 0\n",
        )
        env = self.env.copy()
        env["CODEXFARM_DEEP_HISTORY_BIN"] = str(deep_history)
        env["CODEXFARM_DEEP_HISTORY_PYTHON_BIN"] = sys.executable
        env["TMUX_LIST_PANES_OUTPUT"] = "0:"
        env["CODEX_ANNOTATOR_AUTOSTART"] = "0"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        tmux_commands = self.read_tmux_commands()
        self.assertFalse(any(command[0] == "pipe-pane" for command in tmux_commands))
        self.assertTrue(
            any("@deep-history-auto-start off" in " ".join(command) for command in tmux_commands)
        )
        self.assertTrue(
            any(
                "@deep-history-seamless-pageup on" in " ".join(command) for command in tmux_commands
            )
        )
        deep_commands = []
        for _ in range(100):
            deep_commands = deep_history_log.read_text(encoding="utf-8").splitlines()
            if len(deep_commands) >= 3:
                break
            time.sleep(0.01)
        self.assertEqual(deep_commands[0], f"python={sys.executable} command=install")
        self.assertIn(
            f"python={sys.executable} command=start -t codexfarm:1 --mirror-output",
            deep_commands[1],
        )
        self.assertEqual(deep_commands[2], f"python={sys.executable} command=start-all")
        self.assertTrue(
            any("@deep-history-auto-start on" in " ".join(command) for command in tmux_commands)
        )
        self.assertTrue(
            any(
                command[:4] == ["set-environment", "-g", "TMUX_DEEP_HISTORY_PYTHON", sys.executable]
                for command in tmux_commands
            )
        )
        self.assertTrue(
            any(
                command[:5]
                == [
                    "set-environment",
                    "-t",
                    "codexfarm",
                    "TMUX_DEEP_HISTORY_PYTHON",
                    sys.executable,
                ]
                for command in tmux_commands
            )
        )
        new_window = next(command for command in tmux_commands if command[0] == "new-window")
        self.assertIn("-e", new_window)
        self.assertIn(f"TMUX_DEEP_HISTORY_PYTHON={sys.executable}", new_window)
        self.assertIn("History backend: deep-history", result.stdout)
        log_files = list((Path(env["XDG_STATE_HOME"]) / "codexfarm" / "logs").glob("*.log"))
        self.assertEqual(len(log_files), 1)
        self.assertEqual(log_files[0].stat().st_mode & 0o777, 0o600)

    def test_codex_add_falls_back_when_deep_history_start_fails(self):
        target_dir = self.tmpdir / "proj-deep-history-fallback"
        target_dir.mkdir(parents=True, exist_ok=True)
        deep_history = self.tmpdir / "tmux-deep-history-failure"
        make_executable(
            deep_history,
            "#!/usr/bin/env bash\n"
            "if [ \"${1:-}\" = start ]; then echo 'simulated start failure' >&2; exit 1; fi\n"
            "exit 0\n",
        )
        env = self.env.copy()
        env["CODEXFARM_HISTORY_BACKEND"] = "deep-history"
        env["CODEXFARM_DEEP_HISTORY_BIN"] = str(deep_history)
        env["CODEX_ANNOTATOR_AUTOSTART"] = "0"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(command[0] == "pipe-pane" for command in self.read_tmux_commands()))
        self.assertIn("falling back to legacy pane logging", result.stderr)
        self.assertIn("simulated start failure", result.stderr)
        self.assertIn("History backend: legacy", result.stdout)
        self.assertFalse(
            any(
                "@deep-history-auto-start on" in " ".join(command)
                for command in self.read_tmux_commands()
            )
        )

    def test_codex_add_rejects_invalid_history_backend(self):
        target_dir = self.tmpdir / "proj-invalid-history"
        target_dir.mkdir(parents=True, exist_ok=True)
        env = self.env.copy()
        env["CODEXFARM_HISTORY_BACKEND"] = "invalid"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid CODEXFARM_HISTORY_BACKEND", result.stderr)

    def test_codex_add_legacy_backend_disables_deep_history_hooks(self):
        target_dir = self.tmpdir / "proj-legacy-history"
        target_dir.mkdir(parents=True, exist_ok=True)
        env = self.env.copy()
        env["CODEXFARM_HISTORY_BACKEND"] = "legacy"
        env["CODEX_ANNOTATOR_AUTOSTART"] = "0"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("History backend: legacy", result.stdout)
        self.assertTrue(
            any(
                "@deep-history-auto-start off" in " ".join(command)
                for command in self.read_tmux_commands()
            )
        )

    def test_codex_add_rejects_invalid_deep_history_pageup_setting(self):
        target_dir = self.tmpdir / "proj-invalid-history-pageup"
        target_dir.mkdir(parents=True, exist_ok=True)
        env = self.env.copy()
        env["CODEXFARM_DEEP_HISTORY_SEAMLESS_PAGEUP"] = "sometimes"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid CODEXFARM_DEEP_HISTORY_SEAMLESS_PAGEUP", result.stderr)

    def test_gemini_add_uses_named_session(self):
        target_dir = self.tmpdir / "proj-gemini"
        target_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            [REPO_ROOT / "bin" / "gemini-add", "work", str(target_dir)],
            check=True,
            env=self.env,
        )

        commands = self.read_tmux_commands()
        new_window = [cmd for cmd in commands if cmd and cmd[0] == "new-window"]
        self.assertEqual(len(new_window), 1, f"Unexpected tmux commands: {commands}")
        self.assertIn("work", new_window[0], "Named farm should be used as session")
        self.assertIn(
            "gemini",
            " ".join(new_window[0]),
            "gemini wrapper should launch the gemini tool",
        )
        registry = Path(self.env["XDG_STATE_HOME"]) / "codexfarm" / "managed_sessions"
        self.assertEqual(registry.read_text(encoding="utf-8").splitlines(), ["work"])

    def test_codex_add_single_named_session_uses_current_directory(self):
        target_dir = self.tmpdir / "proj-current"
        target_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "work"],
            check=True,
            env=self.env,
            cwd=target_dir,
        )

        commands = self.read_tmux_commands()
        new_window = [cmd for cmd in commands if cmd and cmd[0] == "new-window"]
        self.assertEqual(len(new_window), 1, f"Unexpected tmux commands: {commands}")
        command = " ".join(new_window[0])
        self.assertIn("-t work", command)
        self.assertIn(f"-c {target_dir.resolve()}", command)

    def test_autoservice_choice_persist_no(self):
        target_dir = self.tmpdir / "proj3"
        target_dir.mkdir(parents=True, exist_ok=True)

        env = self.env.copy()
        env["CODEX_AUTOSERVICE_CHOICE"] = "no"
        env["SYSTEMCTL_BIN"] = str(self.tmpdir / "systemctl")
        make_executable(self.tmpdir / "systemctl", "#!/usr/bin/env bash\nexit 0\n")

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            check=True,
            env=env,
        )

        choice_file = Path(env["XDG_STATE_HOME"]) / "codexfarm" / "autoservice_choice"
        self.assertTrue(choice_file.exists())
        self.assertEqual(choice_file.read_text().strip(), "no")

    def test_autoservice_install_creates_units(self):
        target_dir = self.tmpdir / "proj4"
        target_dir.mkdir(parents=True, exist_ok=True)

        systemctl_log = self.tmpdir / "systemctl.log"
        systemctl_stub = f"""#!/usr/bin/env bash
echo "$*" >> "{systemctl_log}"
exit 0
"""
        make_executable(self.tmpdir / "systemctl", systemctl_stub)

        env = self.env.copy()
        env["CODEX_AUTOSERVICE_CHOICE"] = "yes"
        env["SYSTEMCTL_BIN"] = str(self.tmpdir / "systemctl")

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-add", "-d", str(target_dir)],
            check=True,
            env=env,
        )

        systemd_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
        autosave = systemd_dir / "codex-autosave.service"
        autosave_timer = systemd_dir / "codex-autosave.timer"
        autorestore = systemd_dir / "codex-autorestore.service"
        self.assertTrue(autosave.exists(), "autosave unit missing")
        self.assertTrue(autosave_timer.exists(), "autosave timer missing")
        self.assertTrue(autorestore.exists(), "autorestore unit missing")
        autosave_content = autosave.read_text()
        autosave_timer_content = autosave_timer.read_text()
        autorestore_content = autorestore.read_text()
        self.assertIn("codex-save", autosave_content)
        self.assertIn("--all-registered", autosave_content)
        self.assertIn("ExecStart=", autosave_content)
        self.assertIn("OnCalendar=hourly", autosave_timer_content)
        self.assertIn("Unit=codex-autosave.service", autosave_timer_content)
        self.assertIn("codex-restore", autorestore_content)
        self.assertIn("--all-registered", autorestore_content)

        choice_file = Path(env["XDG_STATE_HOME"]) / "codexfarm" / "autoservice_choice"
        self.assertEqual(choice_file.read_text().strip(), "yes")

        systemctl_calls = systemctl_log.read_text().splitlines()
        self.assertTrue(any("--user" in line for line in systemctl_calls))
        self.assertTrue(
            any("codex-autosave.timer" in line for line in systemctl_calls),
            f"Expected timer-related systemctl calls, got: {systemctl_calls}",
        )

    def test_autoservice_install_registers_multiple_farms_with_single_units(self):
        systemctl_log = self.tmpdir / "systemctl-session.log"
        systemctl_stub = f"""#!/usr/bin/env bash
echo "$*" >> "{systemctl_log}"
exit 0
"""
        make_executable(self.tmpdir / "systemctl", systemctl_stub)

        env = self.env.copy()
        env["SYSTEMCTL_BIN"] = str(self.tmpdir / "systemctl")

        subprocess.run(
            [
                REPO_ROOT / "bin" / "codex-add",
                "--install-autoservice",
                "--session",
                "work",
            ],
            check=True,
            env=env,
        )
        subprocess.run(
            [
                REPO_ROOT / "bin" / "codex-add",
                "--install-autoservice",
                "--session",
                "personal",
            ],
            check=True,
            env=env,
        )

        systemd_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
        autosave_content = (systemd_dir / "codex-autosave.service").read_text()
        autorestore_content = (systemd_dir / "codex-autorestore.service").read_text()
        self.assertNotIn("Environment=CODEX_SESSION=", autosave_content)
        self.assertNotIn("Environment=CODEX_SESSION=", autorestore_content)
        self.assertIn("ExecStart=", autosave_content)
        self.assertIn("--all-registered", autosave_content)
        self.assertIn("--all-registered", autorestore_content)

        unit_names = sorted(path.name for path in systemd_dir.glob("codex-auto*"))
        self.assertEqual(
            unit_names,
            [
                "codex-autorestore.service",
                "codex-autosave.service",
                "codex-autosave.timer",
            ],
        )

        registry = Path(env["XDG_CONFIG_HOME"]) / "codexfarm" / "farms.tsv"
        rows = registry.read_text(encoding="utf-8").splitlines()
        config_dir = Path(env["XDG_CONFIG_HOME"]) / "codexfarm"
        self.assertEqual(
            rows,
            [
                "session\tmanifest",
                f"work\t{config_dir / 'manifests' / 'work.tsv'}",
                f"personal\t{config_dir / 'manifests' / 'personal.tsv'}",
            ],
        )

    def test_codex_memoryflag_handles_first_tmux_socket_under_nounset(self):
        env = self.env.copy()
        env["TMUX_TMPDIR"] = str(self.tmpdir / "tmux-tmp")
        env.pop("TMUX", None)
        socket_dir = Path(env["TMUX_TMPDIR"]) / "tmux-501"
        socket_dir.mkdir(parents=True)
        socket_path = socket_dir / "default"

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(str(socket_path))

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-memoryflag", "--dry-run"],
            check=True,
            env=env,
            text=True,
            capture_output=True,
        )

        self.assertEqual(
            result.stdout,
            "Dry run complete: 0 windows scanned, threshold 200 MiB, 0 sockets skipped.\n",
        )

    def test_codex_watch_rejects_invalid_mode_without_creating_state(self):
        state_home = Path(self.env["XDG_STATE_HOME"])

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-watch", "--mode", "sideways"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid mode: sideways", result.stderr)
        self.assertFalse((state_home / "codexfarm").exists())


class SaveScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="save-script-test-"))
        self.manifest = self.tmpdir / "manifest.tsv"
        self.hook_session_id = "123e4567-e89b-42d3-a456-426614174000"
        self.second_codex_session_id = "123e4567-e89b-42d3-a456-426614174001"
        self.second_claude_session_id = "123e4567-e89b-42d3-a456-426614174002"
        self.second_gemini_session_id = "123e4567-e89b-42d3-a456-426614174003"

        codex_session_path = (
            "/home/test/.codex/sessions/2026/05/11/"
            "rollout-2026-05-11T03-23-27-019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4.jsonl"
        )
        self.codex_session_path = codex_session_path
        self.custom_codex_session_path = codex_session_path.replace(
            "/home/test/.codex", "/srv/custom-codex-home"
        )
        second_codex_session_path = (
            "/home/test/.codex/sessions/2026/05/12/"
            "rollout-2026-05-12T04-24-28-"
            f"{self.second_codex_session_id}.jsonl"
        )
        claude_session_path = (
            "/home/test/.claude/projects/-tmp-project/54f5b65c-a31c-4aa1-b91b-896b35e2a759.jsonl"
        )
        second_claude_session_path = (
            f"/home/test/.claude/projects/-tmp-project/{self.second_claude_session_id}.jsonl"
        )
        gemini_session_path = (
            self.tmpdir
            / ".gemini"
            / "tmp"
            / "project-hash"
            / "chats"
            / "session-2026-05-11T03-23-27bd36d0.jsonl"
        )
        gemini_session_path.parent.mkdir(parents=True)
        gemini_session_path.write_text(
            '{"sessionId":"27bd36d0-2977-4cce-9d5d-33764d915f1d","projectHash":"project-hash"}\n',
            encoding="utf-8",
        )
        second_gemini_session_path = (
            self.tmpdir
            / ".gemini"
            / "tmp"
            / "project-hash"
            / "chats"
            / "session-2026-05-12T04-24-28second.jsonl"
        )
        second_gemini_session_path.write_text(
            (f'{{"sessionId":"{self.second_gemini_session_id}","projectHash":"project-hash"}}}}\n'),
            encoding="utf-8",
        )
        tmux_stub = """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  has-session)
    exit 0
    ;;
  list-windows)
    if [ "${MULTI_SESSION:-0}" = "1" ]; then
      printf '0\\n1\\n2\\n3\\n4\\n5\\n6\\n'
    else
      printf '0\\n1\\n2\\n3\\n'
    fi
    exit 0
    ;;
  show-options)
    target=""
    option=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -t) target="$2"; shift 2 ;;
        -p|-v) shift ;;
        *) option="$1"; shift ;;
      esac
    done
    case "$target|$option" in
      *:1.0'|@codexfarm_name')
        [ -n "${STABLE_CODEX_NAME:-}" ] && printf '%s\\n' "$STABLE_CODEX_NAME"
        ;;
      *:1.0'|@codexfarm_provider')
        [ -n "${PANE_PROVIDER:-}" ] && printf '%s\\n' "$PANE_PROVIDER"
        ;;
      *:1.0'|@codexfarm_session_id')
        [ -n "${HOOK_SESSION_ID:-}" ] && printf '%s\\n' "$HOOK_SESSION_ID"
        ;;
      *:1.0'|@codexfarm_session_pid')
        [ -n "${HOOK_SESSION_PID:-}" ] && printf '%s\\n' "$HOOK_SESSION_PID"
        ;;
      *) exit 1 ;;
    esac
    exit 0
    ;;
  display-message)
    target=""
    format=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -t) target="$2"; shift 2 ;;
        -p) shift ;;
        *) format="$1"; shift ;;
      esac
    done
    case "$target|$format" in
      *:0'|#{window_name}') printf '*READY* home\\n' ;;
      *:1'|#{window_name}')
        if [ -n "${STACKED_WINDOW_NAME:-}" ]; then
          printf '%s\\n' "$STACKED_WINDOW_NAME"
        else
          printf '*RUN* proj\\n'
        fi
        ;;
      *:1.0'|#{pane_current_path}') printf '/tmp/project\\n' ;;
      *:1.0'|#{pane_start_command}')
        if [ "${METADATA_ONLY:-0}" = "1" ] || [ "${SHELL_STARTED_CODEX:-0}" = "1" ]; then
          printf '\\n'
        elif [ "${WRAPPED_CODEX_START:-0}" = "1" ]; then
          printf 'tmux set-window-option -q remain-on-exit on >/dev/null 2>&1 || true; exec codex\\n'
        elif [ "${QUOTED_CODEX_START:-0}" = "1" ]; then
          printf '"codex "\\n'
        else
          printf 'codex\\n'
        fi
        ;;
      *:1.0'|#{pane_current_command}') printf 'node\\n' ;;
      *:1.0'|#{pane_pid}') printf '100\\n' ;;
      *:2'|#{window_name}') printf 'claude-proj\\n' ;;
      *:2.0'|#{pane_current_path}') printf '/tmp/claude-project\\n' ;;
      *:2.0'|#{pane_start_command}') printf 'claude\\n' ;;
      *:2.0'|#{pane_current_command}') printf 'node\\n' ;;
      *:2.0'|#{pane_pid}') printf '200\\n' ;;
      *:3'|#{window_name}') printf 'gemini-proj\\n' ;;
      *:3.0'|#{pane_current_path}') printf '/tmp/gemini-project\\n' ;;
      *:3.0'|#{pane_start_command}') printf 'gemini\\n' ;;
      *:3.0'|#{pane_current_command}') printf 'node\\n' ;;
      *:3.0'|#{pane_pid}') printf '300\\n' ;;
      *:4'|#{window_name}') printf 'codex-second\\n' ;;
      *:4.0'|#{pane_current_path}') printf '/tmp/codex-second\\n' ;;
      *:4.0'|#{pane_start_command}') printf 'codex\\n' ;;
      *:4.0'|#{pane_current_command}') printf 'node\\n' ;;
      *:4.0'|#{pane_pid}') printf '400\\n' ;;
      *:5'|#{window_name}') printf 'claude-second\\n' ;;
      *:5.0'|#{pane_current_path}') printf '/tmp/claude-second\\n' ;;
      *:5.0'|#{pane_start_command}') printf 'claude\\n' ;;
      *:5.0'|#{pane_current_command}') printf 'node\\n' ;;
      *:5.0'|#{pane_pid}') printf '500\\n' ;;
      *:6'|#{window_name}') printf 'gemini-second\\n' ;;
      *:6.0'|#{pane_current_path}') printf '/tmp/gemini-second\\n' ;;
      *:6.0'|#{pane_start_command}') printf 'gemini\\n' ;;
      *:6.0'|#{pane_current_command}') printf 'node\\n' ;;
      *:6.0'|#{pane_pid}') printf '600\\n' ;;
      *'.0|#{pane_dead}') printf '0\\n' ;;
      *) exit 1 ;;
    esac
    exit 0
    ;;
  *)
    exit 1
    ;;
esac
"""
        pgrep_stub = """#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "-P" ] && [ "$2" = "100" ]; then
  printf '101\\n'
elif [ "$1" = "-P" ] && [ "$2" = "200" ]; then
  printf '201\\n'
elif [ "$1" = "-P" ] && [ "$2" = "300" ]; then
  printf '301\\n'
elif [ "$1" = "-P" ] && [ "$2" = "400" ]; then
  printf '401\\n'
elif [ "$1" = "-P" ] && [ "$2" = "500" ]; then
  printf '501\\n'
elif [ "$1" = "-P" ] && [ "$2" = "600" ]; then
  printf '601\\n'
fi
"""
        lsof_stub = f"""#!/usr/bin/env bash
set -euo pipefail
case "$3" in
  101)
    if [ "${{NO_CODEX_SESSION:-0}}" = "1" ]; then
      exit 0
    fi
    printf 'p101\\n'
    printf 'n{codex_session_path}\\n'
    ;;
  201)
    printf 'p201\\n'
    printf 'n{claude_session_path}\\n'
    ;;
  301)
    printf 'p301\\n'
    printf 'n{gemini_session_path}\\n'
    ;;
  401)
    printf 'p401\\n'
    printf 'n{second_codex_session_path}\\n'
    ;;
  501)
    printf 'p501\\n'
    printf 'n{second_claude_session_path}\\n'
    ;;
  601)
    printf 'p601\\n'
    printf 'n{second_gemini_session_path}\\n'
    ;;
esac
"""
        make_executable(self.tmpdir / "tmux", tmux_stub)
        make_executable(self.tmpdir / "pgrep", pgrep_stub)
        make_executable(self.tmpdir / "lsof", lsof_stub)
        make_executable(
            self.tmpdir / "ps",
            """#!/usr/bin/env bash
set -euo pipefail
pid=""
format=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -p) pid="$2"; shift 2 ;;
    -o) format="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [ "${METADATA_ONLY:-0}" = "1" ]; then
  exit 0
fi
case "$pid|$format" in
  100'|comm=') printf 'bash\\n' ;;
  100'|args=') printf 'bash\\n' ;;
  101'|comm=') printf 'node\\n' ;;
  101'|args=') printf '/usr/bin/node /usr/local/bin/codex\\n' ;;
  200'|comm=') printf 'bash\\n' ;;
  200'|args=') printf 'bash\\n' ;;
  201'|comm=') printf 'node\\n' ;;
  201'|args=') printf '/usr/bin/node /usr/local/bin/claude\\n' ;;
  300'|comm=') printf 'bash\\n' ;;
  300'|args=') printf 'bash\\n' ;;
  301'|comm=') printf 'node\\n' ;;
  301'|args=') printf '/usr/bin/node /usr/local/bin/gemini\\n' ;;
  400'|comm=') printf 'bash\\n' ;;
  400'|args=') printf 'bash\\n' ;;
  401'|comm=') printf 'node\\n' ;;
  401'|args=') printf '/usr/bin/node /usr/local/bin/codex\\n' ;;
  500'|comm=') printf 'bash\\n' ;;
  500'|args=') printf 'bash\\n' ;;
  501'|comm=') printf 'node\\n' ;;
  501'|args=') printf '/usr/bin/node /usr/local/bin/claude\\n' ;;
  600'|comm=') printf 'bash\\n' ;;
  600'|args=') printf 'bash\\n' ;;
  601'|comm=') printf 'node\\n' ;;
  601'|args=') printf '/usr/bin/node /usr/local/bin/gemini\\n' ;;
esac
""",
        )

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.tmpdir}:{self.env.get('PATH', '')}"
        self.env["HOME"] = str(self.tmpdir)
        self.env["XDG_CONFIG_HOME"] = str(self.tmpdir / "config")

    def test_codex_save_records_exact_codex_resume_session_id(self):
        subprocess.run(
            [REPO_ROOT / "bin" / "codex-save", str(self.manifest)],
            check=True,
            env=self.env,
        )

        rows = self.manifest.read_text(encoding="utf-8").splitlines()
        self.assertEqual(rows[0], "name\tdir\tcmd\targs")
        self.assertEqual(len(rows), 4, "annotated home window should still be skipped")
        self.assertIn(
            "proj\t/tmp/project\tcodex\tresume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4",
            rows,
        )
        self.assertIn(
            "claude-proj\t/tmp/claude-project\tclaude\t--resume 54f5b65c-a31c-4aa1-b91b-896b35e2a759",
            rows,
        )
        self.assertIn(
            "gemini-proj\t/tmp/gemini-project\tgemini\t--resume 27bd36d0-2977-4cce-9d5d-33764d915f1d",
            rows,
        )
        self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o600)

    def test_codex_save_finds_custom_home_session_for_shell_started_codex(self):
        env = self.env.copy()
        env["SHELL_STARTED_CODEX"] = "1"
        env["NO_CODEX_SESSION"] = "1"
        proc_fd_dir = self.tmpdir / "proc" / "101" / "fd"
        proc_fd_dir.mkdir(parents=True)
        (proc_fd_dir / "7").symlink_to(self.custom_codex_session_path)
        env["CODEX_PROC_ROOT"] = str(self.tmpdir / "proc")

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-save", str(self.manifest)],
            check=True,
            env=env,
        )

        rows = self.manifest.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "proj\t/tmp/project\tcodex\tresume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4",
            rows,
        )

    def test_codex_save_strips_stacked_status_and_memory_prefixes(self):
        env = self.env.copy()
        env["STACKED_WINDOW_NAME"] = "*READY* *512+MB** *RUN* proj"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-save", str(self.manifest)],
            check=True,
            env=env,
        )

        rows = self.manifest.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "proj\t/tmp/project\tcodex\tresume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4",
            rows,
        )

    def test_codex_save_prefers_stable_name_and_provider_pane_options(self):
        env = self.env.copy()
        env["STACKED_WINDOW_NAME"] = "node"
        env["STABLE_CODEX_NAME"] = "logical-codex-project"
        env["PANE_PROVIDER"] = "codex"
        env["METADATA_ONLY"] = "1"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-save", str(self.manifest)],
            check=True,
            env=env,
        )

        rows = self.manifest.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "logical-codex-project\t/tmp/project\tcodex\t"
            "resume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4",
            rows,
        )
        self.assertFalse(any(row.startswith("node\t") for row in rows))

    def test_codex_save_prefers_fresh_hook_session_metadata(self):
        env = self.env.copy()
        env["HOOK_SESSION_ID"] = self.hook_session_id
        env["HOOK_SESSION_PID"] = "101"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-save", str(self.manifest)],
            check=True,
            env=env,
        )

        rows = self.manifest.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            f"proj\t/tmp/project\tcodex\tresume {self.hook_session_id}",
            rows,
        )
        self.assertNotIn(
            "proj\t/tmp/project\tcodex\tresume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4",
            rows,
        )

    def test_codex_save_ignores_hook_metadata_owned_by_a_stale_pid(self):
        env = self.env.copy()
        env["HOOK_SESSION_ID"] = self.hook_session_id
        env["HOOK_SESSION_PID"] = "999"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-save", str(self.manifest)],
            check=True,
            env=env,
        )

        rows = self.manifest.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "proj\t/tmp/project\tcodex\tresume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4",
            rows,
        )
        self.assertNotIn(self.hook_session_id, "\n".join(rows))

    def test_codex_save_records_two_distinct_sessions_for_each_provider(self):
        env = self.env.copy()
        env["MULTI_SESSION"] = "1"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-save", str(self.manifest)],
            check=True,
            env=env,
        )

        rows = self.manifest.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 7)
        expected_args = {
            "proj": "resume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4",
            "codex-second": f"resume {self.second_codex_session_id}",
            "claude-proj": "--resume 54f5b65c-a31c-4aa1-b91b-896b35e2a759",
            "claude-second": f"--resume {self.second_claude_session_id}",
            "gemini-proj": "--resume 27bd36d0-2977-4cce-9d5d-33764d915f1d",
            "gemini-second": f"--resume {self.second_gemini_session_id}",
        }
        actual_args = {
            fields[0]: fields[3] for row in rows[1:] if len(fields := row.split("\t")) == 4
        }
        self.assertEqual(actual_args, expected_args)
        for first, second in (
            ("proj", "codex-second"),
            ("claude-proj", "claude-second"),
            ("gemini-proj", "gemini-second"),
        ):
            self.assertNotEqual(actual_args[first], actual_args[second])

    def test_codex_save_rejects_extra_manifest_arguments(self):
        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-save", str(self.manifest), str(self.tmpdir / "other.tsv")],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unexpected argument", result.stderr)

    def test_codex_save_named_session_uses_named_manifest_by_default(self):
        env = self.env.copy()
        env["CODEX_SESSION"] = "work"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-save"],
            check=True,
            env=env,
        )

        manifest = Path(env["XDG_CONFIG_HOME"]) / "codexfarm" / "manifests" / "work.tsv"
        rows = manifest.read_text(encoding="utf-8").splitlines()
        self.assertEqual(rows[0], "name\tdir\tcmd\targs")
        self.assertIn(
            "proj\t/tmp/project\tcodex\tresume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4",
            rows,
        )

    def test_codex_save_trims_remain_on_exit_wrapper(self):
        env = self.env.copy()
        env["WRAPPED_CODEX_START"] = "1"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-save", str(self.manifest)],
            check=True,
            env=env,
        )

        rows = self.manifest.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "proj\t/tmp/project\tcodex\tresume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4",
            rows,
        )

    def test_codex_save_trims_quoted_command_with_trailing_space(self):
        env = self.env.copy()
        env["QUOTED_CODEX_START"] = "1"
        env["NO_CODEX_SESSION"] = "1"

        subprocess.run(
            [
                REPO_ROOT / "bin" / "codex-save",
                "--allow-fallback",
                str(self.manifest),
            ],
            check=True,
            env=env,
        )

        rows = self.manifest.read_text(encoding="utf-8").splitlines()
        self.assertIn("proj\t/tmp/project\tcodex\tresume --last", rows)

    def test_codex_save_refuses_fallback_and_preserves_existing_manifest(self):
        original = b"existing manifest must survive\n"
        self.manifest.write_bytes(original)
        env = self.env.copy()
        env["NO_CODEX_SESSION"] = "1"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-save", str(self.manifest)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertIn("exact codex session", result.stderr.lower())
        self.assertIn("--allow-fallback", result.stderr)
        self.assertNotIn(self.hook_session_id, result.stderr)

    def test_codex_save_all_registered_writes_each_registered_manifest(self):
        registry = Path(self.env["XDG_CONFIG_HOME"]) / "codexfarm" / "farms.tsv"
        registry.parent.mkdir(parents=True, exist_ok=True)
        work_manifest = self.tmpdir / "work.tsv"
        registry.write_text(
            f"session\tmanifest\nwork\t{work_manifest}\n",
            encoding="utf-8",
        )
        env = self.env.copy()

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-save", "--all-registered"],
            check=True,
            env=env,
        )

        rows = work_manifest.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "proj\t/tmp/project\tcodex\tresume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4",
            rows,
        )

    def test_codex_save_all_registered_propagates_allow_fallback(self):
        registry = Path(self.env["XDG_CONFIG_HOME"]) / "codexfarm" / "farms.tsv"
        registry.parent.mkdir(parents=True, exist_ok=True)
        work_manifest = self.tmpdir / "work-fallback.tsv"
        registry.write_text(
            f"session\tmanifest\nwork\t{work_manifest}\n",
            encoding="utf-8",
        )
        env = self.env.copy()
        env["NO_CODEX_SESSION"] = "1"

        result = subprocess.run(
            [
                REPO_ROOT / "bin" / "codex-save",
                "--all-registered",
                "--allow-fallback",
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "proj\t/tmp/project\tcodex\tresume --last",
            work_manifest.read_text(encoding="utf-8").splitlines(),
        )


class RestoreScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="restore-script-test-"))
        self.tmux_log = self.tmpdir / "tmux.log"
        self.codex_add_log = self.tmpdir / "codex-add.log"
        self.manifest = self.tmpdir / "manifest.tsv"
        self.project_dir = self.tmpdir / "project"
        self.project_dir.mkdir()

        tmux_stub = """#!/usr/bin/env bash
set -euo pipefail
log="${TMUX_LOG}"
echo "$*" >> "$log"
case "$1" in
  has-session)
    exit 1
    ;;
  list-windows)
    format=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -F) format="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    if [ "${REQUIRE_REAL_TAB:-0}" = "1" ] && [ "$format" != $'#{window_id}\\t#{window_name}' ]; then
      printf 'list-windows format does not contain a real tab\\n' >&2
      exit 64
    fi
    if [ -n "${TMUX_WINDOWS_OUTPUT:-}" ]; then
      printf '%s\n' "${TMUX_WINDOWS_OUTPUT}"
    fi
    if [ -f "${TMUX_STATE_FILE}" ]; then
      cat "${TMUX_STATE_FILE}"
    fi
    exit 0
    ;;
  show-options)
    target=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -t) target="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    if [ "$target" = "${TMUX_STABLE_TARGET:-}" ] && [ -n "${TMUX_STABLE_NAME:-}" ]; then
      printf '%s\n' "$TMUX_STABLE_NAME"
    fi
    exit 0
    ;;
  kill-window)
    if [ "${3:-}" = "${TMUX_KILL_FAIL_TARGET:-}" ]; then
      exit 1
    fi
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""
        codex_add_stub = """#!/usr/bin/env bash
set -euo pipefail
echo "$CODEX_NAME|$CODEX_CMD|$CODEX_ARGS|$*" >> "${CODEX_ADD_LOG}"
count=0
if [ -f "${TMUX_STATE_FILE}" ]; then
  while IFS= read -r _line; do
    count=$((count + 1))
  done < "${TMUX_STATE_FILE}"
fi
count=$((count + 1))
printf '@new%s\\t%s\\n' "$count" "$CODEX_NAME" >> "${TMUX_STATE_FILE}"
exit 0
"""
        make_executable(self.tmpdir / "tmux", tmux_stub)
        make_executable(self.tmpdir / "codex-add", codex_add_stub)

        self.manifest.write_text(
            "name\tdir\tcmd\targs\n"
            f"proj\t{self.project_dir}\tcodex\tresume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4\n",
            encoding="utf-8",
        )

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.tmpdir}:{self.env.get('PATH', '')}"
        self.env["TMUX_LOG"] = str(self.tmux_log)
        self.env["TMUX_STATE_FILE"] = str(self.tmpdir / "tmux-state.tsv")
        self.env["CODEX_ADD_LOG"] = str(self.codex_add_log)
        self.env["CODEX_AUTOSERVICE_CHOICE"] = "no"
        self.env.pop("TMUX", None)
        self.env["HOME"] = str(self.tmpdir)
        self.env["XDG_CONFIG_HOME"] = str(self.tmpdir / "config")
        self.env["CODEX_ADD_BIN"] = str(self.tmpdir / "codex-add")

    def read_tmux_commands(self) -> list[list[str]]:
        lines = self.tmux_log.read_text(encoding="utf-8").splitlines()
        return [line.split() for line in lines]

    def write_duplicate_manifest(self) -> tuple[str, str]:
        first_id = "019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4"
        second_id = "123e4567-e89b-42d3-a456-426614174001"
        self.manifest.write_text(
            "name\tdir\tcmd\targs\n"
            f"dup\t{self.project_dir}\tcodex\tresume {first_id}\n"
            f"dup\t{self.project_dir}\tcodex\tresume {second_id}\n",
            encoding="utf-8",
        )
        return first_id, second_id

    def test_codex_restore_launches_saved_codex_resume_command(self):
        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=self.env,
        )

        add_calls = (
            self.codex_add_log.read_text(encoding="utf-8").splitlines()
            if self.codex_add_log.exists()
            else []
        )
        self.assertEqual(
            add_calls,
            [f"proj|codex|resume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4|-d {self.project_dir}"],
        )
        self.assertFalse(
            any(cmd and cmd[0] == "send-keys" for cmd in self.read_tmux_commands()),
            "restore should launch the resume command instead of typing it into a running CLI",
        )

    def test_codex_restore_creates_every_fresh_duplicate_name_row(self):
        first_id, second_id = self.write_duplicate_manifest()

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=self.env,
        )

        add_calls = self.codex_add_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            add_calls,
            [
                f"dup|codex|resume {first_id}|-d {self.project_dir}",
                f"dup|codex|resume {second_id}|-d {self.project_dir}",
            ],
        )

    def test_codex_restore_adds_only_missing_duplicate_occurrence(self):
        _first_id, second_id = self.write_duplicate_manifest()
        env = self.env.copy()
        env["TMUX_WINDOWS_OUTPUT"] = "@9\tdup"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=env,
        )

        add_calls = (
            self.codex_add_log.read_text(encoding="utf-8").splitlines()
            if self.codex_add_log.exists()
            else []
        )
        self.assertEqual(
            add_calls,
            [f"dup|codex|resume {second_id}|-d {self.project_dir}"],
        )

    def test_codex_restore_is_idempotent_with_two_existing_duplicates(self):
        self.write_duplicate_manifest()
        env = self.env.copy()
        env["TMUX_WINDOWS_OUTPUT"] = "@9\tdup\n@10\t*READY* dup"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=env,
        )

        self.assertFalse(self.codex_add_log.exists())

    def test_codex_restore_force_replaces_all_duplicate_occurrences_once(self):
        self.write_duplicate_manifest()
        env = self.env.copy()
        env["TMUX_WINDOWS_OUTPUT"] = "@9\tdup\n@10\t*READY* dup"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", "--force", str(self.manifest)],
            check=True,
            env=env,
        )

        commands = self.read_tmux_commands()
        killed = [command for command in commands if command[0] == "kill-window"]
        self.assertEqual(
            killed,
            [["kill-window", "-t", "@9"], ["kill-window", "-t", "@10"]],
        )
        self.assertEqual(
            len(self.codex_add_log.read_text(encoding="utf-8").splitlines()),
            2,
        )

    def test_codex_restore_force_stops_if_an_existing_window_cannot_be_removed(self):
        self.write_duplicate_manifest()
        env = self.env.copy()
        env["TMUX_WINDOWS_OUTPUT"] = "@9\tdup"
        env["TMUX_KILL_FAIL_TARGET"] = "@9"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", "--force", str(self.manifest)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.codex_add_log.exists())
        self.assertIn("Unable to remove existing window", result.stderr)

    def test_codex_restore_seeds_exact_session_metadata_without_printing_id(self):
        session_id = "019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4"

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.read_tmux_commands()
        self.assertIn(
            [
                "set-option",
                "-p",
                "-t",
                "@new1.0",
                "@codexfarm_provider",
                "codex",
            ],
            commands,
        )
        self.assertIn(
            [
                "set-option",
                "-p",
                "-t",
                "@new1.0",
                "@codexfarm_session_source",
                "restore",
            ],
            commands,
        )
        self.assertIn(
            [
                "set-option",
                "-p",
                "-t",
                "@new1.0",
                "@codexfarm_session_id",
                session_id,
            ],
            commands,
        )
        self.assertNotIn(session_id, result.stdout)
        self.assertNotIn(session_id, result.stderr)

    def test_codex_restore_matches_stable_pane_name_when_native_title_changes(self):
        env = self.env.copy()
        env["TMUX_WINDOWS_OUTPUT"] = "@9\tnode"
        env["TMUX_STABLE_TARGET"] = "@9.0"
        env["TMUX_STABLE_NAME"] = "proj"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=env,
        )

        self.assertFalse(self.codex_add_log.exists())

    def test_codex_restore_requests_tab_delimited_window_fields(self):
        env = self.env.copy()
        env["REQUIRE_REAL_TAB"] = "1"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=env,
        )

    def test_codex_restore_adds_legacy_codex_resume_last(self):
        self.manifest.write_text(
            f"name\tdir\tcmd\targs\nproj\t{self.project_dir}\tcodex\t\n",
            encoding="utf-8",
        )

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=self.env,
        )

        add_calls = self.codex_add_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(add_calls, [f"proj|codex|resume --last|-d {self.project_dir}"])

    def test_codex_restore_uses_manifest_tool_for_claude(self):
        self.manifest.write_text(
            f"name\tdir\tcmd\targs\nproj\t{self.project_dir}\tclaude\t--continue\n",
            encoding="utf-8",
        )

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=self.env,
        )

        add_calls = self.codex_add_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(add_calls, [f"proj|claude|--continue|-d {self.project_dir}"])

    def test_codex_restore_uses_manifest_tool_for_gemini(self):
        self.manifest.write_text(
            f"name\tdir\tcmd\targs\nproj\t{self.project_dir}\tgemini\t--resume latest\n",
            encoding="utf-8",
        )

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=self.env,
        )

        add_calls = self.codex_add_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(add_calls, [f"proj|gemini|--resume latest|-d {self.project_dir}"])

    def test_codex_restore_keeps_wrapper_provider_for_a_custom_command(self):
        custom_agent = self.tmpdir / "custom-agent"
        checking_add = self.tmpdir / "checking-add"
        make_executable(custom_agent, "#!/usr/bin/env bash\nexit 0\n")
        make_executable(
            checking_add,
            """#!/usr/bin/env bash
set -euo pipefail
[ "${CODEX_TOOL_NAME}" = codex ]
[ "${CODEX_CMD}" = "${EXPECTED_CUSTOM_COMMAND}" ]
""",
        )
        self.manifest.write_text(
            f"name\tdir\tcmd\targs\ncustom\t{self.project_dir}\t{custom_agent}\t--flag\n",
            encoding="utf-8",
        )
        env = self.env.copy()
        env["CODEX_ADD_BIN"] = str(checking_add)
        env["EXPECTED_CUSTOM_COMMAND"] = str(custom_agent)

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_codex_restore_skips_existing_annotated_window_name(self):
        env = self.env.copy()
        env["TMUX_WINDOWS_OUTPUT"] = "@9\t*RUN* proj"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=env,
        )

        add_calls = (
            self.codex_add_log.read_text(encoding="utf-8").splitlines()
            if self.codex_add_log.exists()
            else []
        )
        self.assertEqual(add_calls, [])

    def test_codex_restore_matches_stacked_annotated_window_name(self):
        env = self.env.copy()
        env["TMUX_WINDOWS_OUTPUT"] = "@9\t*READY* *512+MB** *RUN* proj"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=env,
        )

        add_calls = (
            self.codex_add_log.read_text(encoding="utf-8").splitlines()
            if self.codex_add_log.exists()
            else []
        )
        self.assertEqual(add_calls, [])

    def test_codex_restore_rejects_invalid_manifest_header(self):
        self.manifest.write_text("bad\theader\n", encoding="utf-8")

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Malformed manifest header", result.stderr)
        self.assertFalse(self.tmux_log.exists())

    def test_codex_restore_falls_back_when_saved_directory_is_missing(self):
        missing_dir = self.tmpdir / "missing"
        self.manifest.write_text(
            f"name\tdir\tcmd\targs\nproj\t{missing_dir}\tcodex\tresume --last\n",
            encoding="utf-8",
        )

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=self.env,
        )

        add_calls = self.codex_add_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(add_calls, [f"proj|codex|resume --last|-d {self.tmpdir}"])

    def test_codex_restore_all_registered_restores_each_farm_manifest(self):
        work_manifest = self.tmpdir / "work.tsv"
        work_manifest.write_text(
            "name\tdir\tcmd\targs\n"
            f"proj\t{self.project_dir}\tcodex\tresume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4\n",
            encoding="utf-8",
        )
        registry = Path(self.env["XDG_CONFIG_HOME"]) / "codexfarm" / "farms.tsv"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            f"session\tmanifest\nwork\t{work_manifest}\n",
            encoding="utf-8",
        )

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", "--all-registered"],
            check=True,
            env=self.env,
        )

        commands = self.read_tmux_commands()
        self.assertIn(["new-session", "-d", "-s", "work", "-n", "home"], commands)


class ResumeAndBoardScriptsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="resume-script-test-"))
        self.tmux_log = self.tmpdir / "tmux.log"

        tmux_stub = """#!/usr/bin/env bash
set -euo pipefail
log="${TMUX_LOG}"
echo "$*" >> "$log"
existing=" ${TMUX_EXISTING_SESSIONS:-} "
case "$1" in
  list-sessions)
    exit 0
    ;;
  has-session)
    if [[ "$existing" == *" $3 "* ]]; then
      exit 0
    fi
    exit 1
    ;;
  list-windows)
    if [ "${TMUX_LIST_WINDOWS_WITH_NAMES:-0}" = "1" ]; then
      printf '0\talpha\n1\thome\n2\tbeta\n'
    else
      printf '0\n1\n2\n'
    fi
    exit 0
    ;;
  new-window)
    echo "1"
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""
        make_executable(self.tmpdir / "tmux", tmux_stub)

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.tmpdir}:{self.env.get('PATH', '')}"
        self.env["TMUX_LOG"] = str(self.tmux_log)
        self.env.pop("TMUX", None)

    def read_tmux_commands(self) -> list[list[str]]:
        lines = self.tmux_log.read_text(encoding="utf-8").splitlines()
        return [line.split() for line in lines]

    def test_codex_resume_named_farm_attaches_to_that_session(self):
        env = self.env.copy()
        env["TMUX_EXISTING_SESSIONS"] = "work"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-resume", "work"],
            check=True,
            env=env,
        )

        commands = self.read_tmux_commands()
        self.assertIn(["attach", "-t", "work"], commands)

    def test_codex_resume_named_farm_board_attaches_to_derived_board(self):
        env = self.env.copy()
        env["TMUX_EXISTING_SESSIONS"] = "work work-board"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-resume", "work", "--board"],
            check=True,
            env=env,
        )

        commands = self.read_tmux_commands()
        self.assertIn(["attach", "-t", "work-board"], commands)

    def test_codex_board_named_farm_create_uses_derived_board_name(self):
        subprocess.run(
            [REPO_ROOT / "bin" / "codex-board", "create", "work"],
            check=True,
            env=self.env,
        )

        commands = self.read_tmux_commands()
        self.assertIn(["new-session", "-d", "-s", "work-board"], commands)

    def test_codex_board_named_farm_link_uses_named_main_and_board_sessions(self):
        env = self.env.copy()
        env["TMUX_EXISTING_SESSIONS"] = "work work-board"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-board", "link", "work"],
            check=True,
            env=env,
        )

        commands = self.read_tmux_commands()
        self.assertIn(
            ["list-windows", "-t", "work", "-F", "#{window_index}", "#{window_name}"],
            commands,
        )
        self.assertIn(["link-window", "-s", "work:1", "-t", "work-board"], commands)

    def test_codex_resume_rejects_extra_session_arguments(self):
        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-resume", "work", "other"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unexpected argument: other", result.stderr)

    def test_codex_resume_rejects_duplicate_session_arguments(self):
        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-resume", "--session", "work", "other"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Session specified multiple times", result.stderr)

    def test_codex_board_rejects_extra_session_arguments(self):
        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-board", "link", "work", "other"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Unexpected argument: other", result.stderr)

    def test_codex_board_link_skips_home_window_by_name(self):
        env = self.env.copy()
        env["TMUX_EXISTING_SESSIONS"] = "work work-board"
        env["TMUX_LIST_WINDOWS_WITH_NAMES"] = "1"

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-board", "link", "work"],
            check=True,
            env=env,
        )

        commands = self.read_tmux_commands()
        self.assertIn(
            ["list-windows", "-t", "work", "-F", "#{window_index}", "#{window_name}"],
            commands,
        )
        self.assertIn(["link-window", "-s", "work:0", "-t", "work-board"], commands)
        self.assertNotIn(["link-window", "-s", "work:1", "-t", "work-board"], commands)
        self.assertIn(["link-window", "-s", "work:2", "-t", "work-board"], commands)


class StatusAndWatchScriptsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="status-script-test-"))
        self.tmux_log = self.tmpdir / "tmux.log"
        tmux_stub = """#!/usr/bin/env bash
set -euo pipefail
log="${TMUX_LOG}"
echo "$*" >> "$log"
case "$1" in
  list-sessions)
    printf 'work: 1 windows (created Mon May 18 10:00:00 2026)\\n'
    exit 0
    ;;
  has-session)
    case "$3" in
      work|work-board) exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
  *)
    exit 0
    ;;
esac
"""
        make_executable(self.tmpdir / "tmux", tmux_stub)
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.tmpdir}:{self.env.get('PATH', '')}"
        self.env["TMUX_LOG"] = str(self.tmux_log)
        self.env["XDG_STATE_HOME"] = str(self.tmpdir / "state")
        self.env["HOME"] = str(self.tmpdir)

    def read_tmux_commands(self) -> list[list[str]]:
        lines = self.tmux_log.read_text(encoding="utf-8").splitlines()
        return [line.split() for line in lines]

    def test_codex_status_named_session_uses_derived_board(self):
        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-status", "--session", "work", "sessions"],
            check=True,
            env=self.env,
            text=True,
            capture_output=True,
        )

        self.assertIn("Main Codex session 'work' is running", result.stdout)
        self.assertIn("Board session 'work-board' is running", result.stdout)
        commands = self.read_tmux_commands()
        self.assertIn(["has-session", "-t", "work-board"], commands)

    def test_codex_watch_honors_state_basename_for_help(self):
        env = self.env.copy()
        env["CODEX_STATE_BASENAME"] = "workstate"
        logdir = Path(env["XDG_STATE_HOME"]) / "workstate" / "logs"
        logdir.mkdir(parents=True)
        (logdir / "one.log").write_text("hello\n", encoding="utf-8")

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-watch", "--help"],
            check=True,
            env=env,
            text=True,
            capture_output=True,
        )

        self.assertIn(f"Watches log files in {logdir}", result.stdout)

    def test_codex_status_reports_missing_tmux(self):
        env = self.env.copy()
        sparse_path = self.tmpdir / "sparse-path"
        sparse_path.mkdir()
        for command in ["bash", "basename"]:
            system_path = shutil.which(command)
            if not system_path:
                raise RuntimeError(f"Missing required test command: {command}")
            (sparse_path / command).symlink_to(system_path)
        env["PATH"] = str(sparse_path)

        result = subprocess.run(
            [REPO_ROOT / "bin" / "codex-status", "sessions"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 127)
        self.assertIn("tmux not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
