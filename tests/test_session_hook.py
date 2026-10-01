from __future__ import annotations

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
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "bin" / "codex-session-hook.py"
INSTALLER = REPO_ROOT / "bin" / "codex-session-hook-install.py"
SESSION_ID = "019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4"
HOOK_SPEC = importlib.util.spec_from_file_location("codex_session_hook", HOOK)
assert HOOK_SPEC is not None and HOOK_SPEC.loader is not None
hook = importlib.util.module_from_spec(HOOK_SPEC)
HOOK_SPEC.loader.exec_module(hook)


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class SessionHookAncestorTests(unittest.TestCase):
    def record_with_processes(self, processes: dict[int, tuple[int, list[str]]]) -> list[list[str]]:
        payload = json.dumps({"hook_event_name": "SessionStart", "session_id": SESSION_ID})
        stdin = io.TextIOWrapper(io.BytesIO(payload.encode()))
        env = {"CODEXFARM_MANAGED": "1", "CODEXFARM_PROVIDER": "codex", "TMUX_PANE": "%17"}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(hook.os, "getppid", return_value=100),
            mock.patch.object(hook, "process_info", side_effect=processes.get),
            mock.patch.object(hook.sys, "stdin", stdin),
            mock.patch.object(hook.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            hook.record_session()
        stdin.close()
        return [call.args[0] for call in run.call_args_list]

    def test_records_direct_tui_with_app_server_option_values_or_prompt(self) -> None:
        argument_cases = [
            ["--no-daemon", "--profile", "app-server"],
            ["--profile=app-server"],
            ["-papp-server"],
            ["resume", SESSION_ID, "app-server"],
            ["--", "app-server"],
        ]
        for option in (
            "-c",
            "--config",
            "--enable",
            "--disable",
            "-i",
            "--image",
            "-m",
            "--model",
            "--local-provider",
            "--remote",
            "-p",
            "--profile",
            "-s",
            "--sandbox",
            "-a",
            "--ask-for-approval",
            "-C",
            "--cd",
            "--add-dir",
        ):
            argument_cases.append([option, "app-server"])
        for arguments in argument_cases:
            with self.subTest(arguments=arguments):
                commands = self.record_with_processes(
                    {100: (1, ["codex", "/opt/codex", *arguments])}
                )
                self.assertEqual(len(commands), 6)
                self.assertEqual(commands[-2][-1], "100")
                self.assertEqual(commands[-1][-1], SESSION_ID)

    def test_app_server_command_stops_before_tmux_mutation_or_parent_tui(self) -> None:
        for arguments in (
            ["app-server"],
            ["app-server", "--managed-daemon"],
            ["--profile", "app-server", "app-server", "--managed-daemon"],
            ["-c", "name=app-server", "app-server"],
            ["--remote", "unix:///tmp/server", "app-server"],
        ):
            for prefix in (
                ["codex", "/opt/codex"],
                ["bash", "/bin/bash", "/opt/codex"],
                ["node", "/usr/bin/node", "/opt/codex.js"],
            ):
                with self.subTest(prefix=prefix, arguments=arguments):
                    commands = self.record_with_processes(
                        {
                            100: (200, [*prefix, *arguments]),
                            200: (1, ["codex", "/opt/codex", "--no-daemon"]),
                        }
                    )
                    self.assertEqual(commands, [])

    def test_unrelated_process_arguments_do_not_block_parent_tui(self) -> None:
        for tokens in (
            ["python3", "/usr/bin/python3", "/opt/hook.py", "app-server"],
            ["helper", "/opt/helper", "codex", "app-server"],
            ["bash", "/bin/bash", "-c", "codex", "app-server"],
        ):
            with self.subTest(tokens=tokens):
                commands = self.record_with_processes(
                    {
                        100: (200, tokens),
                        200: (1, ["codex", "/opt/codex", "--no-daemon"]),
                    }
                )
                self.assertEqual(len(commands), 6)
                self.assertEqual(commands[-2][-1], "200")

    def test_provider_name_in_unrelated_argv_is_not_a_provider(self) -> None:
        commands = self.record_with_processes(
            {100: (1, ["helper", "/opt/helper", "codex", "resume", SESSION_ID])}
        )
        self.assertEqual(commands, [])

    def test_ps_fallback_keeps_comm_separate_from_codex_argv(self) -> None:
        for arguments, expected_pid in (
            ("--no-daemon --profile app-server", 100),
            ("resume " + SESSION_ID + " app-server", 100),
            ("--profile app-server app-server --managed-daemon", None),
        ):
            with (
                self.subTest(arguments=arguments),
                mock.patch.object(hook.os, "getppid", return_value=100),
                mock.patch.object(Path, "read_text", side_effect=OSError),
                mock.patch.object(hook.subprocess, "run") as run,
            ):
                run.return_value.returncode = 0
                run.return_value.stdout = "1 codex /opt/codex " + arguments
                self.assertEqual(hook.provider_ancestor_pid("codex"), expected_pid)


class SessionHookRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="session-hook-test-")
        self.root = Path(self.temp_dir.name)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.tmux_log = self.root / "tmux.log"

        make_executable(
            self.fake_bin / "tmux",
            """#!/usr/bin/env bash
{
  printf '%q ' "$@"
  printf '\\n'
} >> "$TMUX_LOG"
""",
        )
        for provider in ("codex", "claude", "gemini"):
            make_executable(
                self.fake_bin / provider,
                """#!/usr/bin/env bash
if [[ "${1:-}" == "--with-server" ]]; then
  shift
  "$0" app-server "$@"
  exit "$?"
fi
if [[ "${1:-}" == "app-server" ]]; then
  shift
  if [[ "${1:-}" == "--managed-daemon" ]]; then
    shift
  fi
fi
"$@"
exit "$?"
""",
            )

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.fake_bin}:{self.env.get('PATH', '')}"
        self.env["TMUX_LOG"] = str(self.tmux_log)
        self.env["TMUX_PANE"] = "%17"
        self.env["CODEXFARM_MANAGED"] = "1"
        self.env["CODEXFARM_PROVIDER"] = "codex"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_hook(
        self,
        payload: str,
        *,
        env: dict[str, str] | None = None,
        through_provider: bool = True,
        provider_arguments: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        hook_env = env or self.env
        command = [HOOK]
        if through_provider:
            command = [
                self.fake_bin / hook_env["CODEXFARM_PROVIDER"],
                *provider_arguments,
                HOOK,
            ]
        return subprocess.run(
            command,
            input=payload,
            env=hook_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def tmux_commands(self) -> list[list[str]]:
        if not self.tmux_log.exists():
            return []
        return [
            shlex.split(line) for line in self.tmux_log.read_text(encoding="utf-8").splitlines()
        ]

    def test_records_valid_session_metadata_and_writes_session_id_last(self) -> None:
        result = self.run_hook(
            f'{{"hook_event_name":"SessionStart","session_id":"{SESSION_ID}","cwd":"/tmp/project"}}'
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        commands = self.tmux_commands()
        self.assertEqual(
            [commands[0][5], *[command[4] for command in commands[1:]]],
            [
                "@codexfarm_session_id",
                "@codexfarm_provider",
                "@codexfarm_session_source",
                "@codexfarm_session_seen_at",
                "@codexfarm_session_pid",
                "@codexfarm_session_id",
            ],
        )
        self.assertEqual(
            commands[0],
            ["set-option", "-p", "-u", "-t", "%17", "@codexfarm_session_id"],
        )
        self.assertTrue(
            all(command[:4] == ["set-option", "-p", "-t", "%17"] for command in commands[1:])
        )
        self.assertEqual(commands[1][5], "codex")
        self.assertEqual(commands[2][5], "hook:SessionStart")
        self.assertGreater(int(commands[3][5]), 0)
        self.assertGreater(int(commands[4][5]), 0)
        self.assertEqual(commands[5][5], SESSION_ID)

    def test_ignores_codex_app_server_hooks(self) -> None:
        for arguments in (("app-server",), ("app-server", "--managed-daemon")):
            for event in ("SessionStart", "UserPromptSubmit"):
                with self.subTest(arguments=arguments, event=event):
                    result = self.run_hook(
                        json.dumps({"hook_event_name": event, "session_id": SESSION_ID}),
                        provider_arguments=arguments,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(self.tmux_commands(), [])

    def test_app_server_hook_does_not_fall_back_to_parent_codex_tui(self) -> None:
        result = self.run_hook(
            json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": SESSION_ID}),
            provider_arguments=("--with-server",),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.tmux_commands(), [])

    def test_records_claude_and_gemini_sessions(self) -> None:
        for provider in ("claude", "gemini"):
            with self.subTest(provider=provider):
                result = self.run_hook(
                    json.dumps({"hook_event_name": "SessionStart", "session_id": SESSION_ID}),
                    env={**self.env, "CODEXFARM_PROVIDER": provider},
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "{}\n" if provider == "gemini" else "")
                self.assertEqual(result.stderr, "")
                commands = self.tmux_commands()
                self.assertEqual(commands[-5][5], provider)
                self.assertGreater(int(commands[-2][5]), 0)
                self.assertEqual(commands[-1][5], SESSION_ID)

    def test_ignores_unmanaged_panes(self) -> None:
        env = self.env.copy()
        env.pop("CODEXFARM_MANAGED")

        result = self.run_hook(
            f'{{"hook_event_name":"SessionStart","session_id":"{SESSION_ID}"}}',
            env=env,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.tmux_commands(), [])

    def test_gemini_hook_returns_valid_json_even_when_unmanaged(self) -> None:
        result = self.run_hook(
            f'{{"hook_event_name":"SessionStart","session_id":"{SESSION_ID}"}}',
            env={
                **self.env,
                "CODEXFARM_PROVIDER": "gemini",
                "CODEXFARM_MANAGED": "0",
                "CODEXFARM_HOOK_FORMAT": "json",
            },
            through_provider=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})

    def test_gemini_global_hook_returns_json_outside_a_farm(self) -> None:
        env = {**self.env, "CODEXFARM_HOOK_FORMAT": "json"}
        env.pop("CODEXFARM_PROVIDER")
        env.pop("CODEXFARM_MANAGED")
        result = self.run_hook("{}", env=env, through_provider=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})

    def test_ignores_malformed_input_invalid_pane_and_invalid_uuid(self) -> None:
        cases = [
            ("not-json", self.env),
            (
                f'{{"hook_event_name":"SessionStart","session_id":"{SESSION_ID}"}}',
                {**self.env, "TMUX_PANE": "codexfarm:1"},
            ),
            (
                '{"hook_event_name":"SessionStart","session_id":"latest"}',
                self.env,
            ),
        ]

        for payload, env in cases:
            with self.subTest(payload=payload, pane=env["TMUX_PANE"]):
                result = self.run_hook(payload, env=env)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")

        self.assertEqual(self.tmux_commands(), [])

    def test_does_not_record_metadata_without_a_provider_ancestor(self) -> None:
        env = {**self.env, "CODEXFARM_PROVIDER": "claude"}
        result = self.run_hook(
            f'{{"hook_event_name":"UserPromptSubmit","session_id":"{SESSION_ID}"}}',
            env=env,
            through_provider=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.tmux_commands(), [])

    def test_ignores_codex_subagent_prompt_events(self) -> None:
        for marker in (
            {"agent_id": "123e4567-e89b-42d3-a456-426614174099"},
            {"agent_type": "worker"},
        ):
            with self.subTest(marker=marker):
                payload = {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "123e4567-e89b-42d3-a456-426614174099",
                    **marker,
                }
                result = self.run_hook(json.dumps(payload))

                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")

        self.assertEqual(self.tmux_commands(), [])


class SessionHookInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="session-hook-install-test-")
        self.root = Path(self.temp_dir.name)
        self.hooks_file = self.root / ".codex" / "hooks.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_installer(
        self,
        *arguments: str,
        hooks_file: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                INSTALLER,
                "--hooks-file",
                hooks_file or self.hooks_file,
                "--hook-command",
                HOOK,
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def read_config(self) -> dict[str, object]:
        return json.loads(self.hooks_file.read_text(encoding="utf-8"))

    @staticmethod
    def farm_handlers(config: dict[str, object], event: str) -> list[dict[str, object]]:
        hooks = config["hooks"]
        assert isinstance(hooks, dict)
        groups = hooks[event]
        assert isinstance(groups, list)
        return [
            handler
            for group in groups
            for handler in group["hooks"]
            if Path(shlex.split(handler["command"])[-1]).name == "codex-session-hook.py"
        ]

    def test_installs_both_events_owner_only_and_is_idempotent(self) -> None:
        first = self.run_installer()

        self.assertEqual(first.returncode, 0, first.stderr)
        config = self.read_config()
        self.assertEqual(stat.S_IMODE(self.hooks_file.stat().st_mode), 0o600)
        self.assertEqual(len(self.farm_handlers(config, "SessionStart")), 1)
        self.assertEqual(len(self.farm_handlers(config, "UserPromptSubmit")), 1)
        session_groups = config["hooks"]["SessionStart"]
        self.assertEqual(session_groups[0]["matcher"], "startup|resume|clear|compact")
        first_bytes = self.hooks_file.read_bytes()

        second = self.run_installer()

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.hooks_file.read_bytes(), first_bytes)
        config = self.read_config()
        self.assertEqual(len(self.farm_handlers(config, "SessionStart")), 1)
        self.assertEqual(len(self.farm_handlers(config, "UserPromptSubmit")), 1)

    def test_installs_claude_and_gemini_in_provider_settings(self) -> None:
        for provider in ("claude", "gemini"):
            with self.subTest(provider=provider):
                settings = self.root / provider / "settings.json"
                settings.parent.mkdir()
                settings.write_text(json.dumps({"model": "test", "hooks": {"Stop": []}}))
                first = self.run_installer("--provider", provider, hooks_file=settings)
                self.assertEqual(first.returncode, 0, first.stderr)
                data = json.loads(settings.read_text())
                self.assertEqual(data["model"], "test")
                self.assertEqual(data["hooks"]["Stop"], [])
                self.assertEqual(len(self.farm_handlers(data, "SessionStart")), 1)
                self.assertEqual(len(self.farm_handlers(data, "UserPromptSubmit")), 1)
                if provider == "gemini":
                    self.assertEqual(self.farm_handlers(data, "SessionStart")[0]["timeout"], 3000)
                    self.assertNotIn("matcher", data["hooks"]["SessionStart"][0])
                    self.assertIn(
                        "CODEXFARM_HOOK_FORMAT=json",
                        self.farm_handlers(data, "SessionStart")[0]["command"],
                    )
                before = settings.read_bytes()
                again = self.run_installer("--provider", provider, hooks_file=settings)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertEqual(settings.read_bytes(), before)

    def test_preserves_unrelated_hooks_and_replaces_old_farm_handlers(self) -> None:
        self.hooks_file.parent.mkdir(parents=True)
        existing = {
            "description": "keep me",
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [
                            {"type": "command", "command": "echo unrelated"},
                            {
                                "type": "command",
                                "command": "python3 /old/codex-session-hook.py",
                                "timeout": 9,
                            },
                        ],
                    }
                ],
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/old/codex-session-hook.py",
                            }
                        ]
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo post"}],
                    }
                ],
            },
        }
        self.hooks_file.write_text(json.dumps(existing), encoding="utf-8")

        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.read_config()
        self.assertEqual(config["description"], "keep me")
        serialized = json.dumps(config)
        self.assertIn("echo unrelated", serialized)
        self.assertIn("echo post", serialized)
        self.assertNotIn("/old/codex-session-hook.py", serialized)
        self.assertEqual(len(self.farm_handlers(config, "SessionStart")), 1)
        self.assertEqual(len(self.farm_handlers(config, "UserPromptSubmit")), 1)

    def test_can_pin_the_supported_python_command_for_hook_runtime(self) -> None:
        result = self.run_installer("--python-command", "python3.12")

        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.read_config()
        for event in ("SessionStart", "UserPromptSubmit"):
            handlers = self.farm_handlers(config, event)
            self.assertEqual(
                handlers[0]["command"],
                shlex.join(["python3.12", str(HOOK)]),
            )

    def test_check_reports_whether_both_current_handlers_are_installed(self) -> None:
        missing = self.run_installer("--check")
        self.assertEqual(missing.returncode, 1)

        installed = self.run_installer()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        current = self.run_installer("--check")
        self.assertEqual(current.returncode, 0, current.stderr)

        config = self.read_config()
        del config["hooks"]["UserPromptSubmit"]
        self.hooks_file.write_text(json.dumps(config), encoding="utf-8")
        incomplete = self.run_installer("--check")
        self.assertEqual(incomplete.returncode, 1)

    def test_rejects_malformed_or_symlinked_config_without_overwriting(self) -> None:
        self.hooks_file.parent.mkdir(parents=True)
        malformed = "{not json"
        self.hooks_file.write_text(malformed, encoding="utf-8")

        malformed_result = self.run_installer()

        self.assertEqual(malformed_result.returncode, 2)
        self.assertEqual(self.hooks_file.read_text(encoding="utf-8"), malformed)
        self.assertIn("malformed", malformed_result.stderr.lower())

        self.hooks_file.unlink()
        target = self.root / "target.json"
        target.write_text('{"hooks": {}}', encoding="utf-8")
        self.hooks_file.symlink_to(target)

        symlink_result = self.run_installer()

        self.assertEqual(symlink_result.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"hooks": {}}')
        self.assertIn("symlink", symlink_result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
