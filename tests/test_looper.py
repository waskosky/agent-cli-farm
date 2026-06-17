import importlib.util
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LOOPER_PATH = REPO_ROOT / "bin" / "codex-looper.py"


def load_looper_module():
    loader = SourceFileLoader("codex_looper_test_module", str(LOOPER_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Unable to load codex-looper module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class LooperCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.looper = load_looper_module()

    def test_load_prompts_splits_on_separator_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "prompts.md"
            path.write_text("one\n---\ntwo\n---   \nthree\n", encoding="utf-8")

            self.assertEqual(
                self.looper.load_prompts(path, r"^---\s*$"),
                ["one", "two", "three"],
            )

    def test_builds_codex_first_and_resume_commands(self) -> None:
        agent = self.looper.AgentConfig(
            name="codex",
            kind="codex",
            extra_args=["--sandbox", "workspace-write"],
        )
        context = self.looper.CommandContext(
            prompt="do it",
            session="label-loop-0001",
            session_id="thread-123",
            loop=1,
            prompt_index=2,
            label="label",
            run_dir=Path("runs/x"),
        )

        first = self.looper.build_command(
            agent=agent,
            context=context,
            is_first_prompt_in_session=True,
        )
        resume = self.looper.build_command(
            agent=agent,
            context=context,
            is_first_prompt_in_session=False,
        )

        self.assertEqual(
            first,
            ["codex", "exec", "--json", "--sandbox", "workspace-write", "do it"],
        )
        self.assertEqual(
            resume,
            [
                "codex",
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "resume",
                "thread-123",
                "do it",
            ],
        )

    def test_builds_claude_first_and_resume_commands(self) -> None:
        agent = self.looper.AgentConfig(
            name="claude",
            kind="claude",
            extra_args=["--max-turns", "20"],
        )
        context = self.looper.CommandContext(
            prompt="do it",
            session="label-loop-0001",
            session_id="",
            loop=1,
            prompt_index=1,
            label="label",
            run_dir=Path("runs/x"),
        )

        first = self.looper.build_command(
            agent=agent,
            context=context,
            is_first_prompt_in_session=True,
        )
        resume = self.looper.build_command(
            agent=agent,
            context=context,
            is_first_prompt_in_session=False,
        )

        self.assertEqual(
            first,
            [
                "claude",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--max-turns",
                "20",
                "--name",
                "label-loop-0001",
                "do it",
            ],
        )
        self.assertEqual(
            resume,
            [
                "claude",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--max-turns",
                "20",
                "--resume",
                "label-loop-0001",
                "do it",
            ],
        )

    def test_load_config_falls_back_when_tomllib_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "agent-looper.toml"
            path.write_text(
                r'''
[looper]
default_agent = "claude"
prompt_file = "custom-prompts.md"
separator = "^---\\s*$"
timeout_seconds = 600
sleep_seconds = 5
fresh_session_per_loop = false
max_loops = 15
log_dir = ".agent-looper/runs"
stop_patterns = ["rate limit", "overloaded"]
kill_on_stop_pattern = false
ignore_nonzero = true
scan_stdout_for_stop_patterns = true

[agents.codex]
kind = "codex"
extra_args = ["--sandbox", "workspace-write"]
env = { CODEX_HOME = ".codex" }

[agents.gemini]
kind = "generic"
first_command = ["gemini", "-p", "{prompt}"]
resume_command = ["gemini", "-p", "{prompt}"]
scan_stdout_for_stop_patterns = true
''',
                encoding="utf-8",
            )

            self.looper.tomllib = None
            loaded = self.looper.load_config(path)

        self.assertEqual(loaded.looper.default_agent, "claude")
        self.assertEqual(loaded.looper.prompt_file, Path("custom-prompts.md"))
        self.assertEqual(loaded.looper.separator, r"^---\s*$")
        self.assertEqual(loaded.looper.timeout_seconds, 600.0)
        self.assertEqual(loaded.looper.sleep_seconds, 5.0)
        self.assertFalse(loaded.looper.fresh_session_per_loop)
        self.assertEqual(loaded.looper.max_loops, 15)
        self.assertEqual(loaded.looper.stop_patterns, ["rate limit", "overloaded"])
        self.assertFalse(loaded.looper.kill_on_stop_pattern)
        self.assertTrue(loaded.looper.ignore_nonzero)
        self.assertTrue(loaded.looper.scan_stdout_for_stop_patterns)
        self.assertEqual(loaded.agents["codex"].extra_args, ["--sandbox", "workspace-write"])
        self.assertEqual(loaded.agents["codex"].env["CODEX_HOME"], ".codex")
        self.assertEqual(loaded.agents["gemini"].first_command, ["gemini", "-p", "{prompt}"])
        self.assertTrue(loaded.agents["gemini"].scan_stdout_for_stop_patterns)

    def test_stop_signal_parsing_ignores_normal_stdout(self) -> None:
        patterns = self.looper.compile_stop_patterns([r"rate\s*limit"])

        parsed = self.looper.parse_output_line(
            line='{"type":"item.completed","text":"rate limit docs"}\n',
            stream="stdout",
            agent_kind="codex",
            patterns=patterns,
            scan_stdout=False,
        )

        self.assertIsNone(parsed.stop_reason)

    def test_stop_signal_parsing_detects_stderr_and_codex_thread_id(self) -> None:
        patterns = self.looper.compile_stop_patterns([r"rate\s*limit"])

        thread = self.looper.parse_output_line(
            line='{"type":"thread.started","thread_id":"thread-abc"}\n',
            stream="stdout",
            agent_kind="codex",
            patterns=patterns,
            scan_stdout=False,
        )
        stopped = self.looper.parse_output_line(
            line="Error: rate limit reached\n",
            stream="stderr",
            agent_kind="generic",
            patterns=patterns,
            scan_stdout=False,
        )

        self.assertEqual(thread.session_id, "thread-abc")
        self.assertIsNotNone(stopped.stop_reason)


class LooperCliTests(unittest.TestCase):
    def test_dry_run_prints_commands_without_running_agent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            prompt_file = workdir / "prompts.md"
            prompt_file.write_text("hello\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(LOOPER_PATH),
                    "--agent",
                    "codex",
                    "--prompt-file",
                    str(prompt_file),
                    "--label",
                    "smoke",
                    "--once",
                    "--dry-run",
                ],
                cwd=workdir,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agent: codex (codex)", result.stdout)
        self.assertIn("$ codex exec --json hello", result.stdout)
        self.assertIn("dry run complete", result.stdout)

    def test_init_writes_starter_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = subprocess.run(
                [sys.executable, str(LOOPER_PATH), "init", "--force"],
                cwd=td,
                text=True,
                capture_output=True,
                check=False,
            )
            config = Path(td) / "agent-looper.toml"
            prompts = Path(td) / "prompts.md"

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(config.exists())
            self.assertTrue(prompts.exists())
            self.assertIn("[looper]", config.read_text(encoding="utf-8"))

    def test_no_args_first_run_initializes_and_prints_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = subprocess.run(
                [sys.executable, str(LOOPER_PATH)],
                cwd=td,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(td) / "agent-looper.toml").exists())
            self.assertTrue((Path(td) / "prompts.md").exists())
            self.assertIn("Initialized Agent Looper", result.stdout)
            self.assertIn("Edit prompts.md", result.stdout)
            self.assertIn("codex-looper --once --label", result.stdout)

    def test_no_args_in_initialized_directory_prints_guidance_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "agent-looper.toml"
            prompts = Path(td) / "prompts.md"
            config.write_text("custom config\n", encoding="utf-8")
            prompts.write_text("custom prompt\n", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(LOOPER_PATH)],
                cwd=td,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_text(encoding="utf-8"), "custom config\n")
            self.assertEqual(prompts.read_text(encoding="utf-8"), "custom prompt\n")
            self.assertIn("Agent Looper is already initialized", result.stdout)
            self.assertIn("codex-looper --once --label", result.stdout)

    def test_interactive_init_writes_custom_prompts_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            answers = "\n".join(
                [
                    "claude",
                    "600",
                    "5",
                    "Summarize this repository.",
                    "Propose one safe cleanup.",
                    "",
                    "",
                ]
            )
            result = subprocess.run(
                [sys.executable, str(LOOPER_PATH), "init", "--interactive", "--force"],
                cwd=td,
                input=answers,
                text=True,
                capture_output=True,
                check=False,
            )
            config = (Path(td) / "agent-looper.toml").read_text(encoding="utf-8")
            prompts = (Path(td) / "prompts.md").read_text(encoding="utf-8")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('default_agent = "claude"', config)
            self.assertIn("timeout_seconds = 600", config)
            self.assertIn("sleep_seconds = 5", config)
            self.assertIn("Summarize this repository.", prompts)
            self.assertIn("---", prompts)
            self.assertIn("Propose one safe cleanup.", prompts)
            self.assertIn("Starter files are ready", result.stdout)

    def test_farm_session_launches_through_codex_add(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            project = tmpdir / "project"
            project.mkdir()
            log = tmpdir / "codex-add.log"
            codex_add = tmpdir / "codex-add"
            make_executable(
                codex_add,
                f"""#!/usr/bin/env bash
set -euo pipefail
{{
  echo "args=$*"
  echo "CODEX_NAME=${{CODEX_NAME:-}}"
  echo "CODEX_CMD=${{CODEX_CMD:-}}"
  echo "CODEX_ARGS=${{CODEX_ARGS:-}}"
}} >> {shlex.quote(str(log))}
""",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(LOOPER_PATH),
                    "--agent",
                    "codex",
                    "--farm-session",
                    "work",
                    "--farm-add-bin",
                    str(codex_add),
                    "--label",
                    "sweep",
                    "--cwd",
                    str(project),
                    "--once",
                    "--dry-run",
                ],
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertIn(f"args=-d work {project}", lines)
            self.assertIn("CODEX_NAME=sweep", lines)
            cmd_line = next(line for line in lines if line.startswith("CODEX_CMD="))
            args_line = next(line for line in lines if line.startswith("CODEX_ARGS="))
            self.assertTrue(cmd_line.endswith("codex-looper.py"), cmd_line)
            self.assertIn("--agent codex", args_line)
            self.assertIn("--label sweep", args_line)
            self.assertNotIn("--farm-session", args_line)
            self.assertNotIn("--farm-add-bin", args_line)


if __name__ == "__main__":
    unittest.main()
