import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_deep_history_installer import build_archive, write_lock

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def make_fake_python(path: Path, major: int, minor: int) -> None:
    exit_code = 0 if (major, minor) >= (3, 10) else 1
    make_executable(
        path,
        f"""#!/bin/bash
if [ -n "${{FAKE_PYTHON_LOG:-}}" ]; then
  echo "${{0##*/}} $*" >> "$FAKE_PYTHON_LOG"
fi
if [ "${{1:-}}" = "--version" ]; then
  echo "Python {major}.{minor}.0"
  exit 0
fi
exit {exit_code}
""",
    )


class SetupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="setup-test-"))
        self.bin_dir = self.tmpdir / "bin-tools"
        self.bin_dir.mkdir()
        self.pkg_log = self.tmpdir / "pkg.log"

        self.env = os.environ.copy()
        self.env["HOME"] = str(self.tmpdir / "home")
        self.env["XDG_STATE_HOME"] = str(self.tmpdir / "state")
        self.env["XDG_CONFIG_HOME"] = str(self.tmpdir / "config")
        self.env["PATH"] = str(self.bin_dir)
        self.env["SHELL"] = "/bin/bash"

        Path(self.env["HOME"]).mkdir(parents=True, exist_ok=True)
        for command in [
            "basename",
            "cat",
            "chmod",
            "cp",
            "dirname",
            "grep",
            "mkdir",
            "rm",
        ]:
            system_path = shutil.which(command)
            if not system_path:
                raise RuntimeError(f"Missing required test command: {command}")
            (self.bin_dir / command).symlink_to(system_path)
        make_fake_python(self.bin_dir / "python3", 3, 12)

    def run_setup(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(REPO_ROOT / "setup.sh"), *arguments],
            cwd=REPO_ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_help_documents_optional_deep_history_install_without_dependencies(self) -> None:
        result = self.run_setup("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--with-deep-history", result.stdout)
        self.assertIn("--without-session-hook", result.stdout)

    def test_installs_session_hook_and_preserves_unrelated_user_hooks(self) -> None:
        (self.bin_dir / "python3").unlink()
        (self.bin_dir / "python3").symlink_to(sys.executable)
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")
        hooks_file = Path(self.env["HOME"]) / ".codex" / "hooks.json"
        hooks_file.parent.mkdir(parents=True)
        hooks_file.write_text(
            json.dumps(
                {
                    "description": "preserve",
                    "hooks": {
                        "PostToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "echo existing"}],
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

        first = self.run_setup()
        second = self.run_setup()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        config = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertEqual(config["description"], "preserve")
        self.assertIn("echo existing", json.dumps(config))
        expected_hook = Path(self.env["HOME"]) / "bin" / "codex-session-hook.py"
        commands = [
            handler["command"]
            for event in ("SessionStart", "UserPromptSubmit")
            for group in config["hooks"][event]
            for handler in group["hooks"]
            if Path(shlex.split(handler["command"])[-1]).name == "codex-session-hook.py"
        ]
        expected_command = shlex.join([str(self.bin_dir / "python3"), str(expected_hook)])
        self.assertEqual(commands, [expected_command, expected_command])
        self.assertEqual(stat.S_IMODE(hooks_file.stat().st_mode), 0o600)
        self.assertIn("Review and trust it with /hooks", first.stdout)
        for provider in ("claude", "gemini"):
            settings = Path(self.env["HOME"]) / f".{provider}" / "settings.json"
            config = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o600)
            self.assertEqual(len(config["hooks"]["SessionStart"]), 1)
            self.assertEqual(len(config["hooks"]["UserPromptSubmit"]), 1)

    def test_without_session_hook_skips_user_config_change(self) -> None:
        (self.bin_dir / "python3").unlink()
        (self.bin_dir / "python3").symlink_to(sys.executable)
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")
        hooks_file = Path(self.env["HOME"]) / ".codex" / "hooks.json"

        result = self.run_setup("--without-session-hook")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(hooks_file.exists())
        self.assertFalse((Path(self.env["HOME"]) / ".claude/settings.json").exists())
        self.assertFalse((Path(self.env["HOME"]) / ".gemini/settings.json").exists())
        self.assertIn("Session hook installation skipped", result.stdout)

    def test_resource_helpers_installed_inert_and_import_packaged_modules(self):
        (self.bin_dir / "python3").unlink()
        (self.bin_dir / "python3").symlink_to(sys.executable)
        result = self.run_setup("--without-session-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("codex-resource status", result.stdout)
        home = Path(self.env["HOME"])
        for name in ("codex-resource", "codex-job", "codex-resource-host"):
            installed = home / "bin" / name
            self.assertEqual(installed.read_bytes(), (REPO_ROOT / "bin" / name).read_bytes())
            result = subprocess.run(
                [sys.executable, str(installed), "--help"],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            (Path(self.env["XDG_CONFIG_HOME"]) / "codexfarm/resource-settings.json").exists()
        )
        self.assertFalse((home / ".config/systemd").exists())

    def test_installed_jobs_register_after_setup_with_ordinary_umasks(self):
        (self.bin_dir / "python3").unlink()
        (self.bin_dir / "python3").symlink_to(sys.executable)
        for umask, role in (("022", "batch"), ("002", "batch"), ("022", "agent"), ("002", "agent")):
            with self.subTest(umask=umask, role=role):
                root = self.tmpdir / umask / role
                home, state, config = root / "home", root / "state", root / "config"
                for directory in (home, state, config):
                    directory.mkdir(parents=True, mode=0o755)
                    directory.chmod(0o755)
                env = dict(
                    self.env, HOME=str(home), XDG_STATE_HOME=str(state), XDG_CONFIG_HOME=str(config)
                )
                masks = config / "systemd/user"
                masks.mkdir(parents=True)
                mask = masks / "codexfarm-autosave.timer"
                mask.symlink_to("/dev/null")
                result = subprocess.run(
                    [
                        "/bin/bash",
                        "-c",
                        'umask "$1"; exec /bin/bash "$2" --without-session-hook',
                        "setup-test",
                        umask,
                        str(REPO_ROOT / "setup.sh"),
                    ],
                    cwd=REPO_ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                farm_state = state / "codexfarm"
                self.assertEqual(stat.S_IMODE(farm_state.stat().st_mode), 0o777 & ~int(umask, 8))
                archive = farm_state / "backups/sentinel.tar.gz"
                archive.parent.mkdir()
                archive.write_bytes(b"existing archive")
                count = root / "launches"
                code = (
                    "import json,sys; "
                    f"open({str(count)!r},'a').write('once\\n'); "
                    "print(json.dumps(sys.argv[1:]))"
                )
                literal_args = ["a b", "$value; `data`", "", "\\", '"quoted"']
                argv = [sys.executable, "-c", code, *literal_args]
                launch = [
                    str(home / "bin/codex-job"),
                    "run",
                    "--role",
                    role,
                    "--scope",
                    "off",
                    "--memory-policy",
                    "ignore",
                    "--",
                    *argv,
                ]
                if role == "agent":
                    env["CODEXFARM_RESOURCE_PROTECTION"] = "1"
                    # Exercise the installed opt-in launcher integration, with
                    # scope off to avoid any real user-manager interaction.
                    bootstrap = (
                        "import os,sys; "
                        f"sys.path.insert(0,{str(home / 'bin')!r}); "
                        "from codex_looper.resource_jobs import optional_agent_command; "
                        "command=optional_agent_command(sys.argv[1:],dict(os.environ)); "
                        "assert command != sys.argv[1:]; "
                        "command[command.index('--'):command.index('--')]='--scope off "
                        "--memory-policy ignore'.split(); "
                        "os.execvpe(command[0],command,os.environ)"
                    )
                    launch = [sys.executable, "-c", bootstrap, *argv]
                result = subprocess.run(
                    launch,
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), literal_args)
                self.assertNotIn("registration unavailable", result.stderr)
                records = [
                    json.loads(path.read_text())
                    for path in (farm_state / "resources/jobs").glob("*.json")
                    if ".started." not in path.name
                ]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["role"], role)
                self.assertEqual(records[0]["argv"], argv)
                self.assertTrue(records[0]["managed_launch"])
                self.assertIsNotNone(records[0]["payload_start_ticks"])
                self.assertEqual(count.read_text().splitlines(), ["once"])
                for directory in (
                    farm_state,
                    farm_state / "resources",
                    farm_state / "resources/jobs",
                    farm_state / "resources/overrides",
                ):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                for path in (farm_state / "resources").rglob("*"):
                    if path.is_file():
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                for directory in (home, state, config):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
                self.assertEqual(os.readlink(mask), "/dev/null")
                self.assertEqual(archive.read_bytes(), b"existing archive")

    def test_sourced_setup_forwards_deep_history_flag(self) -> None:
        (self.bin_dir / "python3").unlink()
        (self.bin_dir / "python3").symlink_to(sys.executable)
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")
        archive = self.tmpdir / "tmux-deep-history.zip"
        lock = self.tmpdir / "tmux-deep-history.lock"
        write_lock(lock, build_archive(archive))
        self.env["CODEXFARM_DEEP_HISTORY_ARCHIVE"] = str(archive)
        self.env["CODEXFARM_DEEP_HISTORY_LOCK_FILE"] = str(lock)

        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                f'. "{REPO_ROOT / "setup.sh"}" --with-deep-history',
            ],
            cwd=REPO_ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        installed = (
            Path(self.env["HOME"])
            / ".local"
            / "share"
            / "codexfarm"
            / "plugins"
            / "tmux-deep-history"
        )
        self.assertTrue((installed / "bin" / "tmux-deep-history").is_file())
        self.assertIn("Installed tmux-deep-history 0.1.0", result.stdout)

    def test_with_deep_history_installs_pinned_local_release(self) -> None:
        (self.bin_dir / "python3").unlink()
        (self.bin_dir / "python3").symlink_to(sys.executable)
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")
        archive = self.tmpdir / "tmux-deep-history.zip"
        lock = self.tmpdir / "tmux-deep-history.lock"
        write_lock(lock, build_archive(archive))
        self.env["CODEXFARM_DEEP_HISTORY_ARCHIVE"] = str(archive)
        self.env["CODEXFARM_DEEP_HISTORY_LOCK_FILE"] = str(lock)

        result = self.run_setup("--with-deep-history")

        self.assertEqual(result.returncode, 0, result.stderr)
        installed = (
            Path(self.env["HOME"])
            / ".local"
            / "share"
            / "codexfarm"
            / "plugins"
            / "tmux-deep-history"
        )
        self.assertTrue((installed / "bin" / "tmux-deep-history").is_file())
        self.assertIn("Installed tmux-deep-history 0.1.0", result.stdout)

    def test_with_deep_history_without_installer_overrides_works_under_nounset(self) -> None:
        python_log = self.tmpdir / "python.log"
        self.env["FAKE_PYTHON_LOG"] = str(python_log)

        result = self.run_setup("--with-deep-history")

        self.assertEqual(result.returncode, 0, result.stderr)
        invocations = python_log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(
            any(
                invocation.endswith("integrations/install_tmux_deep_history.py")
                for invocation in invocations
            ),
            invocations,
        )

    def test_skips_package_manager_when_dependencies_already_exist(self) -> None:
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(
            self.bin_dir / "apt",
            f"""#!/bin/bash
echo "apt $*" >> "{self.pkg_log}"
exit 99
""",
        )
        make_executable(
            self.bin_dir / "sudo",
            f"""#!/bin/bash
echo "sudo $*" >> "{self.pkg_log}"
exit 98
""",
        )

        result = self.run_setup()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Dependencies already available; skipping package installation.",
            result.stdout,
        )
        self.assertFalse(self.pkg_log.exists(), "package manager should not be called")
        self.assertTrue((Path(self.env["HOME"]) / "bin" / "codex-add").exists())
        self.assertTrue((Path(self.env["HOME"]) / "bin" / "codex-looper").exists())
        self.assertTrue((Path(self.env["HOME"]) / "bin" / "codex-doctor").exists())
        self.assertTrue((Path(self.env["HOME"]) / "bin" / "codex-memoryflag").exists())
        self.assertTrue((Path(self.env["HOME"]) / "bin" / "add_high_memory_warning.sh").exists())
        source_marker = Path(self.env["XDG_STATE_HOME"]) / "codexfarm" / "install-source"
        self.assertEqual(source_marker.read_text(encoding="utf-8").strip(), str(REPO_ROOT))
        self.assertEqual(stat.S_IMODE(source_marker.stat().st_mode), 0o600)

    def test_installs_claude_and_gemini_wrappers(self) -> None:
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")

        result = self.run_setup()

        self.assertEqual(result.returncode, 0, result.stderr)
        home_bin = Path(self.env["HOME"]) / "bin"
        self.assertTrue((home_bin / "claude-add").exists())
        self.assertTrue((home_bin / "claude-farm-reboot").exists())
        self.assertTrue((home_bin / "claude-looper").exists())
        self.assertTrue((home_bin / "gemini-add").exists())
        self.assertTrue((home_bin / "gemini-farm-reboot").exists())
        self.assertTrue((home_bin / "gemini-looper").exists())
        self.assertTrue((home_bin / "codex-farm-reboot").exists())

    def test_setup_examples_show_default_farm_looper_usage(self) -> None:
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")

        result = self.run_setup()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "codex-looper                 # Run initialized prompt loops in the default farm",
            result.stdout,
        )
        self.assertIn("codex-looper --local --once --label local-smoke", result.stdout)
        self.assertNotIn("codex-looper --farm-session work --label sweep", result.stdout)

    def test_skips_package_manager_when_dependencies_missing(self) -> None:
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(
            self.bin_dir / "apt",
            f"""#!/bin/bash
echo "$*" >> "{self.pkg_log}"
exit 99
""",
        )
        make_executable(
            self.bin_dir / "sudo",
            f"""#!/bin/bash
echo "sudo $*" >> "{self.pkg_log}"
exit 98
""",
        )

        result = self.run_setup()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Missing commands: multitail", result.stdout)
        self.assertFalse(self.pkg_log.exists(), "package manager and sudo should not be called")
        self.assertIn(
            "Dependency installation skipped; install missing commands separately for full functionality.",
            result.stdout,
        )
        self.assertIn(
            "multitail is still missing; codex-watch will fall back to simple tail mode.",
            result.stdout,
        )
        self.assertTrue((Path(self.env["HOME"]) / "bin" / "codex-watch").exists())

    def test_uses_versioned_python_when_python3_is_too_old(self) -> None:
        make_fake_python(self.bin_dir / "python3", 3, 9)
        make_fake_python(self.bin_dir / "python3.12", 3, 12)
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")

        result = self.run_setup()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"Using Python interpreter: {self.bin_dir / 'python3.12'}",
            result.stdout,
        )

    def test_installed_python_wrappers_fall_back_to_versioned_python(self) -> None:
        make_fake_python(self.bin_dir / "python3", 3, 9)
        make_fake_python(self.bin_dir / "python3.12", 3, 12)
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")

        result = self.run_setup()

        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.tmpdir / "python-wrapper.log"
        self.env["FAKE_PYTHON_LOG"] = str(log)
        wrapper = Path(self.env["HOME"]) / "bin" / "codex-looper"
        wrapper_result = subprocess.run(
            ["/bin/bash", str(wrapper), "--help"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(wrapper_result.returncode, 0, wrapper_result.stderr)
        self.assertTrue(log.exists())
        log_lines = log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(log_lines[-1].startswith("python3.12 "))

    def test_installed_codex_looper_imports_packaged_modules(self) -> None:
        (self.bin_dir / "python3").unlink()
        (self.bin_dir / "python3").symlink_to(sys.executable)
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")

        result = self.run_setup()

        self.assertEqual(result.returncode, 0, result.stderr)
        wrapper = Path(self.env["HOME"]) / "bin" / "codex-looper"
        wrapper_result = subprocess.run(
            ["/bin/bash", str(wrapper), "--help"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(wrapper_result.returncode, 0, wrapper_result.stderr)
        self.assertIn("Tiny coding-agent looper", wrapper_result.stdout)

    def test_can_be_sourced_without_leaking_strict_shell_options(self) -> None:
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")
        outside = self.tmpdir / "outside"
        outside.mkdir()

        script = f"""
set +e
set +u
set +o pipefail
cd "{outside}"
. "{REPO_ROOT / "setup.sh"}"
case "$-" in *e*) echo "errexit leaked"; exit 41;; esac
case "$-" in *u*) echo "nounset leaked"; exit 42;; esac
if set -o | grep -q '^pipefail[[:space:]]*on'; then
  echo "pipefail leaked"
  exit 43
fi
case ":$PATH:" in
  *:"$HOME/bin":*) ;;
  *) echo "home bin missing from PATH"; exit 44;;
esac
if declare -F codexfarm_setup_main >/dev/null; then
  echo "setup helper leaked"
  exit 45
fi
"""

        result = subprocess.run(
            ["/bin/bash", "-c", script],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
