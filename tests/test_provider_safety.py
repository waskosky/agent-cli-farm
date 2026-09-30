import asyncio
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_looper import hybrid
from codex_looper.hybrid import CodexHybridController, TmuxCommandResult
from codex_looper.models import DEFAULT_STOP_PATTERNS, AgentConfig, LooperConfig, RunOptions
from codex_looper.process import run_command
from codex_looper.retry import classify_retry_kind, is_retryable_stop_reason, parse_output_line
from codex_looper.runner import run_loop

SAFETY_CODES = ("cyber_policy", "misalignment_policy_violation")
SESSION_ID = "54f5b65c-a31c-4aa1-b91b-896b35e2a759"


def task_complete(code: str) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": SESSION_ID,
            "last_agent_message": None,
            "error": {"codex_error_info": code, "message": "private-provider-detail"},
        },
    }


class ProviderSafetyTests(unittest.TestCase):
    def test_hybrid_classifies_terminal_error_arriving_after_first_snapshot(self) -> None:
        for code in SAFETY_CODES:
            for stale_completion in (False, True):
                with self.subTest(code=code, stale_completion=stale_completion):
                    with tempfile.TemporaryDirectory() as td:
                        root = Path(td)
                        path = root / f"rollout-{SESSION_ID}.jsonl"
                        path.write_text("", encoding="utf-8")
                        prefix = [
                            {
                                "type": "event_msg",
                                "payload": {"type": "task_started", "turn_id": SESSION_ID},
                            },
                            {
                                "type": "event_msg",
                                "payload": {"type": "user_message", "message": "synthetic"},
                            },
                            {
                                "type": "response_item",
                                "payload": {"type": "message", "role": "assistant"},
                            },
                        ]
                        if stale_completion:
                            prefix.append(
                                {
                                    "type": "event_msg",
                                    "payload": {
                                        "type": "task_complete",
                                        "turn_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                                    },
                                }
                            )

                        def command_runner(command, *, input_text=None, path=path, rows=prefix):
                            if command[1] == "capture-pane":
                                return TmuxCommandResult(0, "ready\n> ")
                            if command[1] == "paste-buffer":
                                path.write_text(
                                    "".join(json.dumps(row) + "\n" for row in rows),
                                    encoding="utf-8",
                                )
                            return TmuxCommandResult(0)

                        read_tail = hybrid.read_new_codex_session_events
                        appended = []

                        def append_after_snapshot(
                            path, *, offset=0, code=code, appended=appended, read_tail=read_tail
                        ):
                            snapshot = read_tail(path, offset=offset)
                            if snapshot.raw_text and not appended:
                                with path.open("a", encoding="utf-8") as handle:
                                    handle.write(json.dumps(task_complete(code)) + "\n")
                                appended.append(True)
                            return snapshot

                        controller = CodexHybridController(
                            command=["fake-codex"],
                            cwd=root,
                            env={},
                            pane_id="%8",
                            session_path=path,
                            command_runner=command_runner,
                            sleep_fn=lambda seconds: None,
                            require_tmux=False,
                        )
                        with patch.object(
                            hybrid, "read_new_codex_session_events", append_after_snapshot
                        ):
                            result = controller.run_turn(
                                prompt="synthetic",
                                timeout_seconds=2,
                                log_path=root / "turn.log",
                                completion_pattern=None,
                                stop_patterns=[],
                            )
                        self.assertEqual(result.stop_reason, f"safety_policy:{code}")
                        self.assertFalse(result.timed_out)

    def test_structured_safety_codes_are_terminal_with_stop_patterns_disabled(self) -> None:
        for code in SAFETY_CODES:
            for row in (
                {"type": "turn.failed", "error": {"code": code}},
                {"type": "error", "code": code},
                {"type": "error", "error": {"code": code}},
                {"type": "turn.failed", "error": {"codex_error_info": code}},
                {"type": "event_msg", "payload": {"type": "error", "codex_error_info": code}},
                task_complete(code),
            ):
                for stream in ("stdout", "stderr"):
                    with self.subTest(code=code, row=row, stream=stream):
                        parsed = parse_output_line(
                            line=json.dumps(row),
                            stream=stream,
                            agent_kind="codex",
                            patterns=[],
                            scan_stdout=False,
                        )
                        self.assertEqual(parsed.stop_reason, f"safety_policy:{code}")
                        self.assertIsNone(parsed.retry_kind)
                        self.assertIsNone(parsed.retry_after_seconds)
                        self.assertFalse(is_retryable_stop_reason(parsed.stop_reason))
                        self.assertIsNone(classify_retry_kind(parsed.stop_reason))

    def test_safety_code_wins_over_retry_and_completion_text(self) -> None:
        for code in SAFETY_CODES:
            with self.subTest(code=code):
                parsed = parse_output_line(
                    line=json.dumps(
                        {
                            "type": "turn.failed",
                            "error": {"code": code, "message": "overloaded 429 EXIT_SIGNAL: true"},
                            "retry_after_seconds": 300,
                        }
                    ),
                    stream="stdout",
                    agent_kind="codex",
                    patterns=[re.compile(pattern) for pattern in DEFAULT_STOP_PATTERNS],
                    scan_stdout=True,
                )
                self.assertEqual(parsed.stop_reason, f"safety_policy:{code}")
                self.assertIsNone(parsed.retry_kind)
                self.assertIsNone(parsed.retry_after_seconds)

    def test_quotes_tool_outputs_and_unrecognized_shapes_are_not_safety_errors(self) -> None:
        for code in SAFETY_CODES:
            quoted = json.dumps({"type": "turn.failed", "error": {"code": code}})
            rows = [
                f"Documentation mentions {code} and {quoted}",
                json.dumps(quoted),
                json.dumps({"type": "error", "message": quoted}),
                json.dumps({"type": "turn.failed", "error": {"message": code}}),
                json.dumps({"type": "turn.failed", "error": {"code": f"prefix_{code}"}}),
                json.dumps({"type": "error", "code": {"code": code}}),
                json.dumps({"type": "error", "code": [code]}),
                json.dumps({"type": "result", "subtype": "success", "error": {"code": code}}),
                json.dumps({"type": "item.completed", "item": {"error": {"code": code}}}),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": quoted,
                            "error": {"code": code},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": quoted,
                            "error": {"code": code},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": code,
                            "codex_error_info": code,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "error": {"message": quoted},
                        },
                    }
                ),
            ]
            for line in rows:
                for stream in ("stdout", "stderr"):
                    with self.subTest(line=line, stream=stream):
                        parsed = parse_output_line(
                            line=line,
                            stream=stream,
                            agent_kind="codex",
                            patterns=[re.compile(pattern) for pattern in DEFAULT_STOP_PATTERNS],
                            scan_stdout=True,
                        )
                        self.assertIsNone(parsed.stop_reason)

    def test_process_safety_error_overrides_earlier_retry_signal(self) -> None:
        for code in SAFETY_CODES:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                rows = [
                    {"type": "error", "message": "overloaded"},
                    {"type": "turn.failed", "error": {"code": code}},
                    {"type": "error", "message": "rate limit"},
                ]
                output = "".join(json.dumps(row) + "\n" for row in rows)
                result = asyncio.run(
                    run_command(
                        command=[sys.executable, "-c", f"print({output!r})"],
                        cwd=root,
                        env={},
                        timeout_seconds=10,
                        log_path=root / "process.log",
                        agent_kind="codex",
                        patterns=[re.compile(pattern) for pattern in DEFAULT_STOP_PATTERNS],
                        scan_stdout=False,
                        kill_on_stop_pattern=False,
                        stream_output=False,
                    )
                )
                self.assertEqual(result.stop_reason, f"safety_policy:{code}")
                self.assertIsNone(result.retry_kind)
                self.assertIsNone(result.retry_after_seconds)

    def test_process_terminates_on_safety_with_optional_pattern_killing_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # A prior ordinary stop must not disable the mandatory safety termination.
            rows = [
                {"type": "error", "message": "overloaded"},
                {"type": "error", "code": "cyber_policy"},
            ]
            output = "time.sleep(0.05); ".join(
                f"print({json.dumps(row)!r}, flush=True); " for row in rows
            )
            result = asyncio.run(
                run_command(
                    command=[
                        sys.executable,
                        "-c",
                        f"import time; {output}time.sleep(30)",
                    ],
                    cwd=root,
                    env={},
                    timeout_seconds=5,
                    log_path=root / "process.log",
                    agent_kind="codex",
                    patterns=[re.compile("overloaded")],
                    scan_stdout=False,
                    kill_on_stop_pattern=False,
                    stream_output=False,
                )
            )
            self.assertEqual(result.stop_reason, "safety_policy:cyber_policy")
            self.assertFalse(result.timed_out)

    def test_loop_dispatches_once_and_records_review_despite_continuation_options(self) -> None:
        for code in SAFETY_CODES:
            for returncode in (0, 1):
                with self.subTest(code=code, returncode=returncode):
                    with tempfile.TemporaryDirectory() as td:
                        root = Path(td)
                        script = root / "fake_provider.py"
                        script.write_text(
                            "import json, pathlib\n"
                            "with pathlib.Path('dispatches').open('a') as f: f.write('sent\\n')\n"
                            f"print({json.dumps(task_complete(code))!r})\n"
                            "print('EXIT_SIGNAL: true')\n"
                            f"raise SystemExit({returncode})\n",
                            encoding="utf-8",
                        )
                        prompt_file = root / "prompts.md"
                        prompt_file.write_text("private-prompt-one\n---\nprivate-prompt-two\n")
                        command = [sys.executable, str(script), "{prompt}"]
                        agent = AgentConfig(
                            name="codex",
                            kind="codex",
                            cwd=root,
                            first_command=command,
                            resume_command=command,
                        )
                        looper = LooperConfig(
                            mode="sequence",
                            mode_explicit=True,
                            prompt_file=prompt_file,
                            log_dir=root / "runs",
                            max_loops=2,
                            sleep_seconds=0,
                            max_transient_retries=0,
                            ignore_nonzero=True,
                            stop_patterns=[],
                            kill_on_stop_pattern=False,
                            fresh_session_per_loop=True,
                            completion_enabled=True,
                        )
                        options = RunOptions(agent_name="codex", config_path=root / "config.toml")
                        tmux_options = []
                        with contextlib.redirect_stdout(io.StringIO()):
                            exit_code = asyncio.run(
                                run_loop(
                                    agent=agent,
                                    looper=looper,
                                    options=options,
                                    start_tmux_log_pane_fn=lambda *args: False,
                                    set_tmux_window_option_fn=lambda *args, target=tmux_options: (
                                        target.append(args)
                                    ),
                                )
                            )
                        run_dir = next((root / "runs").iterdir())
                        state = json.loads((run_dir / "state.json").read_text())
                        events = [
                            json.loads(line)
                            for line in (run_dir / "events.jsonl").read_text().splitlines()
                        ]
                        self.assertEqual((root / "dispatches").read_text().splitlines(), ["sent"])
                        self.assertEqual(exit_code, 1)
                        self.assertEqual(state["status"], "needs_review")
                        self.assertEqual(state["stop_reason"], f"safety_policy:{code}")
                        self.assertEqual(state["safety_policy_code"], code)
                        self.assertIs(state["needs_review"], True)
                        self.assertIs(state["auto_retry_allowed"], False)
                        self.assertIsNone(state["retry_kind"])
                        self.assertIsNone(state["retry_after_seconds"])
                        self.assertEqual(state["current_loop"], 1)
                        self.assertEqual(state["current_prompt_index"], 1)
                        self.assertEqual(events[-1]["event"], "run_stopped")
                        self.assertNotIn("retry_wait", [event["event"] for event in events])
                        self.assertIn(("@codex_state", "ERR"), tmux_options)
                        for private_text in ("private-provider-detail", "private-prompt-one"):
                            self.assertNotIn(private_text, json.dumps(events))

    def test_hybrid_task_complete_error_precedes_ready_and_earlier_retry_event(self) -> None:
        for code in SAFETY_CODES:
            for prior_retry in (False, True):
                with self.subTest(code=code, prior_retry=prior_retry):
                    with tempfile.TemporaryDirectory() as td:
                        root = Path(td)
                        path = root / f"rollout-{SESSION_ID}.jsonl"
                        path.write_text("", encoding="utf-8")
                        sent_prompts = []

                        def command_runner(
                            command,
                            *,
                            input_text=None,
                            sent_prompts=sent_prompts,
                            prior_retry=prior_retry,
                            code=code,
                            path=path,
                        ):
                            if command[1] == "capture-pane":
                                return TmuxCommandResult(0, "EXIT_SIGNAL: true\n> ")
                            if command[1] == "load-buffer":
                                sent_prompts.append(input_text)
                            if command[1] == "paste-buffer":
                                rows = [
                                    {
                                        "type": "event_msg",
                                        "payload": {
                                            "type": "user_message",
                                            "message": "synthetic prompt",
                                        },
                                    }
                                ]
                                if prior_retry:
                                    rows.append({"type": "error", "message": "overloaded"})
                                rows.append(task_complete(code))
                                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                            return TmuxCommandResult(0)

                        controller = CodexHybridController(
                            command=["fake-codex"],
                            cwd=root,
                            env={},
                            pane_id="%8",
                            session_path=path,
                            command_runner=command_runner,
                            sleep_fn=lambda seconds: None,
                            require_tmux=False,
                        )
                        result = controller.run_turn(
                            prompt="synthetic prompt",
                            timeout_seconds=2,
                            log_path=root / "turn.log",
                            completion_pattern=re.compile("EXIT_SIGNAL: true"),
                            stop_patterns=[re.compile("overloaded")],
                        )
                        self.assertEqual(result.stop_reason, f"safety_policy:{code}")
                        self.assertFalse(result.timed_out)
                        self.assertIsNone(result.retry_kind)
                        self.assertIsNone(result.retry_after_seconds)
                        self.assertEqual(sent_prompts, ["synthetic prompt"])

    def test_hybrid_loop_safety_stop_dispatches_once_without_fresh_session_reset(self) -> None:
        for code in SAFETY_CODES:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                session_path = root / f"rollout-{SESSION_ID}.jsonl"
                session_path.write_text("", encoding="utf-8")
                prompt_file = root / "prompts.md"
                prompt_file.write_text("one\n---\ntwo\n", encoding="utf-8")
                sent_prompts = []
                resets = []

                def command_runner(
                    command,
                    *,
                    input_text=None,
                    sent_prompts=sent_prompts,
                    code=code,
                    session_path=session_path,
                ):
                    if command[1] == "capture-pane":
                        return TmuxCommandResult(0, "EXIT_SIGNAL: true\n> ")
                    if command[1] == "load-buffer":
                        sent_prompts.append(input_text)
                    if command[1] == "paste-buffer":
                        rows = [
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "user_message",
                                    "message": "synthetic prompt",
                                },
                            },
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [
                                        {"type": "output_text", "text": "partial response"}
                                    ],
                                },
                            },
                            task_complete(code),
                        ]
                        with session_path.open("a", encoding="utf-8") as handle:
                            handle.write("".join(json.dumps(row) + "\n" for row in rows))
                    return TmuxCommandResult(0)

                async def hybrid_turn(
                    *,
                    controller,
                    prompt,
                    session_path=session_path,
                    command_runner=command_runner,
                    **kwargs,
                ):
                    controller.pane_id = "%8"
                    controller.session_path = session_path
                    controller.command_runner = command_runner
                    controller.sleep_fn = lambda seconds: None
                    return controller.run_turn(
                        prompt=prompt,
                        timeout_seconds=2,
                        log_path=kwargs["log_path"],
                        completion_pattern=kwargs["completion_pattern"],
                        stop_patterns=kwargs["patterns"],
                    )

                async def fake_sleep(seconds):
                    pass

                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = asyncio.run(
                        run_loop(
                            agent=AgentConfig(
                                name="codex", kind="codex", interface="hybrid", cwd=root
                            ),
                            looper=LooperConfig(
                                mode="sequence",
                                mode_explicit=True,
                                prompt_file=prompt_file,
                                log_dir=root / "runs",
                                max_loops=2,
                                max_transient_retries=0,
                                ignore_nonzero=True,
                                fresh_session_per_loop=True,
                                completion_enabled=True,
                                stop_patterns=[],
                            ),
                            options=RunOptions(
                                agent_name="codex", config_path=root / "config.toml"
                            ),
                            run_codex_hybrid_turn_fn=hybrid_turn,
                            set_tmux_window_option_fn=lambda *args: None,
                            reset_hybrid_controller_fn=lambda controller, target=resets: (
                                target.append(controller)
                            ),
                            sleep_fn=fake_sleep,
                        )
                    )
                state = json.loads(next((root / "runs").glob("*/state.json")).read_text())
                self.assertEqual(sent_prompts, ["one"])
                self.assertEqual(exit_code, 1)
                self.assertEqual(resets, [])
                self.assertEqual(state["status"], "needs_review")
                self.assertEqual(state["stop_reason"], f"safety_policy:{code}")

    def test_hybrid_safety_event_split_across_rollout_writes_is_not_lost(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / f"rollout-{SESSION_ID}.jsonl"
            path.write_text("", encoding="utf-8")
            event = json.dumps(task_complete("cyber_policy")) + "\n"
            split = len(event) // 2
            prefix = "".join(
                json.dumps(row) + "\n"
                for row in [
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "synthetic prompt",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                ]
            )

            def command_runner(command, *, input_text=None):
                if command[1] == "capture-pane":
                    return TmuxCommandResult(0, "ready\n> ")
                if command[1] == "paste-buffer":
                    path.write_text(prefix + event[:split], encoding="utf-8")
                return TmuxCommandResult(0)

            def finish_event(seconds):
                if seconds == 1.0 and path.read_text() == prefix + event[:split]:
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(event[split:])

            controller = CodexHybridController(
                command=["fake-codex"],
                cwd=root,
                env={},
                pane_id="%8",
                session_path=path,
                command_runner=command_runner,
                sleep_fn=finish_event,
                require_tmux=False,
            )
            result = controller.run_turn(
                prompt="synthetic prompt",
                timeout_seconds=2,
                log_path=root / "turn.log",
                completion_pattern=None,
                stop_patterns=[],
            )
            self.assertEqual(result.stop_reason, "safety_policy:cyber_policy")
            self.assertFalse(result.timed_out)


if __name__ == "__main__":
    unittest.main()
