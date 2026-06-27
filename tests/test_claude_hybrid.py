import json
import tempfile
import unittest
from pathlib import Path

from codex_looper.hybrid import (
    ClaudeHybridController,
    TmuxCommandResult,
    assess_claude_hybrid_signals,
    build_claude_hybrid_command,
    extract_session_id_from_claude_session,
    extract_uuid_from_session_path,
    read_new_claude_session_events,
    tmux_prompt_paste_commands,
    tmux_split_window_command,
)
from codex_looper.models import AgentConfig

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

    def test_tmux_split_window_command_passes_cwd_env_and_command(self) -> None:
        command = tmux_split_window_command(
            ["claude", "--model", "opus"],
            cwd=Path("/tmp/project"),
            env={"B_ENV": "two", "A_ENV": "one"},
            split_size="60%",
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
                "-c",
                "/tmp/project",
                "env A_ENV=one B_ENV=two claude --model opus",
            ],
        )

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
                if command[:3] == ["tmux", "list-panes", "-t"]:
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
        self.assertTrue(any(command[:3] == ["tmux", "load-buffer", "-b"] for command, _ in commands))


if __name__ == "__main__":
    unittest.main()
