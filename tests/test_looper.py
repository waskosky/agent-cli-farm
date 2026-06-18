import asyncio
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
        self.assertEqual(loaded.looper.max_transient_retries, 4)
        self.assertEqual(loaded.looper.retry_notify_after_seconds, 120.0)
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

    def test_version_marks_farm_default_looper_release(self) -> None:
        result = subprocess.run(
            [sys.executable, str(LOOPER_PATH), "--version"],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "codex-looper 0.2.0")

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
            prompts = Path(td) / "prompts.md"

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(config.exists())
            self.assertTrue(prompts.exists())
            config_text = config.read_text(encoding="utf-8")
            self.assertIn("[looper]", config_text)
            self.assertIn("timeout_seconds = 7200", config_text)
            self.assertIn("max_transient_retries = 12", config_text)
            self.assertIn("retry_notify_after_seconds = 300", config_text)

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
            prompts = Path(td) / "prompts.md"
            config.write_text("custom config\n", encoding="utf-8")
            prompts.write_text("custom prompt\n", encoding="utf-8")
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
            self.assertEqual(prompts.read_text(encoding="utf-8"), "custom prompt\n")
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertIn(f"args=-d {tmpdir.resolve()}", lines)
            name_line = next(line for line in lines if line.startswith("CODEX_NAME="))
            args_line = next(line for line in lines if line.startswith("CODEX_ARGS="))
            self.assertRegex(name_line, r"^CODEX_NAME=Looper_[0-9a-f]{6}$")
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


if __name__ == "__main__":
    unittest.main()
