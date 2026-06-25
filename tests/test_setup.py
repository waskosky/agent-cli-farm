import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


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
            "python3",
            "rm",
        ]:
            system_path = shutil.which(command)
            if not system_path:
                raise RuntimeError(f"Missing required test command: {command}")
            (self.bin_dir / command).symlink_to(system_path)

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
        self.assertTrue(
            (Path(self.env["HOME"]) / "bin" / "add_high_memory_warning.sh").exists()
        )

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
