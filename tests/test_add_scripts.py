import os
import stat
import subprocess
import tempfile
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
        self.assertIn(f"-c {target_dir}", command)

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
        self.assertIn("ExecStart=", autosave_content)
        self.assertIn("OnCalendar=hourly", autosave_timer_content)
        self.assertIn("Unit=codex-autosave.service", autosave_timer_content)
        self.assertIn("codex-restore -a", autorestore_content)

        choice_file = Path(env["XDG_STATE_HOME"]) / "codexfarm" / "autoservice_choice"
        self.assertEqual(choice_file.read_text().strip(), "yes")

        systemctl_calls = systemctl_log.read_text().splitlines()
        self.assertTrue(any("--user" in line for line in systemctl_calls))
        self.assertTrue(
            any("codex-autosave.timer" in line for line in systemctl_calls),
            f"Expected timer-related systemctl calls, got: {systemctl_calls}",
        )

    def test_autoservice_install_pins_selected_session(self):
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

        systemd_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
        autosave_content = (systemd_dir / "codex-autosave.service").read_text()
        autorestore_content = (systemd_dir / "codex-autorestore.service").read_text()
        self.assertIn("Environment=CODEX_SESSION=work", autosave_content)
        self.assertIn("Environment=CODEX_SESSION=work", autorestore_content)


class SaveScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="save-script-test-"))
        self.manifest = self.tmpdir / "manifest.tsv"

        codex_session_path = (
            "/home/test/.codex/sessions/2026/05/11/"
            "rollout-2026-05-11T03-23-27-019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4.jsonl"
        )
        claude_session_path = (
            "/home/test/.claude/projects/-tmp-project/"
            "54f5b65c-a31c-4aa1-b91b-896b35e2a759.jsonl"
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
        tmux_stub = """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  has-session)
    exit 0
    ;;
  list-windows)
    printf '0\\n1\\n2\\n3\\n'
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
      codexfarm:0'|#{window_name}') printf '*READY* home\\n' ;;
      codexfarm:1'|#{window_name}') printf '*RUN* proj\\n' ;;
      codexfarm:1.0'|#{pane_current_path}') printf '/tmp/project\\n' ;;
      codexfarm:1.0'|#{pane_start_command}') printf 'codex\\n' ;;
      codexfarm:1.0'|#{pane_pid}') printf '100\\n' ;;
      codexfarm:2'|#{window_name}') printf 'claude-proj\\n' ;;
      codexfarm:2.0'|#{pane_current_path}') printf '/tmp/claude-project\\n' ;;
      codexfarm:2.0'|#{pane_start_command}') printf 'claude\\n' ;;
      codexfarm:2.0'|#{pane_pid}') printf '200\\n' ;;
      codexfarm:3'|#{window_name}') printf 'gemini-proj\\n' ;;
      codexfarm:3.0'|#{pane_current_path}') printf '/tmp/gemini-project\\n' ;;
      codexfarm:3.0'|#{pane_start_command}') printf 'gemini\\n' ;;
      codexfarm:3.0'|#{pane_pid}') printf '300\\n' ;;
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
fi
"""
        lsof_stub = f"""#!/usr/bin/env bash
set -euo pipefail
case "$3" in
  101)
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
esac
"""
        make_executable(self.tmpdir / "tmux", tmux_stub)
        make_executable(self.tmpdir / "pgrep", pgrep_stub)
        make_executable(self.tmpdir / "lsof", lsof_stub)

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.tmpdir}:{self.env.get('PATH', '')}"
        self.env["HOME"] = str(self.tmpdir)

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


class RestoreScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="restore-script-test-"))
        self.tmux_log = self.tmpdir / "tmux.log"
        self.codex_add_log = self.tmpdir / "codex-add.log"
        self.manifest = self.tmpdir / "manifest.tsv"

        tmux_stub = """#!/usr/bin/env bash
set -euo pipefail
log="${TMUX_LOG}"
echo "$*" >> "$log"
case "$1" in
  has-session)
    exit 1
    ;;
  list-windows)
    if [ -n "${TMUX_WINDOWS_OUTPUT:-}" ]; then
      printf '%s\n' "${TMUX_WINDOWS_OUTPUT}"
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
exit 0
"""
        make_executable(self.tmpdir / "tmux", tmux_stub)
        make_executable(self.tmpdir / "codex-add", codex_add_stub)

        self.manifest.write_text(
            "name\tdir\tcmd\targs\n"
            "proj\t/tmp/project\tcodex\tresume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4\n",
            encoding="utf-8",
        )

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.tmpdir}:{self.env.get('PATH', '')}"
        self.env["TMUX_LOG"] = str(self.tmux_log)
        self.env["CODEX_ADD_LOG"] = str(self.codex_add_log)
        self.env["CODEX_AUTOSERVICE_CHOICE"] = "no"
        self.env.pop("TMUX", None)
        self.env["HOME"] = str(self.tmpdir)
        self.env["CODEX_ADD_BIN"] = str(self.tmpdir / "codex-add")

    def read_tmux_commands(self) -> list[list[str]]:
        lines = self.tmux_log.read_text(encoding="utf-8").splitlines()
        return [line.split() for line in lines]

    def test_codex_restore_launches_saved_codex_resume_command(self):
        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=self.env,
        )

        add_calls = self.codex_add_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            add_calls,
            [
                "proj|codex|resume 019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4|-d /tmp/project"
            ],
        )
        self.assertFalse(
            any(cmd and cmd[0] == "send-keys" for cmd in self.read_tmux_commands()),
            "restore should launch the resume command instead of typing it into a running CLI",
        )

    def test_codex_restore_adds_legacy_codex_resume_last(self):
        self.manifest.write_text(
            "name\tdir\tcmd\targs\nproj\t/tmp/project\tcodex\t\n",
            encoding="utf-8",
        )

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=self.env,
        )

        add_calls = self.codex_add_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(add_calls, ["proj|codex|resume --last|-d /tmp/project"])

    def test_codex_restore_uses_manifest_tool_for_claude(self):
        self.manifest.write_text(
            "name\tdir\tcmd\targs\nproj\t/tmp/project\tclaude\t--continue\n",
            encoding="utf-8",
        )

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=self.env,
        )

        add_calls = self.codex_add_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(add_calls, ["proj|claude|--continue|-d /tmp/project"])

    def test_codex_restore_uses_manifest_tool_for_gemini(self):
        self.manifest.write_text(
            "name\tdir\tcmd\targs\nproj\t/tmp/project\tgemini\t--resume latest\n",
            encoding="utf-8",
        )

        subprocess.run(
            [REPO_ROOT / "bin" / "codex-restore", str(self.manifest)],
            check=True,
            env=self.env,
        )

        add_calls = self.codex_add_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(add_calls, ["proj|gemini|--resume latest|-d /tmp/project"])

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
    printf '0\n1\n2\n'
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
        self.assertIn(["list-windows", "-t", "work", "-F", "#{window_index}"], commands)
        self.assertIn(["link-window", "-s", "work:1", "-t", "work-board"], commands)


if __name__ == "__main__":
    unittest.main()
