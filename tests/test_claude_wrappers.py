import stat
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"

WRAPPER_SUFFIXES = [
    "add",
    "annotator",
    "board",
    "restore",
    "resume",
    "save",
    "status",
    "watch",
]


class ToolWrappersExistenceTests(unittest.TestCase):
    def test_wrappers_present_and_executable(self):
        missing = []
        non_exec = []
        for tool in ["claude", "gemini"]:
            for suffix in WRAPPER_SUFFIXES:
                name = f"{tool}-{suffix}"
                path = BIN_DIR / name
                if not path.exists():
                    missing.append(name)
                    continue
                mode = path.stat().st_mode
                if not (mode & stat.S_IXUSR):
                    non_exec.append(name)
        self.assertEqual(
            missing,
            [],
            f"Missing tool wrapper scripts: {missing}",
        )
        self.assertEqual(
            non_exec,
            [],
            f"Non-executable tool wrapper scripts: {non_exec}",
        )


if __name__ == "__main__":
    unittest.main()
