import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_looper.hybrid import (
    ClaudeHybridController,
    CodexHybridController,
    TmuxCommandResult,
    assess_claude_hybrid_signals,
    assess_codex_hybrid_signals,
    build_claude_hybrid_command,
    build_codex_hybrid_command,
    extract_session_id_from_claude_session,
    extract_session_id_from_codex_session,
    extract_uuid_from_session_path,
    prompt_submit_delay_seconds,
    read_new_claude_session_events,
    read_new_codex_session_events,
    reset_hybrid_controller,
    tmux_prompt_paste_commands,
    tmux_split_window_command,
)
from codex_looper.models import AgentConfig, ConfigError

SESSION_ID = "54f5b65c-a31c-4aa1-b91b-896b35e2a759"


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


class ClaudeHybridTests(unittest.TestCase):
    def test_extracts_session_id_from_uuid_filename_or_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uuid_path = root / f"{SESSION_ID}.jsonl"
            uuid_path.write_text("", encoding="utf-8")
            metadata_path = root / "current.jsonl"
            write_jsonl(
                metadata_path,
                [
                    {"type": "summary", "customTitle": "redacted"},
                    {"type": "user", "sessionId": SESSION_ID, "message": {"role": "user"}},
                ],
            )

            self.assertEqual(extract_uuid_from_session_path(uuid_path), SESSION_ID)
            self.assertEqual(extract_session_id_from_claude_session(uuid_path), SESSION_ID)
            self.assertEqual(extract_session_id_from_claude_session(metadata_path), SESSION_ID)

    def test_reads_only_new_claude_session_events_from_offset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / f"{SESSION_ID}.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "sessionId": SESSION_ID,
                        "timestamp": "2026-06-27T00:00:00Z",
                        "message": {"role": "user", "content": "redacted prompt"},
                    }
                ],
            )

            first = read_new_claude_session_events(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "uuid": "assistant-1",
                            "parentUuid": "user-1",
                            "sessionId": SESSION_ID,
                            "timestamp": "2026-06-27T00:00:01Z",
                            "message": {
                                "role": "assistant",
                                "stop_reason": "end_turn",
                                "content": [{"type": "text", "text": "redacted answer"}],
                            },
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )

            second = read_new_claude_session_events(path, offset=first.offset)

        self.assertEqual(first.offset, len(first.raw_text.encode("utf-8")))
        self.assertEqual([event.event_type for event in first.events], ["user"])
        self.assertEqual([event.event_type for event in second.events], ["assistant"])
        self.assertEqual(second.events[0].role, "assistant")
        self.assertEqual(second.events[0].content_types, ("text",))
        self.assertEqual(second.events[0].stop_reason, "end_turn")

    def test_assessment_is_high_confidence_when_pane_ready_and_session_advanced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / f"{SESSION_ID}.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "sessionId": SESSION_ID,
                        "message": {"role": "user", "content": "redacted prompt"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "assistant-1",
                        "parentUuid": "user-1",
                        "sessionId": SESSION_ID,
                        "message": {
                            "role": "assistant",
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "redacted answer"}],
                        },
                    },
                    {"type": "last-prompt", "sessionId": SESSION_ID},
                ],
            )

            assessment = assess_claude_hybrid_signals("READY", session_path=path)

        self.assertTrue(assessment.ready_to_send_next)
        self.assertEqual(assessment.confidence, "high")
        self.assertEqual(assessment.session_id, SESSION_ID)
        self.assertEqual(assessment.last_event_type, "last-prompt")
        self.assertEqual(assessment.last_role, "assistant")
        self.assertIn("pane ready", assessment.reason)
        self.assertIn("assistant event", assessment.reason)

    def test_assessment_waits_for_terminal_assistant_event_after_tool_use(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / f"{SESSION_ID}.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "sessionId": SESSION_ID,
                        "message": {"role": "user", "content": "redacted prompt"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "assistant-tool",
                        "parentUuid": "user-1",
                        "sessionId": SESSION_ID,
                        "message": {
                            "role": "assistant",
                            "stop_reason": "tool_use",
                            "content": [{"type": "tool_use", "id": "tool-1", "name": "Bash"}],
                        },
                    },
                    {
                        "type": "user",
                        "uuid": "tool-result-1",
                        "parentUuid": "assistant-tool",
                        "sessionId": SESSION_ID,
                        "message": {"role": "user", "content": "tool result"},
                    },
                ],
            )

            assessment = assess_claude_hybrid_signals("READY", session_path=path)

        self.assertFalse(assessment.ready_to_send_next)
        self.assertEqual(assessment.confidence, "running")
        self.assertIn("tool use", assessment.reason)

    def test_assessment_stays_running_when_pane_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / f"{SESSION_ID}.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "uuid": "assistant-1",
                        "sessionId": SESSION_ID,
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "partial"}],
                        },
                    }
                ],
            )

            assessment = assess_claude_hybrid_signals("RUN", session_path=path)

        self.assertFalse(assessment.ready_to_send_next)
        self.assertEqual(assessment.confidence, "running")
        self.assertEqual(assessment.session_id, SESSION_ID)

    def test_terminal_assistant_event_overrides_stale_running_pane(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / f"{SESSION_ID}.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "sessionId": SESSION_ID,
                        "message": {"role": "user", "content": "redacted prompt"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "assistant-1",
                        "parentUuid": "user-1",
                        "sessionId": SESSION_ID,
                        "message": {
                            "role": "assistant",
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "finished"}],
                        },
                    },
                ],
            )

            assessment = assess_claude_hybrid_signals("RUN", session_path=path)

        self.assertTrue(assessment.ready_to_send_next)
        self.assertEqual(assessment.confidence, "high")
        self.assertIn("terminal assistant event", assessment.reason)

    def test_reset_hybrid_controller_kills_pane_and_clears_session_tracking(self) -> None:
        commands: list[list[str]] = []

        def command_runner(command: list[str], *, input_text: str | None = None):
            commands.append(command)
            return TmuxCommandResult(returncode=0)

        controller = ClaudeHybridController(
            command=["claude"],
            cwd=Path("/tmp"),
            env={},
            command_runner=command_runner,
            require_tmux=False,
            pane_id="%7",
            session_path=Path("/tmp/old-session.jsonl"),
        )
        prior_started_at = controller.started_at

        previous_pane_id = reset_hybrid_controller(controller)

        self.assertEqual(previous_pane_id, "%7")
        self.assertEqual(commands, [["tmux", "kill-pane", "-t", "%7"]])
        self.assertIsNone(controller.pane_id)
        self.assertIsNone(controller.session_path)
        self.assertGreaterEqual(controller.started_at, prior_started_at)

    def test_tmux_prompt_paste_commands_keep_prompt_out_of_shell_argv(self) -> None:
        prompt = "line 1\nline 2 with 'quotes' and $(rm -rf nope)"
        commands = tmux_prompt_paste_commands("%7", buffer_name="looper-buffer")

        self.assertEqual(
            commands,
            [
                ["tmux", "load-buffer", "-b", "looper-buffer", "-"],
                ["tmux", "paste-buffer", "-d", "-b", "looper-buffer", "-t", "%7"],
                ["tmux", "send-keys", "-t", "%7", "Enter"],
            ],
        )
        flattened_args = "\0".join(arg for command in commands for arg in command)
        self.assertNotIn(prompt, flattened_args)

    def test_prompt_submit_delay_scales_with_prompt_size(self) -> None:
        small = prompt_submit_delay_seconds("short prompt")
        large = prompt_submit_delay_seconds("x" * 60_000)

        self.assertGreaterEqual(small, 0.25)
        self.assertGreater(large, small)
        self.assertLessEqual(large, 2.0)

    def test_controller_waits_between_paste_and_enter(self) -> None:
        commands: list[list[str]] = []
        sleeps: list[float] = []

        def command_runner(command: list[str], *, input_text: str | None = None):
            commands.append(command)
            return TmuxCommandResult(returncode=0)

        controller = ClaudeHybridController(
            command=["claude"],
            cwd=Path("/tmp/project"),
            env={},
            command_runner=command_runner,
            sleep_fn=lambda seconds: sleeps.append(seconds),
            pane_id="%7",
        )

        controller.send_prompt("x" * 60_000)

        self.assertEqual(
            [command[1] for command in commands], ["load-buffer", "paste-buffer", "send-keys"]
        )
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 1.0)

    def test_tmux_split_window_command_passes_cwd_env_and_command(self) -> None:
        command = tmux_split_window_command(
            ["claude", "--model", "opus"],
            cwd=Path("/tmp/project"),
            env={"B_ENV": "two", "A_ENV": "one"},
            split_size="60%",
            inherited_env={},
        )

        self.assertEqual(
            command,
            [
                "tmux",
                "split-window",
                "-P",
                "-F",
                "#{pane_id}",
                "-d",
                "-v",
                "-l",
                "60%",
                "-e",
                "A_ENV=one",
                "-e",
                "B_ENV=two",
                "-c",
                "/tmp/project",
                "claude --model opus",
            ],
        )

    def test_tmux_split_window_command_inherits_non_secret_agent_home_env(self) -> None:
        command = tmux_split_window_command(
            ["codex", "--no-alt-screen"],
            cwd=Path("/tmp/project"),
            env={},
            inherited_env={
                "CODEX_HOME": "/tmp/codex-home",
                "OPENAI_API_KEY": "secret-token",
            },
        )

        self.assertIn("-e", command)
        self.assertIn("CODEX_HOME=/tmp/codex-home", command)
        self.assertNotIn("OPENAI_API_KEY=secret-token", command)
        self.assertEqual(command[-1], "codex --no-alt-screen")

    def test_build_claude_hybrid_command_allows_custom_interactive_command(self) -> None:
        default_agent = AgentConfig(
            name="claude",
            kind="claude",
            interface="hybrid",
            extra_args=["--dangerously-skip-permissions"],
            model="opus",
        )
        custom_agent = AgentConfig(
            name="claude",
            kind="claude",
            interface="hybrid",
            interactive_command=["claude-wrapper", "--profile", "loop"],
            extra_args=["--ignored"],
        )

        self.assertEqual(
            build_claude_hybrid_command(default_agent),
            ["claude", "--dangerously-skip-permissions", "--model", "opus"],
        )
        self.assertEqual(
            build_claude_hybrid_command(custom_agent),
            ["claude-wrapper", "--profile", "loop"],
        )

    def test_controller_drives_fake_tmux_until_claude_session_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_path = root / ".claude" / "projects" / "-tmp-project" / f"{SESSION_ID}.jsonl"
            session_path.parent.mkdir(parents=True)
            session_path.write_text("", encoding="utf-8")
            commands: list[tuple[list[str], str | None]] = []
            captures = ["Result\n> ", "✶ working … (esc to interrupt)", "Done\n> "]

            def command_runner(command: list[str], *, input_text: str | None = None):
                commands.append((command, input_text))
                if command[:3] == ["tmux", "split-window", "-P"]:
                    return TmuxCommandResult(returncode=0, stdout="%7\n")
                if command[:3] == ["tmux", "capture-pane", "-t"]:
                    if captures:
                        return TmuxCommandResult(returncode=0, stdout=captures.pop(0))
                    return TmuxCommandResult(returncode=0, stdout="Done\n> ")
                if command[:4] == ["tmux", "display-message", "-p", "-t"]:
                    return TmuxCommandResult(returncode=0, stdout="4242\n")
                if command[:2] == ["pgrep", "-P"]:
                    return TmuxCommandResult(returncode=1)
                if command[:2] == ["lsof", "-Fn"]:
                    return TmuxCommandResult(returncode=0, stdout=f"n{session_path}\n")
                if command[:3] == ["tmux", "load-buffer", "-b"]:
                    self.assertEqual(input_text, "complex prompt\nHYBRID_REQUEST")
                    return TmuxCommandResult(returncode=0)
                if command[:3] == ["tmux", "paste-buffer", "-d"]:
                    with session_path.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "type": "user",
                                    "uuid": "user-1",
                                    "sessionId": SESSION_ID,
                                    "message": {"role": "user", "content": "redacted"},
                                }
                            )
                            + "\n"
                        )
                        handle.write(
                            json.dumps(
                                {
                                    "type": "assistant",
                                    "uuid": "assistant-1",
                                    "parentUuid": "user-1",
                                    "sessionId": SESSION_ID,
                                    "message": {
                                        "role": "assistant",
                                        "stop_reason": "end_turn",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "answer HYBRID_DONE",
                                            }
                                        ],
                                    },
                                }
                            )
                            + "\n"
                        )
                    return TmuxCommandResult(returncode=0)
                if command[:3] == ["tmux", "send-keys", "-t"]:
                    return TmuxCommandResult(returncode=0)
                return TmuxCommandResult(returncode=0)

            controller = ClaudeHybridController(
                command=["claude", "--dangerously-skip-permissions"],
                cwd=root,
                env={},
                command_runner=command_runner,
                sleep_fn=lambda seconds: None,
                require_tmux=False,
            )
            log_path = root / "turn.log"

            result = controller.run_turn(
                prompt="complex prompt\nHYBRID_REQUEST",
                timeout_seconds=5,
                log_path=log_path,
                completion_pattern=__import__("re").compile("HYBRID_DONE"),
                stop_patterns=[],
            )
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.session_id, SESSION_ID)
        self.assertTrue(result.completion_detected)
        self.assertGreater(result.output_bytes, 0)
        self.assertIn("HYBRID_DONE", log_text)
        flattened_args = "\0".join(arg for command, _ in commands for arg in command)
        self.assertNotIn("complex prompt", flattened_args)
        self.assertTrue(
            any(command[:3] == ["tmux", "load-buffer", "-b"] for command, _ in commands)
        )

    def test_controller_switches_to_new_claude_session_file_after_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_dir = root / ".claude" / "projects" / "-tmp-project"
            session_dir.mkdir(parents=True)
            stale_session = session_dir / f"{SESSION_ID}.jsonl"
            fresh_session_id = "8a6853a0-81fc-401e-aac2-91e20d060220"
            fresh_session = session_dir / f"{fresh_session_id}.jsonl"
            stale_session.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-stale",
                        "sessionId": SESSION_ID,
                        "message": {"role": "assistant", "content": "old answer"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            fresh_session.write_text("", encoding="utf-8")
            prompt_was_pasted = False

            def command_runner(command: list[str], *, input_text: str | None = None):
                nonlocal prompt_was_pasted
                if command[:3] == ["tmux", "split-window", "-P"]:
                    return TmuxCommandResult(returncode=0, stdout="%7\n")
                if command[:3] == ["tmux", "capture-pane", "-t"]:
                    return TmuxCommandResult(returncode=0, stdout="Done\n> ")
                if command[:4] == ["tmux", "display-message", "-p", "-t"]:
                    return TmuxCommandResult(returncode=0, stdout="4242\n")
                if command[:2] == ["pgrep", "-P"]:
                    return TmuxCommandResult(returncode=1)
                if command[:2] == ["lsof", "-Fn"]:
                    path = fresh_session if prompt_was_pasted else stale_session
                    return TmuxCommandResult(returncode=0, stdout=f"n{path}\n")
                if command[:3] == ["tmux", "paste-buffer", "-d"]:
                    prompt_was_pasted = True
                    with fresh_session.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "type": "user",
                                    "uuid": "user-fresh",
                                    "sessionId": fresh_session_id,
                                    "message": {"role": "user", "content": "redacted"},
                                }
                            )
                            + "\n"
                        )
                        handle.write(
                            json.dumps(
                                {
                                    "type": "assistant",
                                    "uuid": "assistant-fresh",
                                    "parentUuid": "user-fresh",
                                    "sessionId": fresh_session_id,
                                    "message": {
                                        "role": "assistant",
                                        "stop_reason": "end_turn",
                                        "content": [{"type": "text", "text": "fresh answer"}],
                                    },
                                }
                            )
                            + "\n"
                        )
                    return TmuxCommandResult(returncode=0)
                return TmuxCommandResult(returncode=0)

            controller = ClaudeHybridController(
                command=["claude"],
                cwd=root,
                env={},
                command_runner=command_runner,
                sleep_fn=lambda seconds: None,
                require_tmux=False,
            )

            result = controller.run_turn(
                prompt="prompt that opens a fresh Claude transcript",
                timeout_seconds=5,
                log_path=root / "turn.log",
                completion_pattern=None,
                stop_patterns=[],
            )
            log_text = (root / "turn.log").read_text(encoding="utf-8")

        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.session_id, fresh_session_id)
        self.assertIn("fresh answer", log_text)

    def test_controller_waits_for_final_answer_after_tool_use(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_path = root / ".claude" / "projects" / "-tmp-project" / f"{SESSION_ID}.jsonl"
            session_path.parent.mkdir(parents=True)
            session_path.write_text("", encoding="utf-8")
            capture_count = 0

            def append_row(row: dict[str, object]) -> None:
                with session_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row) + "\n")

            def command_runner(command: list[str], *, input_text: str | None = None):
                nonlocal capture_count
                if command[:3] == ["tmux", "split-window", "-P"]:
                    return TmuxCommandResult(returncode=0, stdout="%7\n")
                if command[:3] == ["tmux", "capture-pane", "-t"]:
                    capture_count += 1
                    if capture_count == 2:
                        append_row(
                            {
                                "type": "user",
                                "uuid": "tool-result-1",
                                "sessionId": SESSION_ID,
                                "message": {"role": "user", "content": "tool result"},
                            }
                        )
                        append_row(
                            {
                                "type": "assistant",
                                "uuid": "assistant-final",
                                "parentUuid": "tool-result-1",
                                "sessionId": SESSION_ID,
                                "message": {
                                    "role": "assistant",
                                    "stop_reason": "end_turn",
                                    "content": [{"type": "text", "text": "final answer"}],
                                },
                            }
                        )
                    return TmuxCommandResult(returncode=0, stdout="Done\n> ")
                if command[:4] == ["tmux", "display-message", "-p", "-t"]:
                    return TmuxCommandResult(returncode=0, stdout="4242\n")
                if command[:2] == ["pgrep", "-P"]:
                    return TmuxCommandResult(returncode=1)
                if command[:2] == ["lsof", "-Fn"]:
                    return TmuxCommandResult(returncode=0, stdout=f"n{session_path}\n")
                if command[:3] == ["tmux", "paste-buffer", "-d"]:
                    append_row(
                        {
                            "type": "user",
                            "uuid": "user-1",
                            "sessionId": SESSION_ID,
                            "message": {"role": "user", "content": "redacted"},
                        }
                    )
                    append_row(
                        {
                            "type": "assistant",
                            "uuid": "assistant-tool",
                            "parentUuid": "user-1",
                            "sessionId": SESSION_ID,
                            "message": {
                                "role": "assistant",
                                "stop_reason": "tool_use",
                                "content": [{"type": "tool_use", "id": "tool-1", "name": "Bash"}],
                            },
                        }
                    )
                    return TmuxCommandResult(returncode=0)
                return TmuxCommandResult(returncode=0)

            controller = ClaudeHybridController(
                command=["claude"],
                cwd=root,
                env={},
                command_runner=command_runner,
                sleep_fn=lambda seconds: None,
                require_tmux=False,
            )

            result = controller.run_turn(
                prompt="prompt with tool",
                timeout_seconds=5,
                log_path=root / "turn.log",
                completion_pattern=None,
                stop_patterns=[],
            )

        self.assertEqual(result.returncode, 0)
        self.assertGreaterEqual(capture_count, 2)

    def test_controller_does_not_finish_turn_without_new_user_event(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_path = root / ".claude" / "projects" / "-tmp-project" / f"{SESSION_ID}.jsonl"
            session_path.parent.mkdir(parents=True)
            session_path.write_text("", encoding="utf-8")

            def command_runner(command: list[str], *, input_text: str | None = None):
                if command[:3] == ["tmux", "split-window", "-P"]:
                    return TmuxCommandResult(returncode=0, stdout="%7\n")
                if command[:3] == ["tmux", "capture-pane", "-t"]:
                    return TmuxCommandResult(returncode=0, stdout="Done\n> ")
                if command[:4] == ["tmux", "display-message", "-p", "-t"]:
                    return TmuxCommandResult(returncode=0, stdout="4242\n")
                if command[:2] == ["pgrep", "-P"]:
                    return TmuxCommandResult(returncode=1)
                if command[:2] == ["lsof", "-Fn"]:
                    return TmuxCommandResult(returncode=0, stdout=f"n{session_path}\n")
                if command[:3] == ["tmux", "paste-buffer", "-d"]:
                    with session_path.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "type": "assistant",
                                    "uuid": "assistant-stale",
                                    "sessionId": SESSION_ID,
                                    "message": {
                                        "role": "assistant",
                                        "content": [{"type": "text", "text": "stale answer"}],
                                    },
                                }
                            )
                            + "\n"
                        )
                    return TmuxCommandResult(returncode=0)
                return TmuxCommandResult(returncode=0)

            controller = ClaudeHybridController(
                command=["claude"],
                cwd=root,
                env={},
                command_runner=command_runner,
                sleep_fn=lambda seconds: None,
                require_tmux=False,
            )

            result = controller.run_turn(
                prompt="prompt that was not accepted",
                timeout_seconds=0.01,
                log_path=root / "turn.log",
                completion_pattern=None,
                stop_patterns=[],
            )

        self.assertTrue(result.timed_out)
        self.assertEqual(result.stop_reason, "Claude hybrid timeout after 0.01 seconds")

    def test_reads_codex_session_events_and_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / f"rollout-2026-07-07T00-00-00-{SESSION_ID}.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": SESSION_ID, "cwd": str(root)},
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "prompt"},
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "answer"}],
                        },
                    },
                ],
            )

            tail = read_new_codex_session_events(path)

        self.assertEqual(extract_session_id_from_codex_session(path), SESSION_ID)
        self.assertEqual(tail.events[0].session_id, SESSION_ID)
        self.assertTrue(tail.events[1].is_user_event)
        self.assertTrue(tail.events[2].is_assistant_event)

    def test_codex_assessment_requires_ready_pane_and_session_advance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / f"rollout-2026-07-07T00-00-00-{SESSION_ID}.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "prompt"},
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "answer"}],
                        },
                    },
                ],
            )

            ready = assess_codex_hybrid_signals("READY", session_path=path)
            running = assess_codex_hybrid_signals("RUN", session_path=path)

        self.assertTrue(ready.ready_to_send_next)
        self.assertTrue(ready.user_event_seen)
        self.assertTrue(ready.assistant_event_seen)
        self.assertEqual(ready.confidence, "high")
        self.assertFalse(running.ready_to_send_next)
        self.assertEqual(running.confidence, "running")

    def test_build_codex_hybrid_command_uses_visible_inline_tui(self) -> None:
        default_agent = AgentConfig(
            name="codex",
            kind="codex",
            interface="hybrid",
            extra_args=["--ask-for-approval", "never"],
            model="gpt-5.5",
        )
        custom_agent = AgentConfig(
            name="codex",
            kind="codex",
            interface="hybrid",
            interactive_command=["codex-wrapper", "--profile", "loop"],
            extra_args=["--ignored"],
        )

        self.assertEqual(
            build_codex_hybrid_command(default_agent),
            ["codex", "--no-alt-screen", "--ask-for-approval", "never", "--model", "gpt-5.5"],
        )
        self.assertEqual(
            build_codex_hybrid_command(custom_agent),
            ["codex-wrapper", "--profile", "loop"],
        )

    def test_codex_controller_drives_fake_tmux_until_session_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_root = root / ".codex" / "sessions" / "2026" / "07" / "07"
            session_root.mkdir(parents=True)
            session_path = session_root / f"rollout-2026-07-07T00-00-00-{SESSION_ID}.jsonl"
            write_jsonl(
                session_path,
                [{"type": "session_meta", "payload": {"session_id": SESSION_ID, "cwd": str(root)}}],
            )
            commands: list[tuple[list[str], str | None]] = []
            captures = ["codex ready\n> ", "working", "CODEX_DONE\n> "]

            def command_runner(command: list[str], *, input_text: str | None = None):
                commands.append((command, input_text))
                if command[:3] == ["tmux", "split-window", "-P"]:
                    return TmuxCommandResult(returncode=0, stdout="%8\n")
                if command[:3] == ["tmux", "capture-pane", "-t"]:
                    if captures:
                        return TmuxCommandResult(returncode=0, stdout=captures.pop(0))
                    return TmuxCommandResult(returncode=0, stdout="CODEX_DONE\n> ")
                if command[:4] == ["tmux", "display-message", "-p", "-t"]:
                    return TmuxCommandResult(returncode=0, stdout="5252\n")
                if command[:2] == ["pgrep", "-P"]:
                    return TmuxCommandResult(returncode=1)
                if command[:2] == ["lsof", "-Fn"]:
                    return TmuxCommandResult(returncode=0, stdout=f"n{session_path}\n")
                if command[:3] == ["tmux", "load-buffer", "-b"]:
                    self.assertEqual(input_text, "complex prompt\nCODEX_REQUEST")
                    return TmuxCommandResult(returncode=0)
                if command[:3] == ["tmux", "paste-buffer", "-d"]:
                    with session_path.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "type": "event_msg",
                                    "payload": {
                                        "type": "user_message",
                                        "message": "redacted prompt",
                                    },
                                }
                            )
                            + "\n"
                        )
                        handle.write(
                            json.dumps(
                                {
                                    "type": "response_item",
                                    "payload": {
                                        "type": "message",
                                        "role": "assistant",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "answer CODEX_DONE",
                                            }
                                        ],
                                    },
                                }
                            )
                            + "\n"
                        )
                    return TmuxCommandResult(returncode=0)
                if command[:3] == ["tmux", "send-keys", "-t"]:
                    return TmuxCommandResult(returncode=0)
                return TmuxCommandResult(returncode=0)

            controller = CodexHybridController(
                command=["codex", "--no-alt-screen"],
                cwd=root,
                env={"CODEX_HOME": str(root / ".codex")},
                command_runner=command_runner,
                sleep_fn=lambda seconds: None,
                require_tmux=False,
            )
            result = controller.run_turn(
                prompt="complex prompt\nCODEX_REQUEST",
                timeout_seconds=5,
                log_path=root / "turn.log",
                completion_pattern=__import__("re").compile("CODEX_DONE"),
                stop_patterns=[],
            )
            log_text = (root / "turn.log").read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.session_id, SESSION_ID)
        self.assertTrue(result.completion_detected)
        self.assertGreater(result.output_bytes, 0)
        self.assertIn("CODEX_DONE", log_text)
        flattened_args = "\0".join(arg for command, _ in commands for arg in command)
        self.assertNotIn("complex prompt", flattened_args)
        self.assertTrue(
            any(command[:3] == ["tmux", "load-buffer", "-b"] for command, _ in commands)
        )

    def test_codex_session_discovery_ignores_recently_modified_older_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_root = root / ".codex" / "sessions" / "2026" / "07" / "16"
            session_root.mkdir(parents=True)
            started_at = time.time()
            current_path = session_root / f"rollout-current-{SESSION_ID}.jsonl"
            old_path = session_root / "rollout-old-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
            current_timestamp = datetime.fromtimestamp(started_at + 1, tz=timezone.utc).isoformat()
            old_timestamp = datetime.fromtimestamp(started_at - 3600, tz=timezone.utc).isoformat()
            write_jsonl(
                current_path,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "session_id": SESSION_ID,
                            "timestamp": current_timestamp,
                            "cwd": str(root),
                        },
                    }
                ],
            )
            write_jsonl(
                old_path,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                            "timestamp": old_timestamp,
                            "cwd": str(root),
                        },
                    }
                ],
            )

            controller = CodexHybridController(
                command=["codex", "--no-alt-screen"],
                cwd=root,
                env={"CODEX_HOME": str(root / ".codex")},
                command_runner=lambda command, input_text=None: TmuxCommandResult(returncode=1),
                sleep_fn=lambda seconds: None,
                require_tmux=False,
                started_at=started_at,
            )

            discovered = controller.discover_session_path()

        self.assertEqual(discovered, current_path.resolve())

    def test_codex_controller_blocks_on_workspace_trust_prompt(self) -> None:
        def command_runner(command: list[str], *, input_text: str | None = None):
            if command[:3] == ["tmux", "split-window", "-P"]:
                return TmuxCommandResult(returncode=0, stdout="%8\n")
            if command[:3] == ["tmux", "capture-pane", "-t"]:
                return TmuxCommandResult(
                    returncode=0,
                    stdout=(
                        "Do you trust the contents of this directory?\n"
                        "1. Yes, continue\n"
                        "2. No, quit\n"
                        "Press enter to continue\n"
                    ),
                )
            return TmuxCommandResult(returncode=0)

        controller = CodexHybridController(
            command=["codex", "--no-alt-screen"],
            cwd=Path("/tmp/project"),
            env={},
            command_runner=command_runner,
            sleep_fn=lambda seconds: None,
            require_tmux=False,
        )

        with self.assertRaisesRegex(ConfigError, "workspace trust"):
            controller.ensure_started(timeout_seconds=5)

    def test_codex_controller_skips_update_prompt_before_becoming_ready(self) -> None:
        update_prompt = (
            "Update available! 0.144.4 -> 0.144.5\n"
            "1. Update now\n"
            "2. Skip\n"
            "Press enter to continue\n"
        )
        captures = [update_prompt, update_prompt, "\u203a \n"]
        commands: list[list[str]] = []

        def command_runner(command: list[str], *, input_text: str | None = None):
            commands.append(command)
            if command[:3] == ["tmux", "split-window", "-P"]:
                return TmuxCommandResult(returncode=0, stdout="%9\n")
            if command[:3] == ["tmux", "capture-pane", "-t"]:
                return TmuxCommandResult(returncode=0, stdout=captures.pop(0))
            return TmuxCommandResult(returncode=0)

        controller = CodexHybridController(
            command=["codex", "--no-alt-screen"],
            cwd=Path("/tmp/project"),
            env={},
            command_runner=command_runner,
            sleep_fn=lambda seconds: None,
            require_tmux=False,
        )

        controller.ensure_started(timeout_seconds=5)

        skip_commands = [
            command for command in commands if command[:3] == ["tmux", "send-keys", "-t"]
        ]
        self.assertEqual(skip_commands, [["tmux", "send-keys", "-t", "%9", "2", "Enter"]])


if __name__ == "__main__":
    unittest.main()
