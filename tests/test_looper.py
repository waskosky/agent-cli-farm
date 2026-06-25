import asyncio
import contextlib
import importlib.util
import io
import json
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


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def init_git_repo(repo: Path) -> None:
    run_git(repo, "init")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    run_git(repo, "add", "tracked.txt")
    run_git(repo, "commit", "-m", "initial")


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

    def test_looper_package_exports_core_symbols(self) -> None:
        import codex_looper

        self.assertEqual(codex_looper.LooperConfig().default_agent, "codex")
        self.assertTrue(callable(codex_looper.load_config))
        self.assertTrue(callable(codex_looper.run_command_main))

    def test_looper_package_exports_remaining_runtime_modules(self) -> None:
        import codex_looper.agents as agents
        import codex_looper.cli as cli
        import codex_looper.farm as farm
        import codex_looper.init as init
        import codex_looper.runner as runner

        self.assertTrue(callable(agents.build_command))
        self.assertTrue(callable(runner.run_loop))
        self.assertTrue(callable(init.write_starter_files))
        self.assertTrue(callable(farm.maybe_launch_farm))
        self.assertTrue(callable(cli.run_command_main))

    def test_single_mode_keeps_prompt_file_as_one_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "PROMPT.md"
            path.write_text("one\n---\ntwo\n", encoding="utf-8")

            self.assertEqual(
                self.looper.load_prompts_for_mode(path, r"^---\s*$", "single"),
                ["one\n---\ntwo"],
            )

    def test_sequence_mode_splits_prompt_file_on_separator_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "prompts.md"
            path.write_text("one\n---\ntwo\n", encoding="utf-8")

            self.assertEqual(
                self.looper.load_prompts_for_mode(path, r"^---\s*$", "sequence"),
                ["one", "two"],
            )

    def test_prompt_defaults_use_single_prompt_file_without_existing_sequence_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            resolved = self.looper.resolve_prompt_defaults(
                self.looper.LooperConfig(),
                cwd=Path(td),
            )

        self.assertEqual(resolved.mode, "single")
        self.assertEqual(resolved.prompt_file, Path("PROMPT.md"))

    def test_prompt_defaults_infer_sequence_when_only_legacy_prompts_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "prompts.md").write_text("legacy\n", encoding="utf-8")

            resolved = self.looper.resolve_prompt_defaults(
                self.looper.LooperConfig(),
                cwd=root,
            )

        self.assertEqual(resolved.mode, "sequence")
        self.assertEqual(resolved.prompt_file, Path("prompts.md"))

    def test_custom_prompt_file_defaults_to_single_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            resolved = self.looper.resolve_prompt_defaults(
                self.looper.LooperConfig(
                    prompt_file=Path("custom.md"),
                    prompt_file_explicit=True,
                ),
                cwd=Path(td),
            )

        self.assertEqual(resolved.mode, "single")
        self.assertEqual(resolved.prompt_file, Path("custom.md"))

    def test_float_argument_types_reject_nonfinite_values(self) -> None:
        for parser_type in [self.looper.positive_float, self.looper.nonnegative_float]:
            for value in ["nan", "inf", "-inf"]:
                with self.subTest(parser_type=parser_type.__name__, value=value):
                    with self.assertRaises(self.looper.argparse.ArgumentTypeError):
                        parser_type(value)

    def test_invalid_runtime_regexes_raise_config_errors(self) -> None:
        with self.assertRaisesRegex(self.looper.ConfigError, "invalid stop pattern"):
            self.looper.compile_stop_patterns(["["])

        with self.assertRaisesRegex(self.looper.ConfigError, "invalid completion marker"):
            self.looper.compile_completion_marker(
                self.looper.LooperConfig(completion_enabled=True, completion_marker="[")
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

    def test_builds_codex_command_with_model_and_effort_sugar(self) -> None:
        agent = self.looper.AgentConfig(
            name="codex",
            kind="codex",
            model="gpt-5.4",
            effort="high",
            extra_args=["--sandbox", "workspace-write"],
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

        command = self.looper.build_command(
            agent=agent,
            context=context,
            is_first_prompt_in_session=True,
        )

        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "--model",
                "gpt-5.4",
                "--effort",
                "high",
                "do it",
            ],
        )

    def test_builds_claude_command_with_model_and_effort_sugar(self) -> None:
        agent = self.looper.AgentConfig(
            name="claude",
            kind="claude",
            model="claude-opus-4-8",
            effort="max",
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

        command = self.looper.build_command(
            agent=agent,
            context=context,
            is_first_prompt_in_session=True,
        )

        self.assertEqual(
            command,
            [
                "claude",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                "claude-opus-4-8",
                "--effort",
                "max",
                "--name",
                "label-loop-0001",
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
                r"""
[looper]
default_agent = "claude"
prompt_file = "custom-prompts.md"
separator = "^---\\s*$"
timeout_seconds = 600
sleep_seconds = 5
fresh_session_per_loop = false
max_loops = 15
max_transient_retries = 4
retry_notify_after_seconds = 120
log_dir = ".agent-looper/runs"
stop_patterns = ["rate limit", "overloaded"]
kill_on_stop_pattern = false
ignore_nonzero = true
scan_stdout_for_stop_patterns = true

[agents.codex]
kind = "codex"
extra_args = ["--sandbox", "workspace-write"]
model = "gpt-5.4"
effort = "high"
env = { CODEX_HOME = ".codex" }

[agents.gemini]
kind = "generic"
first_command = ["gemini", "-p", "{prompt}"]
resume_command = ["gemini", "-p", "{prompt}"]
scan_stdout_for_stop_patterns = true
""",
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
        self.assertEqual(loaded.looper.max_transient_retries, 4)
        self.assertEqual(loaded.looper.retry_notify_after_seconds, 120.0)
        self.assertEqual(loaded.looper.stop_patterns, ["rate limit", "overloaded"])
        self.assertFalse(loaded.looper.kill_on_stop_pattern)
        self.assertTrue(loaded.looper.ignore_nonzero)
        self.assertTrue(loaded.looper.scan_stdout_for_stop_patterns)
        self.assertEqual(loaded.agents["codex"].extra_args, ["--sandbox", "workspace-write"])
        self.assertEqual(loaded.agents["codex"].model, "gpt-5.4")
        self.assertEqual(loaded.agents["codex"].effort, "high")
        self.assertEqual(loaded.agents["codex"].env["CODEX_HOME"], ".codex")
        self.assertEqual(loaded.agents["gemini"].first_command, ["gemini", "-p", "{prompt}"])
        self.assertTrue(loaded.agents["gemini"].scan_stdout_for_stop_patterns)

    def test_load_config_rejects_wrong_scalar_types_and_nonfinite_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "agent-looper.toml"
            path.write_text(
                """
[looper]
max_loops = "3"
timeout_seconds = inf
fresh_session_per_loop = "false"
""",
                encoding="utf-8",
            )

            with self.assertRaises(self.looper.ConfigError):
                self.looper.load_config(path)

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

    def test_stop_signal_parsing_ignores_claude_success_result_text(self) -> None:
        patterns = self.looper.compile_stop_patterns([r"rate[\s_-]*limit"])

        parsed = self.looper.parse_output_line(
            line=(
                '{"type":"result","subtype":"success","is_error":false,'
                '"api_error_status":null,'
                '"result":"The previous blocker mentioned rate_limit, but this run succeeded."}\n'
            ),
            stream="stdout",
            agent_kind="claude",
            patterns=patterns,
            scan_stdout=False,
        )

        self.assertIsNone(parsed.stop_reason)

    def test_stop_signal_parsing_ignores_allowed_rate_limit_event(self) -> None:
        patterns = self.looper.compile_stop_patterns([r"rate[\s_-]*limit"])

        parsed = self.looper.parse_output_line(
            line=(
                '{"type":"rate_limit_event",'
                '"rate_limit_info":{"status":"allowed","rateLimitType":"five_hour"}}\n'
            ),
            stream="stdout",
            agent_kind="claude",
            patterns=patterns,
            scan_stdout=False,
        )

        self.assertIsNone(parsed.stop_reason)

    def test_stop_signal_parsing_extracts_rate_limit_reset_delay(self) -> None:
        patterns = self.looper.compile_stop_patterns([r"rate[\s_-]*limit"])

        parsed = self.looper.parse_output_line(
            line=(
                '{"type":"rate_limit_event",'
                '"rate_limit_info":{"status":"rejected","resetsAt":1300}}\n'
            ),
            stream="stdout",
            agent_kind="claude",
            patterns=patterns,
            scan_stdout=False,
            now_epoch=1000.0,
        )

        self.assertEqual(parsed.stop_reason, "rate limit event: status=rejected")
        self.assertEqual(parsed.retry_after_seconds, 300.0)
        self.assertEqual(parsed.retry_kind, "rate_limit")

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

    def test_format_agent_log_line_renders_claude_text_event(self) -> None:
        line = (
            '[20260625T052249Z] stdout: {"type":"assistant","message":{"role":"assistant",'
            '"content":[{"type":"text","text":"Exactly my 5 files staged."}]}}\n'
        )

        self.assertEqual(
            self.looper.format_agent_log_line(line),
            "Exactly my 5 files staged.",
        )

    def test_format_agent_log_line_summarizes_tool_use_and_result(self) -> None:
        tool_use = (
            '[20260625T052302Z] stdout: {"type":"assistant","message":{"content":['
            '{"type":"tool_use","name":"Bash","input":{"command":"git status",'
            '"description":"Check status"}}]}}\n'
        )
        tool_result = (
            '[20260625T052302Z] stdout: {"type":"user","message":{"content":['
            '{"type":"tool_result","content":"On branch main","is_error":false}]}}\n'
        )

        self.assertEqual(
            self.looper.format_agent_log_line(tool_use),
            "tool: Bash - Check status\n$ git status",
        )
        self.assertEqual(
            self.looper.format_agent_log_line(tool_result),
            "tool result:\nOn branch main",
        )

    def test_format_agent_log_line_skips_opaque_thinking_events(self) -> None:
        system_line = (
            '[20260625T052405Z] stdout: {"type":"system","subtype":"thinking_tokens",'
            '"estimated_tokens":350}\n'
        )
        assistant_thinking = (
            '[20260625T052249Z] stdout: {"type":"assistant","message":{"content":['
            '{"type":"thinking","thinking":"","signature":"opaque"}]}}\n'
        )

        self.assertIsNone(self.looper.format_agent_log_line(system_line))
        self.assertIsNone(self.looper.format_agent_log_line(assistant_thinking))

    def test_transcript_log_cli_renders_human_readable_stdin(self) -> None:
        line = (
            '[20260625T052249Z] stdout: {"type":"assistant","message":{"role":"assistant",'
            '"content":[{"type":"text","text":"Readable note"}]}}\n'
        )

        result = subprocess.run(
            [sys.executable, str(LOOPER_PATH), "transcript-log"],
            input=line,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Readable note\n")

    def test_timeout_closes_subprocess_transport(self) -> None:
        async def exercise() -> tuple[object, object]:
            stdout = asyncio.StreamReader()
            stderr = asyncio.StreamReader()
            stdout.feed_eof()
            stderr.feed_eof()

            class FakeTransport:
                def __init__(self) -> None:
                    self.closed = False

                def close(self) -> None:
                    self.closed = True

            class FakeProcess:
                def __init__(self) -> None:
                    self.pid = 12345
                    self.returncode = None
                    self.stdout = stdout
                    self.stderr = stderr
                    self._transport = FakeTransport()
                    self._done = asyncio.Event()

                async def wait(self) -> int:
                    await self._done.wait()
                    return int(self.returncode or 0)

            process = FakeProcess()
            original_create = self.looper.asyncio.create_subprocess_exec
            original_terminate = self.looper._terminate_process_group

            async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
                return process

            async def fake_terminate_process_group(fake_process: FakeProcess) -> None:
                fake_process.returncode = -15
                fake_process._done.set()
                await fake_process.wait()

            self.looper.asyncio.create_subprocess_exec = fake_create_subprocess_exec
            self.looper._terminate_process_group = fake_terminate_process_group
            try:
                with tempfile.TemporaryDirectory() as td:
                    result = await self.looper.run_command(
                        command=["slow-agent"],
                        cwd=Path(td),
                        env={},
                        timeout_seconds=0.01,
                        log_path=Path(td) / "run.log",
                        agent_kind="generic",
                        patterns=[],
                        scan_stdout=False,
                        kill_on_stop_pattern=True,
                    )
            finally:
                self.looper.asyncio.create_subprocess_exec = original_create
                self.looper._terminate_process_group = original_terminate

            return result, process._transport

        result, transport = asyncio.run(exercise())

        self.assertTrue(result.timed_out)
        self.assertEqual(result.stop_reason, "local timeout after 0.01 seconds")
        self.assertTrue(transport.closed)

    def test_run_command_drains_jsonl_line_larger_than_asyncio_default_limit(self) -> None:
        async def exercise() -> tuple[object, str, int]:
            large_text = "x" * (70 * 1024)
            first_line = "{" + f'"type":"assistant","message":"{large_text}"' + "}\n"
            second_line = '{"type":"result","subtype":"success"}\n'
            script = (
                "import sys\n"
                f"sys.stdout.write({first_line!r})\n"
                f"sys.stdout.write({second_line!r})\n"
                "sys.stdout.flush()\n"
            )

            with tempfile.TemporaryDirectory() as td:
                log_path = Path(td) / "run.log"
                result = await self.looper.run_command(
                    command=[sys.executable, "-c", script],
                    cwd=Path(td),
                    env={},
                    timeout_seconds=1.0,
                    log_path=log_path,
                    agent_kind="claude",
                    patterns=[],
                    scan_stdout=False,
                    kill_on_stop_pattern=True,
                )
                return (
                    result,
                    log_path.read_text(encoding="utf-8"),
                    len(first_line) + len(second_line),
                )

        result, log_text, expected_bytes = asyncio.run(exercise())

        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.output_bytes, expected_bytes)
        self.assertIn('"message":"xxxxxxxxxx', log_text)
        self.assertIn('"type":"result"', log_text)

    def test_run_command_reports_reader_failure_instead_of_waiting_for_timeout(self) -> None:
        async def exercise() -> tuple[object, str]:
            stderr = asyncio.StreamReader()
            stderr.feed_eof()

            class FailingReader:
                async def read(self, size: int = -1) -> bytes:
                    raise RuntimeError("stream exploded")

                async def readline(self) -> bytes:
                    raise RuntimeError("stream exploded")

            class FakeProcess:
                def __init__(self) -> None:
                    self.pid = 12345
                    self.returncode = None
                    self.stdout = FailingReader()
                    self.stderr = stderr
                    self._done = asyncio.Event()

                async def wait(self) -> int:
                    await self._done.wait()
                    return int(self.returncode or 0)

            process = FakeProcess()
            original_create = self.looper.asyncio.create_subprocess_exec
            original_terminate = self.looper._terminate_process_group

            async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
                return process

            async def fake_terminate_process_group(fake_process: FakeProcess) -> None:
                fake_process.returncode = -15
                fake_process._done.set()
                await fake_process.wait()

            self.looper.asyncio.create_subprocess_exec = fake_create_subprocess_exec
            self.looper._terminate_process_group = fake_terminate_process_group
            try:
                with tempfile.TemporaryDirectory() as td:
                    log_path = Path(td) / "run.log"
                    result = await self.looper.run_command(
                        command=["failing-agent"],
                        cwd=Path(td),
                        env={},
                        timeout_seconds=1.0,
                        log_path=log_path,
                        agent_kind="generic",
                        patterns=[],
                        scan_stdout=False,
                        kill_on_stop_pattern=True,
                    )
                    return result, log_path.read_text(encoding="utf-8")
            finally:
                self.looper.asyncio.create_subprocess_exec = original_create
                self.looper._terminate_process_group = original_terminate

        result, log_text = asyncio.run(exercise())

        self.assertEqual(result.returncode, -15)
        self.assertFalse(result.timed_out)
        self.assertEqual(
            result.stop_reason, "local stdout reader failed: RuntimeError: stream exploded"
        )
        self.assertIn("stdout_reader_error", log_text)

    def test_run_command_reports_missing_executable_cleanly(self) -> None:
        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as td:
                await self.looper.run_command(
                    command=["/no/such/looper-agent"],
                    cwd=Path(td),
                    env={},
                    timeout_seconds=1.0,
                    log_path=Path(td) / "run.log",
                    agent_kind="generic",
                    patterns=[],
                    scan_stdout=False,
                    kill_on_stop_pattern=True,
                )

        with self.assertRaisesRegex(self.looper.ConfigError, "executable not found"):
            asyncio.run(exercise())

    def test_run_loop_retries_current_prompt_after_rate_limit_stop(self) -> None:
        async def exercise() -> tuple[int, list[list[str]], list[float], list[tuple[str, str]]]:
            calls: list[list[str]] = []
            sleeps: list[float] = []
            tmux_options: list[tuple[str, str]] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep
            original_set_tmux_window_option = self.looper.set_tmux_window_option

            async def fake_run_command(**kwargs: object) -> object:
                calls.append(list(kwargs["command"]))  # type: ignore[index]
                if len(calls) == 1:
                    return self.looper.ProcessResult(
                        returncode=1,
                        stop_reason="rate limit event: status=rejected",
                        retry_after_seconds=42.0,
                        retry_kind="rate_limit",
                    )
                return self.looper.ProcessResult(returncode=0)

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            def fake_set_tmux_window_option(name: str, value: str) -> None:
                tmux_options.append((name, value))

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            self.looper.set_tmux_window_option = fake_set_tmux_window_option
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "prompts.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        prompt_file=prompt_file,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        max_loops=1,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="retry-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep
                self.looper.set_tmux_window_option = original_set_tmux_window_option

            return result, calls, sleeps, tmux_options

        result, calls, sleeps, tmux_options = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(calls, [["agent", "hello"], ["agent", "hello"]])
        self.assertEqual(sleeps, [42.0])
        self.assertIn((self.looper.TMUX_STATE_OPTION, "RUN"), tmux_options)
        self.assertIn(
            (
                self.looper.TMUX_STOP_REASON_OPTION,
                "retrying rate_limit attempt 1; next in 42s: rate limit event: status=rejected",
            ),
            tmux_options,
        )

    def test_run_loop_does_not_rename_tmux_window_to_label(self) -> None:
        async def exercise() -> list[list[str]]:
            tmux_commands: list[list[str]] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep
            original_subprocess_run = self.looper.subprocess.run
            original_shutil_which = self.looper.shutil.which
            original_tmux = self.looper.os.environ.get("TMUX")

            async def fake_run_command(**kwargs: object) -> object:
                return self.looper.ProcessResult(returncode=0)

            async def fake_sleep(seconds: float) -> None:
                return None

            def fake_which(name: str):
                if name == "tmux":
                    return "tmux"
                return original_shutil_which(name)

            def fake_subprocess_run(command: list[str], **kwargs: object) -> object:
                if command and command[0] == "tmux":
                    tmux_commands.append(command)
                return self.looper.subprocess.CompletedProcess(command, 0)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            self.looper.shutil.which = fake_which
            self.looper.subprocess.run = fake_subprocess_run
            self.looper.os.environ["TMUX"] = "tmux-session"
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "PROMPT.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        prompt_file=prompt_file,
                        log_dir=root / "runs",
                        max_loops=1,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="window-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep
                self.looper.shutil.which = original_shutil_which
                self.looper.subprocess.run = original_subprocess_run
                if original_tmux is None:
                    self.looper.os.environ.pop("TMUX", None)
                else:
                    self.looper.os.environ["TMUX"] = original_tmux

            self.assertEqual(result, 0)
            return tmux_commands

        tmux_commands = asyncio.run(exercise())

        self.assertNotIn(["tmux", "rename-window", "window-smoke"], tmux_commands)
        self.assertFalse(
            any(command[:2] == ["tmux", "split-window"] for command in tmux_commands),
            tmux_commands,
        )

    def test_run_loop_split_layout_opens_tail_pane_and_tracks_current_log(self) -> None:
        async def exercise() -> tuple[int, list[list[str]], list[dict[str, object]], str]:
            tmux_commands: list[list[str]] = []
            run_command_calls: list[dict[str, object]] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep
            original_subprocess_run = self.looper.subprocess.run
            original_shutil_which = self.looper.shutil.which
            original_tmux = self.looper.os.environ.get("TMUX")

            async def fake_run_command(**kwargs: object) -> object:
                run_command_calls.append(kwargs)
                return self.looper.ProcessResult(returncode=0)

            async def fake_sleep(seconds: float) -> None:
                return None

            def fake_which(name: str):
                if name == "tmux":
                    return "tmux"
                return original_shutil_which(name)

            def fake_subprocess_run(command: list[str], **kwargs: object) -> object:
                if command and command[0] == "tmux":
                    tmux_commands.append(command)
                return self.looper.subprocess.CompletedProcess(command, 0)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            self.looper.shutil.which = fake_which
            self.looper.subprocess.run = fake_subprocess_run
            self.looper.os.environ["TMUX"] = "tmux-session"
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "PROMPT.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        prompt_file=prompt_file,
                        log_dir=root / "runs",
                        max_loops=1,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="split-smoke",
                        tmux_layout="split",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
                    pointer_path = next((root / "runs").glob("*/current-log.path"))
                    pointer_text = pointer_path.read_text(encoding="utf-8")
                    return result, tmux_commands, run_command_calls, pointer_text
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep
                self.looper.shutil.which = original_shutil_which
                self.looper.subprocess.run = original_subprocess_run
                if original_tmux is None:
                    self.looper.os.environ.pop("TMUX", None)
                else:
                    self.looper.os.environ["TMUX"] = original_tmux

        result, tmux_commands, run_command_calls, pointer_text = asyncio.run(exercise())

        self.assertEqual(result, 0)
        split_commands = [
            command for command in tmux_commands if command[:2] == ["tmux", "split-window"]
        ]
        self.assertEqual(len(split_commands), 1, tmux_commands)
        self.assertIn("-d", split_commands[0])
        self.assertIn("current-log.path", split_commands[0][-1])
        self.assertIn("tail -n +1 -F", split_commands[0][-1])
        self.assertIn("transcript-log", split_commands[0][-1])
        self.assertIn("loop-0001__prompt-001.log", pointer_text)
        self.assertEqual(len(run_command_calls), 1)
        self.assertIn("stream_output", run_command_calls[0])
        self.assertFalse(run_command_calls[0]["stream_output"])

    def test_run_loop_writes_durable_state_and_event_history(self) -> None:
        async def exercise() -> tuple[int, dict[str, object], list[dict[str, object]]]:
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep

            async def fake_run_command(**kwargs: object) -> object:
                return self.looper.ProcessResult(
                    returncode=0,
                    session_id="session-abc",
                    output_bytes=123,
                )

            async def fake_sleep(seconds: float) -> None:
                return None

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "PROMPT.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        prompt_file=prompt_file,
                        log_dir=root / ".agent-looper" / "runs",
                        max_loops=1,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="state-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
                    run_dir = next((root / ".agent-looper" / "runs").glob("*state-smoke*"))
                    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
                    events = [
                        json.loads(line)
                        for line in (
                            run_dir / "events.jsonl"
                        ).read_text(encoding="utf-8").splitlines()
                    ]
                    return result, state, events
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep

        result, state, events = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["label"], "state-smoke")
        self.assertEqual(state["agent_name"], "generic")
        self.assertEqual(state["agent_kind"], "generic")
        self.assertEqual(state["current_loop"], 1)
        self.assertEqual(state["current_prompt_index"], 1)
        self.assertEqual(state["current_session_id"], "session-abc")
        self.assertEqual(state["last_output_bytes"], 123)
        self.assertEqual(state["exit_code"], 0)
        self.assertEqual(state["stop_reason"], "max loops reached: 1")
        self.assertTrue(str(state["last_log"]).endswith("loop-0001__prompt-001.log"))
        self.assertEqual(
            [event["event"] for event in events],
            [
                "run_started",
                "loop_started",
                "prompt_started",
                "prompt_completed",
                "loop_completed",
                "run_stopped",
            ],
        )
        self.assertEqual(events[-1]["status"], "stopped")
        self.assertEqual(events[-1]["stop_reason"], "max loops reached: 1")

    def test_run_loop_streams_in_supervisor_when_split_pane_fails(self) -> None:
        async def exercise() -> list[dict[str, object]]:
            run_command_calls: list[dict[str, object]] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep
            original_subprocess_run = self.looper.subprocess.run
            original_shutil_which = self.looper.shutil.which
            original_tmux = self.looper.os.environ.get("TMUX")

            async def fake_run_command(**kwargs: object) -> object:
                run_command_calls.append(kwargs)
                return self.looper.ProcessResult(returncode=0)

            async def fake_sleep(seconds: float) -> None:
                return None

            def fake_which(name: str):
                if name == "tmux":
                    return "tmux"
                return original_shutil_which(name)

            def fake_subprocess_run(command: list[str], **kwargs: object) -> object:
                if command and command[0] == "tmux" and command[1] == "split-window":
                    return self.looper.subprocess.CompletedProcess(command, 1)
                return self.looper.subprocess.CompletedProcess(command, 0)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            self.looper.shutil.which = fake_which
            self.looper.subprocess.run = fake_subprocess_run
            self.looper.os.environ["TMUX"] = "tmux-session"
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "PROMPT.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        prompt_file=prompt_file,
                        log_dir=root / "runs",
                        max_loops=1,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="split-fail-smoke",
                        tmux_layout="split",
                    )
                    await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep
                self.looper.shutil.which = original_shutil_which
                self.looper.subprocess.run = original_subprocess_run
                if original_tmux is None:
                    self.looper.os.environ.pop("TMUX", None)
                else:
                    self.looper.os.environ["TMUX"] = original_tmux

            return run_command_calls

        calls = asyncio.run(exercise())
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["stream_output"])

    def test_prune_backup_branches_does_not_cross_prefix_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            run_git(repo, "branch", "looper-backup/old")
            run_git(repo, "branch", "looper-backup/new")
            run_git(repo, "branch", "looper-backup-old/keep")

            pruned = self.looper.prune_backup_branches(repo, prefix="looper-backup", keep=1)

            branches = run_git(repo, "branch", "--format=%(refname:short)").stdout.splitlines()
        self.assertEqual(pruned, ["looper-backup/new"])
        self.assertIn("looper-backup-old/keep", branches)

    def test_git_workspace_fingerprint_detects_repeated_edits_to_dirty_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            tracked = repo / "tracked.txt"
            tracked.write_text("first dirty edit\n", encoding="utf-8")
            first = self.looper.git_workspace_fingerprint(repo)
            tracked.write_text("second dirty edit\n", encoding="utf-8")
            second = self.looper.git_workspace_fingerprint(repo)

        self.assertNotEqual(first, second)

    def test_make_run_dir_is_unique_for_same_label(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td)
            first = self.looper.make_run_dir(log_dir, "same-label")
            second = self.looper.make_run_dir(log_dir, "same-label")

        self.assertNotEqual(first, second)

    def test_doctor_checks_only_selected_agent_in_local_mode(self) -> None:
        calls: list[str] = []
        original_which = self.looper.shutil.which

        def fake_which(name: str) -> str | None:
            calls.append(name)
            if name == "claude":
                return f"/fake/{name}"
            return None

        self.looper.shutil.which = fake_which
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = self.looper.doctor_main(["--agent", "claude", "--local"])
        finally:
            self.looper.shutil.which = original_which

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["claude"])

    def test_top_level_help_explains_optional_run_subcommand(self) -> None:
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            result = self.looper.main(["--help"])

        self.assertEqual(result, 0)
        self.assertIn("run subcommand is optional", stdout.getvalue())

    def test_missing_farm_launcher_has_clean_error(self) -> None:
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stderr(stderr):
            result = self.looper.run_command_main(
                [
                    "--farm-session",
                    "work",
                    "--farm-add-bin",
                    str(Path(td) / "missing-codex-add"),
                    "--cwd",
                    td,
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("looper error: farm launcher not found", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_named_preset_honors_xdg_config_home(self) -> None:
        original_xdg = os.environ.get("XDG_CONFIG_HOME")
        try:
            with tempfile.TemporaryDirectory() as td:
                preset = Path(td) / "codexfarm" / "presets" / "custom.toml"
                preset.parent.mkdir(parents=True)
                preset.write_text("[looper]\nmax_loops = 1\n", encoding="utf-8")
                os.environ["XDG_CONFIG_HOME"] = td

                self.assertEqual(self.looper.resolve_preset_path("custom"), preset)
        finally:
            if original_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = original_xdg

    def test_run_loop_notifies_for_long_retry_delay(self) -> None:
        async def exercise() -> list[str]:
            notifications: list[str] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep
            original_display_tmux_message = self.looper.display_tmux_message

            calls = 0

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return self.looper.ProcessResult(
                        returncode=1,
                        stop_reason="rate limit event: status=rejected",
                        retry_after_seconds=600.0,
                        retry_kind="rate_limit",
                    )
                return self.looper.ProcessResult(returncode=0)

            async def fake_sleep(seconds: float) -> None:
                return None

            def fake_display_tmux_message(message: str) -> None:
                notifications.append(message)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            self.looper.display_tmux_message = fake_display_tmux_message
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "prompts.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        prompt_file=prompt_file,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        max_loops=1,
                        retry_notify_after_seconds=300.0,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="notify-smoke",
                    )

                    await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep
                self.looper.display_tmux_message = original_display_tmux_message

            return notifications

        notifications = asyncio.run(exercise())

        self.assertEqual(
            notifications,
            [
                "Looper retry wait: retrying rate_limit attempt 1; next in 10m: "
                "rate limit event: status=rejected"
            ],
        )

    def test_run_loop_does_not_notify_for_short_retry_delay(self) -> None:
        async def exercise() -> list[str]:
            notifications: list[str] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep
            original_display_tmux_message = self.looper.display_tmux_message

            calls = 0

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return self.looper.ProcessResult(
                        returncode=1,
                        stop_reason="rate limit event: status=rejected",
                        retry_after_seconds=30.0,
                        retry_kind="rate_limit",
                    )
                return self.looper.ProcessResult(returncode=0)

            async def fake_sleep(seconds: float) -> None:
                return None

            def fake_display_tmux_message(message: str) -> None:
                notifications.append(message)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            self.looper.display_tmux_message = fake_display_tmux_message
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "prompts.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        prompt_file=prompt_file,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        max_loops=1,
                        retry_notify_after_seconds=300.0,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="quiet-smoke",
                    )

                    await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep
                self.looper.display_tmux_message = original_display_tmux_message

            return notifications

        self.assertEqual(asyncio.run(exercise()), [])

    def test_run_loop_caps_transient_retries(self) -> None:
        async def exercise() -> tuple[int, int, list[float], list[tuple[str, str]]]:
            calls = 0
            sleeps: list[float] = []
            tmux_options: list[tuple[str, str]] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep
            original_set_tmux_window_option = self.looper.set_tmux_window_option

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return self.looper.ProcessResult(
                    returncode=1,
                    stop_reason="stop pattern detected in stderr: 'overloaded'",
                    retry_kind="transient",
                )

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            def fake_set_tmux_window_option(name: str, value: str) -> None:
                tmux_options.append((name, value))

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            self.looper.set_tmux_window_option = fake_set_tmux_window_option
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "prompts.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        prompt_file=prompt_file,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        max_loops=1,
                        max_transient_retries=2,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="transient-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep
                self.looper.set_tmux_window_option = original_set_tmux_window_option

            return result, calls, sleeps, tmux_options

        result, calls, sleeps, tmux_options = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(calls, 3)
        self.assertEqual(sleeps, [0.25, 0.25])
        self.assertIn(
            (
                self.looper.TMUX_STOP_REASON_OPTION,
                "transient retry limit reached after 2 attempts: stop pattern detected in stderr: 'overloaded'",
            ),
            tmux_options,
        )

    def test_run_loop_does_not_cap_rate_limit_retries(self) -> None:
        async def exercise() -> tuple[int, int, list[float]]:
            calls = 0
            sleeps: list[float] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if calls < 3:
                    return self.looper.ProcessResult(
                        returncode=1,
                        stop_reason="rate limit event: status=rejected",
                        retry_kind="rate_limit",
                    )
                return self.looper.ProcessResult(returncode=0)

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "prompts.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        prompt_file=prompt_file,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        max_loops=1,
                        max_transient_retries=1,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="rate-limit-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep

            return result, calls, sleeps

        result, calls, sleeps = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(calls, 3)
        self.assertEqual(sleeps, [0.25, 0.25])

    def test_run_loop_stops_after_completion_marker(self) -> None:
        async def exercise() -> tuple[int, int, list[float]]:
            calls = 0
            sleeps: list[float] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return self.looper.ProcessResult(returncode=0, completion_detected=True)

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "PROMPT.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        mode="single",
                        mode_explicit=True,
                        prompt_file=prompt_file,
                        prompt_file_explicit=True,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        completion_enabled=True,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="complete-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep

            return result, calls, sleeps

        result, calls, sleeps = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(sleeps, [])

    def test_run_loop_requires_completion_streak(self) -> None:
        async def exercise() -> tuple[int, int, list[float]]:
            calls = 0
            sleeps: list[float] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return self.looper.ProcessResult(returncode=0, completion_detected=True)

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "PROMPT.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        mode="single",
                        mode_explicit=True,
                        prompt_file=prompt_file,
                        prompt_file_explicit=True,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        completion_enabled=True,
                        completion_streak=2,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="streak-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep

            return result, calls, sleeps

        result, calls, sleeps = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [0.25])

    def test_run_loop_resets_completion_streak_when_marker_is_missing(self) -> None:
        async def exercise() -> tuple[int, int, list[float]]:
            detections = [True, False, True, True]
            calls = 0
            sleeps: list[float] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return self.looper.ProcessResult(
                    returncode=0,
                    completion_detected=detections[calls - 1],
                )

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "PROMPT.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        mode="single",
                        mode_explicit=True,
                        prompt_file=prompt_file,
                        prompt_file_explicit=True,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        completion_enabled=True,
                        completion_streak=2,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="reset-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep

            return result, calls, sleeps

        result, calls, sleeps = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(calls, 4)
        self.assertEqual(sleeps, [0.25, 0.25, 0.25])

    def test_markdown_plan_has_unchecked_tasks(self) -> None:
        self.assertTrue(self.looper.markdown_plan_has_unchecked_tasks("notes\n- [ ] finish this\n"))
        self.assertTrue(self.looper.markdown_plan_has_unchecked_tasks("  - [ ] indented\n"))
        self.assertFalse(
            self.looper.markdown_plan_has_unchecked_tasks("- [x] done\n- [X] done too\n")
        )
        self.assertFalse(self.looper.markdown_plan_has_unchecked_tasks("ordinary text\n"))

    def test_completion_marker_does_not_stop_when_plan_file_has_unchecked_tasks(self) -> None:
        async def exercise() -> tuple[int, int, list[float]]:
            calls = 0
            sleeps: list[float] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return self.looper.ProcessResult(returncode=0, completion_detected=True)

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "PROMPT.md"
                    plan_file = root / "fix_plan.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    plan_file.write_text("- [ ] remaining\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        mode="single",
                        mode_explicit=True,
                        prompt_file=prompt_file,
                        prompt_file_explicit=True,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        max_loops=2,
                        completion_enabled=True,
                        plan_file=plan_file,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="incomplete-plan-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep

            return result, calls, sleeps

        result, calls, sleeps = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [0.25])

    def test_completion_marker_stops_when_plan_file_is_complete(self) -> None:
        async def exercise() -> tuple[int, int, list[float]]:
            calls = 0
            sleeps: list[float] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return self.looper.ProcessResult(returncode=0, completion_detected=True)

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "PROMPT.md"
                    plan_file = root / "fix_plan.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    plan_file.write_text("- [x] done\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        mode="single",
                        mode_explicit=True,
                        prompt_file=prompt_file,
                        prompt_file_explicit=True,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        completion_enabled=True,
                        plan_file=plan_file,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="complete-plan-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep

            return result, calls, sleeps

        result, calls, sleeps = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(sleeps, [])

    def test_create_backup_branch_uses_configured_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)

            branch = self.looper.create_backup_branch(
                repo,
                prefix="looper-backup",
                loop_number=3,
                stamp="20260622T000000Z",
            )

            branches = run_git(repo, "branch", "--list", branch).stdout.strip()

        self.assertEqual(branch, "looper-backup/20260622T000000Z-loop-0003")
        self.assertEqual(branches, branch)

    def test_prune_backup_branches_keeps_newest_names(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            names = [
                "looper-backup/20260622T000001Z-loop-0001",
                "looper-backup/20260622T000002Z-loop-0002",
                "looper-backup/20260622T000003Z-loop-0003",
            ]
            for name in names:
                run_git(repo, "branch", name)

            removed = self.looper.prune_backup_branches(repo, prefix="looper-backup", keep=2)
            remaining = run_git(
                repo,
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads/looper-backup",
            ).stdout.splitlines()

        self.assertEqual(removed, ["looper-backup/20260622T000001Z-loop-0001"])
        self.assertEqual(
            remaining,
            [
                "looper-backup/20260622T000002Z-loop-0002",
                "looper-backup/20260622T000003Z-loop-0003",
            ],
        )

    def test_git_workspace_fingerprint_changes_for_content_and_head(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)

            clean = self.looper.git_workspace_fingerprint(repo)
            (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
            dirty = self.looper.git_workspace_fingerprint(repo)
            run_git(repo, "add", "tracked.txt")
            run_git(repo, "commit", "-m", "change")
            committed = self.looper.git_workspace_fingerprint(repo)

        self.assertNotEqual(clean, dirty)
        self.assertNotEqual(dirty, committed)
        self.assertNotEqual(clean, committed)

    def test_git_workspace_fingerprint_ignores_absolute_resolved_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            log_dir = repo / "runs"
            run_dir = log_dir / "abc"
            run_dir.mkdir(parents=True)
            ignored = [log_dir.resolve(), run_dir.resolve()]

            before = self.looper.git_workspace_fingerprint(repo, ignored_paths=ignored)
            (run_dir / "state.json").write_text("{}\n", encoding="utf-8")
            (run_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
            after = self.looper.git_workspace_fingerprint(repo, ignored_paths=ignored)

        self.assertEqual(before, after)

    def test_run_loop_stops_after_configured_no_progress_loops(self) -> None:
        async def exercise() -> tuple[int, int, list[float]]:
            calls = 0
            sleeps: list[float] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return self.looper.ProcessResult(returncode=0)

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    init_git_repo(root)
                    prompt_file = root / "PROMPT.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        mode="single",
                        mode_explicit=True,
                        prompt_file=prompt_file,
                        prompt_file_explicit=True,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        cb_no_progress=2,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="no-progress-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep

            return result, calls, sleeps

        result, calls, sleeps = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [0.25])

    def test_format_loop_metrics_includes_loop_duration_and_output(self) -> None:
        self.assertEqual(
            self.looper.format_loop_metrics(
                loop_number=3, duration_seconds=1.25, output_bytes=1536
            ),
            "loop metrics: loop=3 duration=1.25s output=1.5KiB",
        )

    def test_run_loop_stops_after_configured_output_declines(self) -> None:
        async def exercise() -> tuple[int, int, list[float]]:
            outputs = [100, 80, 70]
            calls = 0
            sleeps: list[float] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return self.looper.ProcessResult(returncode=0, output_bytes=outputs[calls - 1])

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "PROMPT.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        mode="single",
                        mode_explicit=True,
                        prompt_file=prompt_file,
                        prompt_file_explicit=True,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        cb_output_decline=2,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="output-decline-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep

            return result, calls, sleeps

        result, calls, sleeps = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(calls, 3)
        self.assertEqual(sleeps, [0.25, 0.25])

    def test_output_decline_counter_resets_when_output_recovers(self) -> None:
        async def exercise() -> tuple[int, int, list[float]]:
            outputs = [100, 80, 90, 70, 60]
            calls = 0
            sleeps: list[float] = []
            original_run_command = self.looper.run_command
            original_sleep = self.looper.asyncio.sleep

            async def fake_run_command(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                return self.looper.ProcessResult(returncode=0, output_bytes=outputs[calls - 1])

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            self.looper.run_command = fake_run_command
            self.looper.asyncio.sleep = fake_sleep
            try:
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    prompt_file = root / "PROMPT.md"
                    prompt_file.write_text("hello\n", encoding="utf-8")
                    agent = self.looper.AgentConfig(
                        name="generic",
                        kind="generic",
                        cwd=root,
                        first_command=["agent", "{prompt}"],
                    )
                    looper = self.looper.LooperConfig(
                        mode="single",
                        mode_explicit=True,
                        prompt_file=prompt_file,
                        prompt_file_explicit=True,
                        log_dir=root / "runs",
                        sleep_seconds=0.25,
                        cb_output_decline=2,
                    )
                    options = self.looper.RunOptions(
                        agent_name="generic",
                        config_path=root / "agent-looper.toml",
                        label="output-recovery-smoke",
                    )

                    result = await self.looper.run_loop(agent=agent, looper=looper, options=options)
            finally:
                self.looper.run_command = original_run_command
                self.looper.asyncio.sleep = original_sleep

            return result, calls, sleeps

        result, calls, sleeps = asyncio.run(exercise())

        self.assertEqual(result, 0)
        self.assertEqual(calls, 5)
        self.assertEqual(sleeps, [0.25, 0.25, 0.25, 0.25])


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

    def test_cb_output_decline_flag_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            prompt_file = workdir / "PROMPT.md"
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
                    "decline-smoke",
                    "--cb-output-decline",
                    "2",
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
        self.assertIn("dry run complete", result.stdout)

    def test_version_marks_farm_default_looper_release(self) -> None:
        result = subprocess.run(
            [sys.executable, str(LOOPER_PATH), "--version"],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "codex-looper 0.3.1")

    def test_default_label_uses_looper_short_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            prompt_file = workdir / "prompts.md"
            prompt_file.write_text("hello\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(LOOPER_PATH),
                    "--local",
                    "--agent",
                    "claude",
                    "--prompt-file",
                    str(prompt_file),
                    "--once",
                    "--dry-run",
                ],
                cwd=workdir,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"label: Looper_[0-9a-f]{6}\n")
        self.assertRegex(result.stdout, r"--name Looper_[0-9a-f]{6}-loop-0001 hello")

    def test_double_dash_arguments_are_passed_to_builtin_agent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            prompt_file = workdir / "prompts.md"
            prompt_file.write_text("hello\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(LOOPER_PATH),
                    "--agent",
                    "claude",
                    "--prompt-file",
                    str(prompt_file),
                    "--label",
                    "smoke",
                    "--once",
                    "--dry-run",
                    "--",
                    "--dangerously-skip-permissions",
                    "--max-turns",
                    "20",
                ],
                cwd=workdir,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agent: claude (claude)", result.stdout)
        self.assertIn(
            "$ claude -p --output-format stream-json --verbose "
            "--dangerously-skip-permissions --max-turns 20 --name smoke-loop-0001 hello",
            result.stdout,
        )

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
            prompt = Path(td) / "PROMPT.md"

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(config.exists())
            self.assertTrue(prompt.exists())
            config_text = config.read_text(encoding="utf-8")
            self.assertIn("[looper]", config_text)
            self.assertIn('mode = "single"', config_text)
            self.assertIn('prompt_file = "PROMPT.md"', config_text)
            self.assertIn("timeout_seconds = 7200", config_text)
            self.assertIn("max_transient_retries = 12", config_text)
            self.assertIn("retry_notify_after_seconds = 300", config_text)
            self.assertIn("completion_enabled = false", config_text)
            self.assertIn('plan_file = ""', config_text)
            self.assertIn("backup_enabled = false", config_text)
            self.assertIn("cb_no_progress = 0", config_text)
            self.assertIn("cb_output_decline = 0", config_text)

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
            self.assertTrue((Path(td) / "PROMPT.md").exists())
            self.assertIn("Initialized Agent Looper", result.stdout)
            self.assertIn("Edit PROMPT.md", result.stdout)
            self.assertIn("Run in the default tmux farm: codex-looper", result.stdout)

    def test_no_args_in_initialized_directory_launches_default_farm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            codex_add = tmpdir / "codex-add"
            log = tmpdir / "codex-add.log"
            make_executable(
                codex_add,
                f"""#!/usr/bin/env bash
set -euo pipefail
{{
  echo "args=$*"
  echo "CODEX_NAME=${{CODEX_NAME:-}}"
  echo "CODEX_ARGS=${{CODEX_ARGS:-}}"
}} >> {shlex.quote(str(log))}
""",
            )
            config = Path(td) / "agent-looper.toml"
            prompt = Path(td) / "PROMPT.md"
            config.write_text("custom config\n", encoding="utf-8")
            prompt.write_text("custom prompt\n", encoding="utf-8")
            env = os.environ.copy()
            env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"

            result = subprocess.run(
                [sys.executable, str(LOOPER_PATH)],
                cwd=td,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_text(encoding="utf-8"), "custom config\n")
            self.assertEqual(prompt.read_text(encoding="utf-8"), "custom prompt\n")
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertIn(f"args=-d {tmpdir.resolve()}", lines)
            name_line = next(line for line in lines if line.startswith("CODEX_NAME="))
            args_line = next(line for line in lines if line.startswith("CODEX_ARGS="))
            self.assertEqual(name_line, f"CODEX_NAME={tmpdir.name}")
            self.assertRegex(args_line, r"--label Looper_[0-9a-f]{6}(?: |$)")
            self.assertIn("--local", args_line)
            self.assertNotIn("--farm-session", args_line)

    def test_interactive_init_writes_custom_prompts_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            answers = "\n".join(
                [
                    "claude",
                    "600",
                    "5",
                    "12",
                    "4",
                    "180",
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
            self.assertIn('mode = "sequence"', config)
            self.assertIn('prompt_file = "prompts.md"', config)
            self.assertIn("timeout_seconds = 600", config)
            self.assertIn("sleep_seconds = 5", config)
            self.assertIn("max_loops = 12", config)
            self.assertIn("max_transient_retries = 4", config)
            self.assertIn("retry_notify_after_seconds = 180", config)
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
  echo "CODEX_LOOPER_LAYOUT=${{CODEX_LOOPER_LAYOUT:-}}"
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
            self.assertIn("CODEX_NAME=project", lines)
            self.assertIn("CODEX_LOOPER_LAYOUT=split", lines)
            cmd_line = next(line for line in lines if line.startswith("CODEX_CMD="))
            args_line = next(line for line in lines if line.startswith("CODEX_ARGS="))
            self.assertEqual(cmd_line, f"CODEX_CMD={LOOPER_PATH.resolve()}")
            self.assertIn("--agent codex", args_line)
            self.assertIn("--label sweep", args_line)
            self.assertIn("--tmux-layout split", args_line)
            self.assertNotIn("--farm-session", args_line)
            self.assertNotIn("--farm-add-bin", args_line)

    def test_farm_session_without_value_uses_default_session(self) -> None:
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
  echo "CODEX_ARGS=${{CODEX_ARGS:-}}"
}} >> {shlex.quote(str(log))}
""",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(LOOPER_PATH),
                    "--agent",
                    "claude",
                    "--farm-session",
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
            self.assertIn(f"args=-d {project}", lines)
            args_line = next(line for line in lines if line.startswith("CODEX_ARGS="))
            self.assertIn("--agent claude", args_line)
            self.assertIn("--label sweep", args_line)
            self.assertNotIn("--farm-session", args_line)
            self.assertNotIn("--farm-add-bin", args_line)

    def test_farm_session_preserves_agent_passthrough_arguments(self) -> None:
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
echo "CODEX_ARGS=${{CODEX_ARGS:-}}" >> {shlex.quote(str(log))}
""",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(LOOPER_PATH),
                    "--agent",
                    "claude",
                    "--farm-session",
                    "work",
                    "--farm-add-bin",
                    str(codex_add),
                    "--label",
                    "sweep",
                    "--cwd",
                    str(project),
                    "--once",
                    "--",
                    "--dangerously-skip-permissions",
                ],
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            args_line = log.read_text(encoding="utf-8").strip()
            self.assertIn("--agent claude", args_line)
            self.assertIn("-- --dangerously-skip-permissions", args_line)
            self.assertNotIn("--farm-session", args_line)
            self.assertNotIn("--farm-add-bin", args_line)

    def test_preset_path_loads_looper_and_agent_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            preset = workdir / "custom-preset.toml"
            preset.write_text(
                """
[looper]
default_agent = "claude"
mode = "single"
prompt_file = "PROMPT.md"
completion_enabled = true
completion_marker = "DONE"

[agents.claude]
kind = "claude"
extra_args = ["--max-turns", "3"]
""",
                encoding="utf-8",
            )
            (workdir / "PROMPT.md").write_text("hello\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(LOOPER_PATH),
                    "--local",
                    "--preset",
                    str(preset),
                    "--once",
                    "--dry-run",
                ],
                cwd=workdir,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agent: claude (claude)", result.stdout)
        self.assertIn("mode: single", result.stdout)
        self.assertIn("--max-turns 3 --name", result.stdout)

    def test_named_rai_preset_resolves_repo_example(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            (workdir / "PROMPT.md").write_text("hello\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(LOOPER_PATH),
                    "--local",
                    "--preset",
                    "rai",
                    "--once",
                    "--dry-run",
                ],
                cwd=workdir,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agent: claude (claude)", result.stdout)
        self.assertIn("mode: single", result.stdout)
        self.assertIn("--dangerously-skip-permissions", result.stdout)

    def test_cli_flags_override_preset_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            preset = workdir / "custom-preset.toml"
            preset.write_text(
                """
[looper]
default_agent = "claude"
mode = "single"
prompt_file = "PROMPT.md"

[agents.claude]
kind = "claude"
extra_args = ["--max-turns", "3"]
""",
                encoding="utf-8",
            )
            (workdir / "PROMPT.md").write_text("single prompt\n", encoding="utf-8")
            (workdir / "prompts.md").write_text("sequence prompt\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(LOOPER_PATH),
                    "--local",
                    "--preset",
                    str(preset),
                    "--agent",
                    "codex",
                    "--mode",
                    "sequence",
                    "--prompt-file",
                    "prompts.md",
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
        self.assertIn("mode: sequence", result.stdout)
        self.assertIn("$ codex exec --json 'sequence prompt'", result.stdout)


if __name__ == "__main__":
    unittest.main()
