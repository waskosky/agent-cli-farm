import json
import tempfile
import unittest
from pathlib import Path

from codex_looper.hybrid import (
    assess_claude_hybrid_signals,
    extract_session_id_from_claude_session,
    extract_uuid_from_session_path,
    read_new_claude_session_events,
    tmux_prompt_paste_commands,
)


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


if __name__ == "__main__":
    unittest.main()
