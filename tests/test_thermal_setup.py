"""Private setup/Windows doubles: no drivers, services, elevation or network."""

import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from codex_looper import thermal, thermal_setup
from codex_looper.resource_config import read_private_json, write_private_json
from tests.test_resource_setup import Terminal

ORIGINAL = "123e4567-e89b-42d3-a456-426614174001"


class ThermalSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sensor = self.root / "temperature.json"
        self.directory = self.root / "staged files"
        self.directory.mkdir()
        self.windows = "D:\\Users\\Someone O'Hare\\thermal setup"
        self.bridge = Mock()
        self.bridge.sensor = self.sensor
        self.bridge.stage.return_value = (self.directory, self.windows)
        self.bridge.script.side_effect = self.script
        self.calls = []
        self.cooling_active = False
        self.before_windows = None

    def sample(self, stamp=None):
        self.sensor.write_text(
            json.dumps(
                {
                    "source": "LibreHardwareMonitor",
                    "checked_at": time.time() if stamp is None else stamp,
                    "cpu_celsius": 62,
                    "helper_version": thermal.SENSOR_VERSION,
                    "helper_sha256": thermal.sensor_digest(),
                }
            )
        )

    def script(self, windows, name, arguments="", **kwargs):
        self.calls.append((name, arguments))
        if name == "setup.ps1":
            if self.before_windows:
                self.before_windows()
            self.sample()
            self.cooling_active = "-Cooling" in arguments
            (self.directory / "setup-result.json").write_text('{"success":true}')
            return "Installed requested choices"
        if "-Action status" in arguments:
            return json.dumps(
                {
                    "active_guid": thermal_setup.COOL_GUID if self.cooling_active else ORIGINAL,
                    "verified": self.cooling_active,
                }
            )
        return "COMPLETE COOLING PLAN: 80% maximum, boost disabled, AC and battery"

    def run_choice(
        self,
        answers="",
        args=(),
        *,
        input_tty=True,
        output_tty=True,
        supported=True,
        bridge_error=None,
    ):
        stdout, stderr = Terminal(tty=output_tty), io.StringIO()
        with (
            patch.object(thermal_setup.sys, "stdin", Terminal(answers, tty=input_tty)),
            patch.object(thermal_setup.sys, "stdout", stdout),
            patch.object(thermal_setup.sys, "stderr", stderr),
            patch.object(thermal_setup, "is_wsl", return_value=supported),
            patch.object(thermal_setup, "_nonroot", return_value=True),
            patch.object(
                thermal_setup, "WindowsBridge", return_value=self.bridge, side_effect=bridge_error
            ) as factory,
            patch.object(thermal, "precheck_service") as precheck,
            patch.object(thermal, "install_service") as install,
        ):
            status = thermal_setup.main(list(args))
        return status, stdout.getvalue() + stderr.getvalue(), factory, precheck, install

    def test_default_unattended_never_probes_windows_or_reads_settings(self):
        for stdin, stdout in ((False, False), (False, True), (True, False)):
            with (
                self.subTest(stdin=stdin, stdout=stdout),
                patch.object(thermal, "load_settings", side_effect=AssertionError("no reads")),
            ):
                status, text, windows, precheck, install = self.run_choice(
                    "yes\nyes\n", input_tty=stdin, output_tty=stdout
                )
                self.assertEqual((status, text), (0, ""))
                windows.assert_not_called()
                precheck.assert_not_called()
                install.assert_not_called()
                self.assertFalse(thermal.settings_path().exists())

    def test_explicit_unattended_choices_only_print_manual_steps(self):
        with patch.object(thermal, "load_settings", side_effect=AssertionError("no reads")):
            status, text, windows, _, install = self.run_choice(
                args=("--with-thermal-protection", "--with-windows-cooling"), input_tty=False
            )
        self.assertEqual(status, 0)
        self.assertIn("interactive WSL terminal", text)
        self.assertIn("--with-thermal-protection --with-windows-cooling", text)
        self.assertNotIn("[y/N]", text)
        windows.assert_not_called()
        install.assert_not_called()
        self.assertFalse(thermal.settings_path().exists())

    def test_decline_enter_eof_and_skip_do_not_change_existing_safeguards(self):
        for answers, args in (
            ("no\n", ()),
            ("\n", ()),
            ("", ()),
            ("invalid\nno\n", ()),
            ("yes\nyes\n", ("--without-thermal-protection", "--without-windows-cooling")),
        ):
            with self.subTest(answers=answers, args=args):
                status, _, windows, precheck, install = self.run_choice(answers, args)
                self.assertEqual(status, 0)
                windows.assert_not_called()
                precheck.assert_not_called()
                install.assert_not_called()
                self.assertFalse(thermal.settings_path().exists())

    def test_normal_rerun_preserves_even_unreadable_old_settings_without_reinstall(self):
        path = thermal.settings_path()
        path.parent.mkdir(mode=0o700, parents=True)
        path.write_bytes(b"existing settings must remain untouched")
        with patch.object(thermal, "load_settings", side_effect=AssertionError("no reads")):
            status, text, windows, _, install = self.run_choice("yes\nyes\n")
        self.assertEqual(status, 0)
        self.assertIn("already configured", text)
        windows.assert_not_called()
        install.assert_not_called()
        self.assertEqual(path.read_bytes(), b"existing settings must remain untouched")

    def test_non_wsl_explicit_choice_fails_without_windows_or_settings_changes(self):
        status, text, windows, precheck, _ = self.run_choice(
            args=("--with-thermal-protection",), supported=False
        )
        self.assertEqual(status, 1)
        self.assertIn("WSL2", text)
        windows.assert_not_called()
        precheck.assert_not_called()
        self.assertFalse(thermal.settings_path().exists())

    def test_confirmed_sensor_setup_checks_hardware_before_enabling_and_preserves_numbers(self):
        original = thermal.ThermalSettings(
            warning_c=77,
            queue_c=82,
            critical_c=88,
            recovery_c=70,
            recovery_seconds=90,
            batch_budget_percent=150,
        )
        write_private_json(thermal.settings_path(), asdict(original))
        self.before_windows = lambda: self.assertEqual(thermal.load_settings(), original)
        status, text, _, precheck, install = self.run_choice(
            args=("--with-thermal-protection", "--without-windows-cooling")
        )
        self.assertEqual(status, 0, text)
        self.assertIn("77 C; queue 82 C; critical 88 C", text)
        self.assertEqual(
            thermal.load_settings(),
            replace(original, enabled=True, enforce=True, sensor_file=str(self.sensor)),
        )
        self.bridge.stage.assert_called_once_with(True)
        self.assertEqual(self.calls, [("setup.ps1", " -InstallSensors")])
        self.assertEqual(precheck.call_count, 2)
        install.assert_called_once_with()
        self.assertEqual(thermal.settings_path().stat().st_mode & 0o777, 0o600)

    def test_plain_interactive_yes_explains_before_choices_and_cooling_no_is_independent(self):
        status, text, _, _, _ = self.run_choice("yes\nno\n")
        self.assertEqual(status, 0, text)
        self.assertLess(text.index("signed PawnIO"), text.index("Set up Windows CPU"))
        self.assertLess(text.index("ALL Windows/WSL"), text.index("Also enable whole-machine"))
        self.assertEqual(self.calls, [("setup.ps1", " -InstallSensors")])

    def test_bridge_failure_precedes_cooling_question_and_all_setup_writes(self):
        with patch.object(thermal, "prepare_private_directory") as prepare:
            status, text, windows, _, install = self.run_choice(
                "yes\nyes\n", bridge_error=ValueError("Missing Windows tool: powershell.exe")
            )
        self.assertEqual(status, 1)
        self.assertIn("Missing Windows tool: powershell.exe", text)
        self.assertNotIn("Also enable whole-machine cooling", text)
        self.assertIn("No Windows installation or power changes were attempted", text)
        self.assertNotIn("may have been applied", text)
        self.assertIn("retry only this step", text)
        retry = text.splitlines()[-1]
        self.assertIn("codex-setup-thermal.py --with-thermal-protection", retry)
        # The user has not been offered cooling yet; retry must still ask.
        self.assertNotIn("windows-cooling", retry)
        windows.assert_called_once_with()
        prepare.assert_not_called()
        install.assert_not_called()
        self.bridge.stage.assert_not_called()
        self.assertFalse(thermal.settings_path().parent.exists())
        self.assertFalse(thermal.state_directory().exists())

    def test_bridge_failure_retry_preserves_explicit_independent_choices(self):
        status, text, _, precheck, install = self.run_choice(
            args=("--without-thermal-protection", "--with-windows-cooling"),
            bridge_error=ValueError("Missing WSL tool: wslpath"),
        )
        self.assertEqual(status, 1)
        self.assertIn("--without-thermal-protection --with-windows-cooling", text.splitlines()[-1])
        precheck.assert_not_called()
        install.assert_not_called()
        self.assertFalse(thermal.state_directory().exists())

    def test_staging_failure_reports_no_windows_changes_and_keeps_cooling_consent(self):
        self.bridge.stage.side_effect = ValueError("Windows package checksum mismatch")
        status, text, _, _, install = self.run_choice("yes\nyes\n")
        self.assertEqual(status, 1)
        self.assertIn("No Windows installation or power changes were attempted", text)
        self.assertNotIn("may have been applied", text)
        self.assertIn("--with-thermal-protection --with-windows-cooling", text.splitlines()[-1])
        self.bridge.script.assert_not_called()
        install.assert_not_called()
        self.assertFalse(thermal.settings_path().exists())

    def test_failed_windows_install_never_enables_linux_guard_or_retries(self):
        self.bridge.script.side_effect = ValueError("Windows task disabled by operator")
        status, text, _, _, install = self.run_choice(
            args=("--with-thermal-protection", "--without-windows-cooling")
        )
        self.assertEqual(status, 1)
        self.assertIn("disabled by operator", text)
        self.assertIn("Windows changes may have been applied", text)
        self.assertIn(str(self.directory / "setup-result.json"), text)
        self.assertIn(str(self.directory / "setup-apply.log"), text)
        self.assertNotIn("No Windows installation", text)
        self.assertIn("--with-thermal-protection --without-windows-cooling", text.splitlines()[-1])
        self.assertEqual(self.bridge.script.call_count, 1)
        install.assert_not_called()
        self.assertFalse(thermal.settings_path().exists())

    def test_no_real_temperature_after_install_refuses_enrollment(self):
        self.before_windows = lambda: None

        def no_temperature(*args, **kwargs):
            (self.directory / "setup-result.json").write_text('{"success":true}')

        self.bridge.script.side_effect = no_temperature
        status, text, _, _, install = self.run_choice(
            args=("--with-thermal-protection", "--without-windows-cooling")
        )
        self.assertEqual(status, 1)
        self.assertIn("No fresh real CPU readings", text)
        install.assert_not_called()
        self.assertFalse(thermal.settings_path().exists())

    def test_masked_service_refuses_before_downloads_or_windows_probes(self):
        with (
            patch.object(thermal_setup.sys, "stdin", Terminal()),
            patch.object(thermal_setup.sys, "stdout", Terminal()),
            patch.object(thermal_setup.sys, "stderr", io.StringIO()),
            patch.object(thermal_setup, "is_wsl", return_value=True),
            patch.object(thermal_setup, "_nonroot", return_value=True),
            patch.object(thermal, "precheck_service", side_effect=ValueError("masked")),
            patch.object(thermal_setup, "WindowsBridge") as windows,
        ):
            status = thermal_setup.main(["--with-thermal-protection", "--without-windows-cooling"])
        self.assertEqual(status, 1)
        windows.assert_not_called()
        self.assertFalse(thermal.settings_path().exists())

    def test_cooling_only_is_separate_and_preserves_custom_original_plan(self):
        status, text, _, precheck, install = self.run_choice(
            args=("--without-thermal-protection", "--with-windows-cooling")
        )
        self.assertEqual(status, 0, text)
        self.assertIn("COMPLETE COOLING PLAN", text)
        self.assertIn("powercfg /setactive " + ORIGINAL, text)
        self.bridge.stage.assert_called_once_with(False)
        self.assertIn(("setup.ps1", " -Cooling -OriginalScheme '" + ORIGINAL + "'"), self.calls)
        precheck.assert_not_called()
        install.assert_not_called()
        self.assertFalse(thermal.settings_path().exists())
        receipt = read_private_json(thermal.state_directory() / "thermal-setup.json")
        self.assertTrue(receipt["cooling_approved"])
        self.assertNotIn("thermal_enabled", receipt)
        self.assertEqual(receipt["original_power_scheme"], ORIGINAL)

    def test_healthy_existing_sensors_and_cooling_need_no_elevation_and_keep_undo(self):
        self.sample()
        self.cooling_active = True
        write_private_json(
            thermal.state_directory() / "thermal-setup.json",
            {
                "cooling_approved": True,
                "original_power_scheme": ORIGINAL,
            },
        )
        status, text, _, _, install = self.run_choice(
            args=("--with-thermal-protection", "--with-windows-cooling")
        )
        self.assertEqual(status, 0, text)
        self.bridge.stage.assert_called_once_with(False)
        self.assertFalse(any(name == "setup.ps1" for name, _ in self.calls))
        install.assert_called_once_with()
        receipt = read_private_json(thermal.state_directory() / "thermal-setup.json")
        self.assertEqual(receipt["original_power_scheme"], ORIGINAL)

    def test_operator_changes_to_cooling_profile_are_preserved(self):
        self.bridge.script.return_value = json.dumps(
            {"active_guid": thermal_setup.COOL_GUID, "verified": False}
        )
        self.bridge.script.side_effect = None
        status, text, _, _, install = self.run_choice(
            args=("--without-thermal-protection", "--with-windows-cooling")
        )
        self.assertEqual(status, 1)
        self.assertIn("operator settings preserved", text)
        self.assertEqual(self.bridge.script.call_count, 1)
        install.assert_not_called()

    def test_pinned_download_integrity_and_partial_cleanup(self):
        data = b"official test package"
        path = self.root / "package.zip"
        with patch.object(thermal_setup.urllib.request, "urlopen", return_value=io.BytesIO(data)):
            thermal_setup.fetch_package(
                path, "https://example.invalid/package", hashlib.sha256(data).hexdigest()
            )
        self.assertEqual(path.read_bytes(), data)
        path.unlink()
        with (
            patch.object(thermal_setup.urllib.request, "urlopen", return_value=io.BytesIO(data)),
            self.assertRaisesRegex(ValueError, "checksum"),
        ):
            thermal_setup.fetch_package(path, "https://example.invalid/package", "0" * 64)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix(".zip.partial").exists())

    def test_download_time_budget_removes_partial_data_before_elevation(self):
        path = self.root / "package.zip"
        with (
            patch.object(thermal_setup.urllib.request, "urlopen", return_value=io.BytesIO(b"data")),
            patch.object(thermal_setup.time, "monotonic", side_effect=[0, 121]),
            self.assertRaisesRegex(ValueError, "time budget"),
        ):
            thermal_setup.fetch_package(path, "https://example.invalid/package", "0" * 64)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix(".zip.partial").exists())

    def test_windows_path_quoting_and_exact_encoded_powershell_argv(self):
        self.assertEqual(thermal_setup.ps_literal("A O'Hare $value"), "'A O''Hare $value'")
        with self.assertRaises(ValueError):
            thermal_setup.ps_literal("bad\npath")
        code = "Write-Output 'literal $value'"
        with patch.object(
            thermal_setup.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "ok", ""),
        ) as run:
            self.assertEqual(thermal_setup.powershell("/ps with spaces", code), "ok")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "/ps with spaces")
        decoded = thermal_setup.base64.b64decode(argv[-1]).decode("utf-16le")
        self.assertIn("UTF8Encoding", decoded)
        self.assertTrue(decoded.endswith(code))
        self.assertNotIn("shell", run.call_args.kwargs)


class WindowsBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "custom Windows mount"
        self.exe = str(self.root / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        self.converter = "/test tools/wslpath"
        self.sensor = self.root / "ProgramData/CodexFarm/Thermal/temperature.json"
        self.executables = {
            "powershell.exe": self.exe,
            "wslpath": self.converter,
            self.exe: self.exe,
        }
        self.stdout = io.StringIO()
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        self.which = contexts.enter_context(
            patch.object(thermal_setup.shutil, "which", side_effect=self.executables.get)
        )
        self.run = contexts.enter_context(
            patch.object(thermal_setup.subprocess, "run", side_effect=self.convert)
        )
        self.powershell = contexts.enter_context(
            patch.object(
                thermal_setup,
                "powershell",
                return_value=json.dumps(
                    {
                        "local_app_data": r"C:\Users\O'Hare\AppData\Local",
                        "program_data": r"C:\ProgramData",
                    }
                ),
            )
        )
        contexts.enter_context(patch.object(thermal_setup.sys, "stdout", self.stdout))

    def convert(self, argv, **kwargs):
        if argv == [self.converter, "-u", thermal_setup.POWERSHELL_WINDOWS_PATH]:
            path = self.exe
        elif argv == [self.converter, "-u", r"C:\ProgramData\CodexFarm\Thermal\temperature.json"]:
            path = str(self.sensor)
        else:
            self.fail(f"Unexpected command: {argv!r}")
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], 5)
        return subprocess.CompletedProcess(argv, 0, path + "\n", "")

    def test_path_tools_take_precedence_without_fallback_search(self):
        bridge = thermal_setup.WindowsBridge()
        self.assertEqual(bridge.exe, self.exe)
        self.assertEqual(bridge.wslpath, self.converter)
        self.assertEqual(bridge.sensor, self.sensor)
        self.assertEqual(self.run.call_count, 1)  # Only convert the sensor path.
        self.assertEqual(self.stdout.getvalue(), "")
        self.powershell.assert_called_once()
        self.assertNotIn(unittest.mock.call(self.exe), self.which.call_args_list)

    def test_missing_powershell_on_path_uses_wslpath_and_custom_mount(self):
        del self.executables["powershell.exe"]
        bridge = thermal_setup.WindowsBridge()
        self.assertEqual(bridge.exe, self.exe)
        self.assertEqual(bridge.sensor, self.sensor)
        self.assertEqual(
            self.run.call_args_list[0].args[0],
            [self.converter, "-u", thermal_setup.POWERSHELL_WINDOWS_PATH],
        )
        self.assertIn(self.exe, self.stdout.getvalue())
        self.assertIn("not on PATH", self.stdout.getvalue())
        self.powershell.assert_called_once()

    def test_wslpath_standard_locations_work_with_restricted_path(self):
        del self.executables["wslpath"]
        for candidate in ("/usr/bin/wslpath", "/bin/wslpath"):
            with self.subTest(candidate=candidate):
                self.converter = candidate
                self.executables[candidate] = candidate
                bridge = thermal_setup.WindowsBridge()
                self.assertEqual(bridge.wslpath, candidate)
                self.assertEqual(bridge.sensor, self.sensor)
                del self.executables[candidate]

    def test_missing_wslpath_names_tool_and_suggests_wsl_checks(self):
        del self.executables["wslpath"]
        with self.assertRaises(ValueError) as caught:
            thermal_setup.WindowsBridge()
        message = str(caught.exception)
        self.assertIn("Missing WSL tool: wslpath", message)
        self.assertIn("command -v wslpath", message)
        self.assertIn("/usr/bin/wslpath", message)
        self.assertNotIn("Missing Windows tool", message)
        self.run.assert_not_called()
        self.powershell.assert_not_called()

    def test_missing_powershell_reports_searched_path_and_mount_guidance(self):
        del self.executables["powershell.exe"]
        del self.executables[self.exe]
        with self.assertRaises(ValueError) as caught:
            thermal_setup.WindowsBridge()
        message = str(caught.exception)
        self.assertIn("Missing Windows tool: powershell.exe", message)
        self.assertIn(self.exe, message)
        self.assertIn("Windows system drive is mounted", message)
        self.assertIn("(Get-Command powershell.exe).Source", message)
        self.assertNotIn("Missing WSL tool", message)
        self.powershell.assert_not_called()

    def test_failed_read_only_probe_explains_interop_and_never_retries(self):
        for error in (
            OSError(8, "Exec format error"),
            subprocess.TimeoutExpired(self.exe, 30),
            ValueError("Windows setup command failed: access denied"),
        ):
            with self.subTest(error=error):
                self.powershell.reset_mock()
                self.powershell.side_effect = error
                with self.assertRaises(ValueError) as caught:
                    thermal_setup.WindowsBridge()
                message = str(caught.exception)
                self.assertIn("read-only Windows check failed", message)
                self.assertIn("fresh WSL terminal", message)
                self.assertIn("/etc/wsl.conf", message)
                self.assertIn("'$PSVersionTable.PSVersion'", message)
                self.assertIn(self.exe, message)
                self.powershell.assert_called_once()
                self.run.assert_not_called()

    def test_bad_path_conversion_stops_before_powershell_with_reproducible_check(self):
        del self.executables["powershell.exe"]
        for result in (
            subprocess.CompletedProcess([], 1, "", "drive not mounted"),
            subprocess.CompletedProcess([], 0, "relative/path", ""),
            subprocess.CompletedProcess([], 0, "/first\n/second", ""),
        ):
            with self.subTest(result=result):
                self.run.side_effect = None
                self.run.return_value = result
                with self.assertRaises(ValueError) as caught:
                    thermal_setup.WindowsBridge()
                message = str(caught.exception)
                self.assertIn("wslpath could not convert", message)
                self.assertIn("Windows drive mount", message)
                self.assertIn("'/test tools/wslpath' -u", message)
                self.powershell.assert_not_called()
