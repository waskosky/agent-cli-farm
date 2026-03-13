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
        for command in ["basename", "cat", "chmod", "cp", "dirname", "grep", "mkdir", "rm"]:
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

    def test_continues_when_optional_multitail_install_fails(self) -> None:
        make_executable(self.bin_dir / "tmux", "#!/usr/bin/env bash\nexit 0\n")
        make_executable(
            self.bin_dir / "apt",
            f"""#!/bin/bash
echo "$*" >> "{self.pkg_log}"
case "$1" in
  update) exit 0 ;;
  install) exit 1 ;;
  *) exit 0 ;;
esac
""",
        )
        make_executable(
            self.bin_dir / "sudo",
            """#!/bin/bash
exec "$@"
""",
        )

        result = self.run_setup()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Missing commands: multitail", result.stdout)
        self.assertIn("Dependency installation did not complete cleanly.", result.stdout)
        self.assertIn(
            "multitail is still missing; codex-watch will fall back to simple tail mode.",
            result.stdout,
        )
        self.assertIn("install -y multitail", self.pkg_log.read_text(encoding="utf-8"))
        self.assertTrue((Path(self.env["HOME"]) / "bin" / "codex-watch").exists())


if __name__ == "__main__":
    unittest.main()
