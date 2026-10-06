"""Setup consent tests use private storage and never call host administration."""

import io
import os
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_looper import resource_setup
from codex_looper.resource_config import (
    ResourceSettings,
    load_settings,
    settings_path,
    write_settings,
)


class Terminal(io.StringIO):
    def __init__(self, text="", *, tty=True):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


class ResourceSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ,
            {"HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "config")},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.helper = self.root / "bin with spaces" / "codex-resource-host"
        self.uid = os.getuid()

    def run_choice(self, answers="", args=(), *, input_tty=True, output_tty=True, results=None):
        stdin, stdout, stderr = (
            Terminal(answers, tty=input_tty),
            Terminal(tty=output_tty),
            io.StringIO(),
        )
        if results is None:
            results = [subprocess.CompletedProcess([], 0, '{"complete":"plan"}\n', "")]
        with (
            patch.object(resource_setup.sys, "stdin", stdin),
            patch.object(resource_setup.sys, "stdout", stdout),
            patch.object(resource_setup.sys, "stderr", stderr),
            patch.object(resource_setup.subprocess, "run", side_effect=results) as host,
            patch.object(resource_setup.sys, "platform", "linux"),
            patch.object(resource_setup.os, "access", return_value=True),
        ):
            status = resource_setup.main(list(args), helper_path=self.helper)
        return status, stdout.getvalue() + stderr.getvalue(), host

    def original(self):
        settings = ResourceSettings(
            protect_agents=False,
            queue_background=False,
            investigator="codex",
            automatic_actions=True,
            reserve_mib=1100,
            recovery_mib=1700,
            recovery_seconds=12,
            queue_timeout=42,
            investigator_model="other-model",
            investigator_binary="/private/codex",
        )
        write_settings(settings)
        return settings

    def test_default_unattended_needs_both_terminal_streams_and_reads_nothing(self):
        for input_tty, output_tty in ((False, False), (True, False), (False, True)):
            with (
                self.subTest(stdin=input_tty, stdout=output_tty),
                patch.object(
                    resource_setup, "load_settings", side_effect=AssertionError("must not read")
                ),
            ):
                status, text, host = self.run_choice(
                    "yes\nyes\n", input_tty=input_tty, output_tty=output_tty
                )
                self.assertEqual(status, 0)
                self.assertEqual(text, "")
                host.assert_not_called()
                self.assertFalse(settings_path().exists())

    def test_decline_empty_eof_and_skip_keep_existing_bytes_and_do_not_read(self):
        self.original()
        before = settings_path().read_bytes()
        for answers, args in (
            ("n\n", ()),
            ("\n", ()),
            ("", ()),
            ("yes\nyes\n", ("--without-memory-protection",)),
        ):
            with (
                self.subTest(answers=answers, args=args),
                patch.object(
                    resource_setup, "load_settings", side_effect=AssertionError("must not read")
                ),
            ):
                status, text, host = self.run_choice(answers, args)
                self.assertEqual(status, 0)
                self.assertEqual(settings_path().read_bytes(), before)
                host.assert_not_called()
                if not args:
                    self.assertIn("keep", text.lower())
                    self.assertIn("not uninstall", text.lower())

    def test_explanation_precedes_prompt_and_invalid_answers_retry(self):
        status, text, host = self.run_choice("maybe\nYES\nno\n")
        self.assertEqual(status, 0)
        prompt = "Enable gentle memory protection"
        for phrase in (
            "1 GiB",
            "used memory",
            "Linux/systemd",
            "empty RAM",
            "OOM immunity",
            "competing batch",
            "1024 MiB",
            "1536 MiB",
            "30 seconds",
            "5 minutes",
            "never stopped",
            "future managed launches",
            "does not move current agents",
            "AI",
        ):
            self.assertIn(phrase, text[: text.index(prompt)])
        self.assertEqual(text.count(prompt), 2)
        self.assertEqual(host.call_count, 1)
        self.assertTrue(load_settings().protect_agents)
        self.assertTrue(load_settings().queue_background)

    def test_enable_preserves_other_settings_and_private_permissions(self):
        original = self.original()
        status, text, host = self.run_choice("y\nn\n")
        self.assertEqual(status, 0)
        self.assertEqual(
            load_settings(), replace(original, protect_agents=True, queue_background=True)
        )
        self.assertEqual(stat.S_IMODE(settings_path().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(settings_path().parent.stat().st_mode), 0o700)
        self.assertIn("existing", text)
        self.assertEqual(host.call_count, 1)

    def test_preview_complete_output_and_exact_argv_before_separate_apply(self):
        stdout, stdin = Terminal(), Terminal("yes\nyes\n")
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                self.assertNotIn("Apply these system preferences", stdout.getvalue())
                return subprocess.CompletedProcess(argv, 0, "COMPLETE PLAN\nlast entry\n", "")
            self.assertIn("COMPLETE PLAN\nlast entry", stdout.getvalue())
            self.assertIn("Apply these system preferences with sudo now? [y/N]", stdout.getvalue())
            return subprocess.CompletedProcess(argv, 0)

        with (
            patch.object(resource_setup.sys, "stdin", stdin),
            patch.object(resource_setup.sys, "stdout", stdout),
            patch.object(resource_setup.sys, "platform", "linux"),
            patch.object(resource_setup.os, "access", return_value=True),
            patch.object(resource_setup.subprocess, "run", side_effect=run),
        ):
            status = resource_setup.main([], helper_path=self.helper)
        common = ["--uid", str(self.uid), "--with-maintenance"]
        self.assertEqual(
            calls,
            [
                ["/usr/bin/python3", "-I", str(self.helper), "plan", *common],
                ["sudo", "/usr/bin/python3", "-I", str(self.helper), "apply", *common],
            ],
        )
        self.assertEqual(status, 0)
        for phrase in ("ancestor", "30-second", "other users", "chosen UID", "earlier reclaim"):
            self.assertIn(phrase, stdout.getvalue())

    def test_explicit_unattended_enable_saves_only_options_and_prints_manual_commands(self):
        original = self.original()
        status, text, host = self.run_choice(
            "yes\n", ("--with-memory-protection",), input_tty=False
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            load_settings(), replace(original, protect_agents=True, queue_background=True)
        )
        host.assert_not_called()
        self.assertNotIn("? [y/N]", text)
        self.assertIn("/usr/bin/python3 -I", text)
        self.assertIn("sudo /usr/bin/python3 -I", text)
        self.assertIn(f"--uid {self.uid} --with-maintenance", text)
        self.assertIn("system protection not applied", text.lower())

    def test_explicit_interactive_enable_skips_first_prompt_only(self):
        status, text, host = self.run_choice("n\n", ("--with-memory-protection",))
        self.assertEqual(status, 0)
        self.assertNotIn("Enable gentle memory protection", text)
        self.assertIn("Apply these system preferences", text)
        self.assertEqual(host.call_count, 1)

    def test_host_decline_empty_eof_and_invalid_answers_never_sudo(self):
        for answers in ("y\nn\n", "y\n\n", "y\n", "y\ninvalid\nn\n"):
            with self.subTest(answers=answers):
                status, text, host = self.run_choice(answers)
                self.assertEqual(status, 0)
                self.assertEqual(host.call_count, 1)
                self.assertIn("system protection not applied", text.lower())
                self.assertIn("sudo /usr/bin/python3 -I", text)

    def test_failed_preview_never_prompts_or_applies(self):
        for result in (
            subprocess.CompletedProcess([], 1, "partial plan", "failed preview"),
            OSError("helper unavailable"),
        ):
            with self.subTest(result=result):
                status, text, host = self.run_choice("y\ny\n", results=[result])
                self.assertEqual(status, 0)
                self.assertEqual(host.call_count, 1)
                self.assertNotIn("Apply these system preferences", text)
                self.assertIn("system protection not applied", text.lower())
                self.assertIn("sudo /usr/bin/python3 -I", text)

    def test_apply_failure_returns_error_without_recovery_or_reinstall(self):
        for result in (subprocess.CompletedProcess([], 7), OSError("sudo unavailable")):
            with self.subTest(result=result):
                status, text, host = self.run_choice(
                    "y\ny\n", results=[subprocess.CompletedProcess([], 0, "plan", ""), result]
                )
                self.assertNotEqual(status, 0)
                self.assertEqual(host.call_count, 2)
                self.assertIn("user settings saved", text.lower())
                self.assertIn("host apply failed", text.lower())
                self.assertTrue(load_settings().protect_agents)

    def test_unsupported_host_and_root_account_do_not_call_helper(self):
        for target, value in (
            ("sys.platform", "darwin"),
            ("os.getuid", 0),
            ("os.getuid", -1),
            ("os.access", False),
        ):
            with self.subTest(target=target):
                owner, name = target.split(".")
                kwargs = {"return_value": value} if owner == "os" else {"new": value}
                # Real settings validation uses the real UID; isolate only host eligibility.
                with (
                    patch.object(resource_setup, "write_settings"),
                    patch.object(resource_setup, "load_settings", return_value=ResourceSettings()),
                    patch.object(getattr(resource_setup, owner), name, **kwargs),
                ):
                    # run_choice provides Linux/access defaults, so test unsupported checks directly.
                    with (
                        patch.object(resource_setup.sys, "stdin", Terminal("y\ny\n")),
                        patch.object(resource_setup.sys, "stdout", Terminal()) as out,
                        patch.object(resource_setup.subprocess, "run") as host,
                    ):
                        status = resource_setup.main([], helper_path=self.helper)
                    self.assertEqual(status, 0)
                    host.assert_not_called()
                    self.assertIn("system protection not applied", out.getvalue().lower())

    def test_invalid_settings_are_not_replaced_or_followed_by_host_work(self):
        self.original()
        settings_path().write_text('{"unknown":true}\n')
        before = settings_path().read_bytes()
        status, text, host = self.run_choice("y\ny\n")
        self.assertNotEqual(status, 0)
        self.assertEqual(settings_path().read_bytes(), before)
        host.assert_not_called()
        self.assertIn("settings", text.lower())

    def test_conflicting_flags_fail_before_any_settings_or_host_work(self):
        with patch.object(
            resource_setup, "load_settings", side_effect=AssertionError("must not read")
        ):
            with self.assertRaises(SystemExit) as raised:
                self.run_choice(
                    "yes\nyes\n", ("--with-memory-protection", "--without-memory-protection")
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(settings_path().exists())
