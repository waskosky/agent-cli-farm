from pathlib import Path
import stat
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"

EXPECTED_WRAPPERS = [
    "claude-add",
    "claude-annotator",
    "claude-board",
    "claude-restore",
    "claude-resume",
    "claude-save",
    "claude-status",
    "claude-watch",
]


class ClaudeWrappersExistenceTests(unittest.TestCase):
    def test_wrappers_present_and_executable(self):
        missing = []
        non_exec = []
        for name in EXPECTED_WRAPPERS:
            path = BIN_DIR / name
            if not path.exists():
                missing.append(name)
                continue
            mode = path.stat().st_mode
            if not (mode & stat.S_IXUSR):
                non_exec.append(name)
        self.assertEqual(
            missing, [],
            f"Missing claude wrapper scripts: {missing}",
        )
        self.assertEqual(
            non_exec, [],
            f"Non-executable claude wrapper scripts: {non_exec}",
        )


if __name__ == "__main__":
    unittest.main()
