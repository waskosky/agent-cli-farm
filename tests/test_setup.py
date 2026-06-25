import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

    def run_setup(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(REPO_ROOT / "setup.sh")],
            cwd=REPO_ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
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
        self.assertTrue((Path(self.env["HOME"]) / "bin" / "codex-memoryflag").exists())
        self.assertTrue((Path(self.env["HOME"]) / "bin" / "add_high_memory_warning.sh").exists())

    def test_installs_claude_and_gemini_wrappers(self) -> None:
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(self.bin_dir / "multitail", "#!/usr/bin/env bash\nexit 0\n")

        result = self.run_setup()

        self.assertEqual(result.returncode, 0, result.stderr)
        home_bin = Path(self.env["HOME"]) / "bin"
        self.assertTrue((home_bin / "claude-add").exists())
        self.assertTrue((home_bin / "claude-looper").exists())
        self.assertTrue((home_bin / "gemini-add").exists())
        self.assertTrue((home_bin / "gemini-looper").exists())

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
        self.assertIn("Using Python interpreter: python3.12", result.stdout)

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
