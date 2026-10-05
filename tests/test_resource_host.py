"""Host protection is exercised only against private paths and manager doubles."""

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CLI = Path(__file__).resolve().parents[1] / "bin/codex-resource-host"
if CLI.exists():
    spec = importlib.util.spec_from_loader(
        "resource_host", importlib.machinery.SourceFileLoader("resource_host", str(CLI))
    )
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
else:
    host = None


class Manager:
    def __init__(self):
        self.values = {}
        self.masks = set()
        self.calls = []
        self.fail = None

    def get(self, user, unit, prop):
        default = (
            "[not set]"
            if prop in {"CPUWeight", "IOWeight"}
            else "0"
            if prop == "MemoryLow"
            else "100"
        )
        return self.values.get((user, unit, prop), default)

    def masked(self, user, unit):
        return (user, unit) in self.masks

    def active(self, user, unit):
        return self.values.get((user, unit, "ActiveState"), "active") == "active"

    def set(self, user, unit, prop, value):
        if prop in {"CPUWeight", "IOWeight"}:
            valid = value == "" or (prop == "CPUWeight" and value == "idle")
            valid = valid or (str(value).isdecimal() and 1 <= int(value) <= 10000)
            if not valid:
                raise RuntimeError(f"InvalidArgument: {prop}={value}")
        self.calls.append(("set", user, unit, prop, value))
        if self.fail == unit:
            self.fail = None
            raise RuntimeError("manager failure")
        self.values[user, unit, prop] = "[not set]" if value == "" else str(value)

    def command(self, user, *args):
        self.calls.append(("command", user, *args))


class HostTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(host, "standalone administrative helper is missing")
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = Manager()
        self.helper = host.Host(host.Paths(self.root), self.manager, owner=os.getuid())
        real_write = host.os.write

        def proc_write(fd, data):
            count = real_write(fd, data)
            if "/proc/" in os.readlink(f"/proc/self/fd/{fd}"):
                os.ftruncate(fd, count)  # Fake files emulate proc's replace-on-write semantics.
            return count

        writes = patch.object(host.os, "write", side_effect=proc_write)
        writes.start()
        self.addCleanup(writes.stop)
        self.privilege = patch.object(host.os, "geteuid", return_value=0)
        self.privilege.start()
        self.addCleanup(self.privilege.stop)

    def process(self, pid=123, uid=1003, ticks=12345, cgroup=None, score=0):
        path = self.root / "proc" / str(pid)
        path.mkdir(parents=True, exist_ok=True)
        (path / "stat").write_text(f"{pid} (agent (test)) S " + "0 " * 18 + f"{ticks} 0\n")
        (path / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (path / "cgroup").write_text("0::" + (cgroup or self.agent_group()) + "\n")
        (path / "oom_score_adj").write_text(str(score))
        return path

    def agent_group(self, uid=1003):
        return f"/user.slice/user-{uid}.slice/user@{uid}.service/codexfarm.slice/codexfarm-interactive.slice/codexfarm-agent-{'a' * 32}.scope"

    def test_plan_is_readonly_and_complete(self):
        before = list(self.root.rglob("*"))
        plan = self.helper.plan(1003, maintenance=True)
        self.assertEqual(list(self.root.rglob("*")), before)
        self.assertEqual(self.manager.calls, [])
        text = json.dumps(plan)
        for expected in [
            "1073741824",
            "user.slice",
            "user-1003.slice",
            "user@1003.service",
            "codexfarm-interactive.slice",
            "/etc/systemd/user/",
            "/var/backups/",
            "rollback",
            "200",
            "25",
            "-250",
            "250",
        ]:
            self.assertIn(expected, text)
        for cap in ["MemoryMax", "MemoryHigh", "MemorySwapMax", "CPUQuota", "TasksMax"]:
            self.assertNotIn(cap, text)

    def test_apply_requires_root_before_any_mutation(self):
        with patch.object(host.os, "geteuid", return_value=1003):
            with self.assertRaisesRegex(host.Error, "root"):
                self.helper.apply(1003)
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_hierarchy_weights_stronger_unknown_and_standalone_install(self):
        self.manager.values[False, "user.slice", "MemoryLow"] = "infinity"
        self.manager.values[False, "user-1003.slice", "MemoryLow"] = "2147483648"
        self.manager.values[True, "codexfarm.slice", "MemoryLow"] = "unknown"
        self.helper.apply(1003)
        self.assertEqual(self.manager.get(False, "user.slice", "MemoryLow"), "infinity")
        self.assertEqual(self.manager.get(False, "user-1003.slice", "MemoryLow"), "2147483648")
        self.assertEqual(self.manager.get(True, "codexfarm.slice", "MemoryLow"), "unknown")
        self.assertEqual(self.manager.get(False, "user@1003.service", "MemoryLow"), str(1 << 30))
        self.assertEqual(self.manager.get(True, "codexfarm-interactive.slice", "CPUWeight"), "200")
        self.assertEqual(self.manager.get(True, "codexfarm-batch.slice", "IOWeight"), "25")
        installed = self.root / "usr/local/libexec/codexfarm-resource-protection.py"
        self.assertEqual(installed.read_bytes(), CLI.read_bytes())
        self.assertEqual(installed.stat().st_mode & 0o777, 0o755)
        self.assertNotIn("codex_looper", installed.read_text())
        config = self.root / "etc/codexfarm-resource-protection.json"
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertFalse(
            (self.root / "etc/systemd/system/codexfarm-resource-protection.timer").exists()
        )

    def test_all_masks_prechecked_before_mutation(self):
        for user, unit in [
            (False, "user.slice"),
            (False, "user@1003.service"),
            (True, "codexfarm-batch.slice"),
            (False, "codexfarm-resource-protection.timer"),
        ]:
            with self.subTest(unit=unit):
                self.manager.masks = {(user, unit)}
                with self.assertRaisesRegex(host.Error, "masked"):
                    self.helper.apply(1003, maintenance=True)
                self.assertEqual(list(self.root.rglob("*")), [])
                self.assertEqual(self.manager.calls, [])

    def test_apply_idempotent_and_remove_restores_only_ours(self):
        original = (
            self.root / "etc/systemd/system/user.slice.d/90-codexfarm-resource-protection.conf"
        )
        original.parent.mkdir(parents=True)
        original.write_text("# previous own file\n")
        unrelated = original.with_name("99-operator.conf")
        unrelated.write_text("# untouched\n")
        self.helper.apply(1003, maintenance=True)
        first = len(self.manager.calls)
        self.helper.apply(1003, maintenance=True)
        self.assertEqual(len(self.manager.calls), first)
        changed = (
            self.root
            / "etc/systemd/user/codexfarm-batch.slice.d/90-codexfarm-resource-protection.conf"
        )
        changed.write_text("# later operator edit\n")
        self.manager.values[True, "codexfarm-interactive.slice", "CPUWeight"] = "800"
        self.helper.remove()
        self.helper.remove()
        self.assertEqual(original.read_text(), "# previous own file\n")
        self.assertEqual(unrelated.read_text(), "# untouched\n")
        self.assertEqual(changed.read_text(), "# later operator edit\n")
        self.assertEqual(self.manager.get(True, "codexfarm-interactive.slice", "CPUWeight"), "800")
        self.assertFalse(any("restart" in call or "unmask" in call for call in self.manager.calls))

    def test_manager_failure_rolls_back_files_and_runtime(self):
        self.manager.fail = "codexfarm-interactive.slice"
        with self.assertRaisesRegex(RuntimeError, "manager failure"):
            self.helper.apply(1003)
        self.assertFalse((self.root / "etc/codexfarm-resource-protection.json").exists())
        self.assertFalse(
            (self.root / "usr/local/libexec/codexfarm-resource-protection.py").exists()
        )
        self.assertEqual(self.manager.get(False, "user@1003.service", "MemoryLow"), "0")

    def test_pid_targets_rejected_before_mutation(self):
        for uid, ticks in [(1004, 12345), (1003, 999)]:
            proc = self.process(uid=uid, ticks=ticks)
            before = set(self.root.rglob("*"))
            with self.assertRaisesRegex(host.Error, "identity"):
                self.helper.apply(1003, targets=["123:12345"])
            self.assertEqual(set(self.root.rglob("*")), before)
            self.assertEqual((proc / "oom_score_adj").read_text(), "0")
            self.assertEqual(self.manager.calls, [])

    def test_session_one_shot_and_pid_restore_identity(self):
        proc = self.process(cgroup="/user.slice/user-1003.slice/session-42.scope")
        self.helper.apply(1003, targets=["123:12345"])
        self.assertEqual((proc / "oom_score_adj").read_text(), "-250")
        self.assertEqual(self.manager.get(False, "session-42.scope", "MemoryLow"), str(1 << 30))
        self.process(ticks=54321, score=-250)
        self.helper.remove()
        self.assertEqual((proc / "oom_score_adj").read_text(), "-250")

    def test_maintenance_exact_uid_scope_and_stronger_scores(self):
        agent = self.process()
        batch = self.process(
            pid=124,
            cgroup=self.agent_group().replace("interactive", "batch").replace("agent-", "batch-"),
        )
        wrong = self.process(pid=125, uid=1004)
        unrelated = self.process(pid=126, cgroup="/user.slice/user-1003.slice/session-1.scope")
        prefix = self.process(pid=127, cgroup=self.agent_group() + "-evil/sub")
        child = self.process(pid=128, cgroup=self.agent_group() + "/nested")
        stronger = self.process(pid=129, score=-500)
        self.helper.apply(1003, maintenance=True)
        self.helper.maintain()
        for path, score in [
            (agent, -250),
            (batch, 250),
            (wrong, 0),
            (unrelated, 0),
            (prefix, 0),
            (child, -250),
            (stronger, -500),
        ]:
            self.assertEqual(int((path / "oom_score_adj").read_text()), score)
        (batch / "oom_score_adj").write_text("300")
        self.helper.remove()
        self.assertEqual((agent / "oom_score_adj").read_text(), "0")
        self.assertEqual((batch / "oom_score_adj").read_text(), "300")

    def test_maintain_and_remove_reject_untrusted_config_or_code(self):
        self.helper.apply(1003, maintenance=True)
        config = self.root / "etc/codexfarm-resource-protection.json"
        config.chmod(0o666)
        for action in [self.helper.maintain, self.helper.remove]:
            with self.assertRaisesRegex(host.Error, "trust"):
                action()
        config.chmod(0o600)
        code = self.root / "usr/local/libexec/codexfarm-resource-protection.py"
        code.chmod(0o777)
        with self.assertRaisesRegex(host.Error, "trust"):
            self.helper.maintain()

    def test_file_write_failure_rolls_back(self):
        original = self.helper.write

        def fail(name, data, mode=0o600):
            if name == host.CONFIG:
                raise OSError("disk failure")
            return original(name, data, mode)

        with patch.object(self.helper, "write", side_effect=fail):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.helper.apply(1003)
        self.assertFalse(self.root.joinpath(host.CODE.lstrip("/")).exists())
        self.assertEqual(self.manager.get(False, "user.slice", "MemoryLow"), "0")

    def test_stable_proc_descriptor_does_not_write_replacement_pid(self):
        original = self.process()
        process = host.Process(self.helper.paths, 123)
        self.addCleanup(process.close)
        identity = process.identity()
        original.rename(original.with_name("old"))
        replacement = self.process(ticks=999)
        process.write(identity, 0, -250)
        self.assertEqual((replacement / "oom_score_adj").read_text(), "0")

    def test_maintain_scan_is_bounded_including_record_revalidation(self):
        for pid in range(100, 109):
            self.process(pid=pid)
        self.helper.apply(1003, maintenance=True)
        with patch.object(host, "LIMIT", 4):
            self.helper.maintain()
            calls = []
            original = host.Process.identity

            def identity(process):
                calls.append(process.pid)
                return original(process)

            with patch.object(host.Process, "identity", identity):
                self.helper.maintain()
            self.assertLessEqual(len(calls), 4)

    def test_maintain_journal_write_failures_propagate(self):
        proc = self.process()
        self.helper.apply(1003, maintenance=True)
        real_save = self.helper.save
        calls = 0

        def save(state):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("journal failed")
            real_save(state)

        with patch.object(self.helper, "save", side_effect=save):
            with self.assertRaisesRegex(OSError, "journal failed"):
                self.helper.maintain()
        self.assertEqual((proc / "oom_score_adj").read_text(), "0")

    def test_remove_preserves_operator_timer_mask(self):
        self.helper.apply(1003, maintenance=True)
        timer = self.root / "etc/systemd/system/codexfarm-resource-protection.timer"
        timer.unlink()
        timer.symlink_to("/dev/null")
        self.manager.masks.add((False, host.TIMER))
        self.manager.calls.clear()
        self.helper.remove()
        self.assertTrue(timer.is_symlink())
        self.assertFalse(any("disable" in call for call in self.manager.calls))

    def test_private_state_and_backup_directory_modes(self):
        self.helper.apply(1003)
        for relative in [
            "var/lib/codexfarm-resource-protection",
            "var/backups/codexfarm-resource-protection-originals",
        ]:
            self.assertEqual((self.root / relative).stat().st_mode & 0o777, 0o700)
        state = self.root / host.STATE.lstrip("/")
        state.chmod(0o644)
        with self.assertRaisesRegex(host.Error, "trust"):
            self.helper.remove()

    def test_cli_rejects_path_overrides_and_plan_never_calls_mutators(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            host.main(["plan", "--uid", "1003", "--root", str(self.root)])
        for command in ["apply", "maintain", "remove"]:
            with patch.object(host.os, "geteuid", return_value=1003):
                with self.assertRaisesRegex(host.Error, "root"):
                    getattr(self.helper, command)(*([1003] if command == "apply" else []))

    def test_mask_probe_drops_uid_and_manager_uses_sanitized_absolute_command(self):
        import subprocess

        manager = host.Systemd(1003)

        def run(argv, **kwargs):
            self.assertEqual(
                argv[0], "/usr/bin/systemctl" if "-c" not in argv else "/usr/bin/python3"
            )
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            if "-c" in argv:
                self.assertEqual(kwargs["user"], 1003)
                self.assertEqual(kwargs["extra_groups"], [])
                self.assertIn("-I", argv)
                return subprocess.CompletedProcess(argv, 0, "1", "")
            return subprocess.CompletedProcess(
                argv, 0, "loaded" if "--property=LoadState" in argv else "", ""
            )

        with patch.object(host.subprocess, "run", side_effect=run):
            self.assertTrue(manager.masked(True, "codexfarm.slice"))
        with (
            patch.object(host.os, "geteuid", return_value=1003),
            patch.object(host.subprocess, "run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0, "0", "")
            manager.get(True, "codexfarm.slice", "MemoryLow")
            self.assertEqual(
                run.call_args.kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"],
                "unix:path=/run/user/1003/bus",
            )
        with patch.object(
            host.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "", "bus unavailable"),
        ):
            with self.assertRaisesRegex(host.Error, "bus unavailable"):
                manager.get(True, "codexfarm.slice", "MemoryLow")

    def test_remove_keeps_trusted_recovery_material_if_runtime_undo_fails(self):
        self.helper.apply(1003)
        self.manager.fail = "user@1003.service"
        with self.assertRaisesRegex(host.Error, "rollback incomplete"):
            self.helper.remove()
        self.assertTrue(self.root.joinpath(host.CODE.lstrip("/")).exists())
        self.assertTrue(self.root.joinpath(host.CONFIG.lstrip("/")).exists())
        self.helper.remove()
        self.assertFalse(self.root.joinpath(host.STATE.lstrip("/")).exists())

    def test_timer_activation_link_is_planned_and_operator_changes_survive(self):
        plan = json.dumps(self.helper.plan(1003, maintenance=True))
        self.assertIn("timers.target.wants/codexfarm-resource-protection.timer", plan)
        self.helper.apply(1003, maintenance=True)
        enabled = (
            self.root / "etc/systemd/system/timers.target.wants/codexfarm-resource-protection.timer"
        )
        self.assertTrue(enabled.is_symlink())
        enabled.unlink()
        enabled.symlink_to("/etc/systemd/system/operator.timer")
        self.helper.remove()
        self.assertEqual(os.readlink(enabled), "/etc/systemd/system/operator.timer")

    def test_clock_budget_stops_before_scanning_processes(self):
        proc = self.process()
        self.helper.apply(1003, maintenance=True)
        with patch.object(host.time, "monotonic", side_effect=[0, 3]):
            self.helper.maintain()
        self.assertEqual((proc / "oom_score_adj").read_text(), "0")

    def test_maintenance_rejects_symlink_state_and_installed_helper(self):
        self.helper.apply(1003, maintenance=True)
        state = self.root / host.STATE.lstrip("/")
        state.rename(state.with_suffix(".old"))
        state.symlink_to(state.with_suffix(".old"))
        with self.assertRaisesRegex(host.Error, "trust"):
            self.helper.maintain()

    def test_plan_existing_session_includes_runtime_protection(self):
        self.process(cgroup="/user.slice/user-1003.slice/session-42.scope")
        plan = self.helper.plan(1003, targets=["123:12345"])
        self.assertTrue(
            any(
                item["unit"] == "session-42.scope" and item["value"] == str(1 << 30)
                for item in plan["runtime"]
            )
        )
        self.assertFalse(self.manager.calls)

    def test_existing_private_directory_cannot_be_world_readable(self):
        parent = self.root / "var/lib/codexfarm-resource-protection"
        parent.mkdir(parents=True, mode=0o755)
        with self.assertRaisesRegex(host.Error, "trust"):
            self.helper.apply(1003)

    def test_failed_apply_journal_cannot_be_mistaken_for_success(self):
        self.helper.apply(1003)
        statepath = self.root / host.STATE.lstrip("/")
        state = json.loads(statepath.read_text())
        state["phase"] = "applying"
        statepath.write_text(json.dumps(state))
        with self.assertRaisesRegex(host.Error, "incomplete"):
            self.helper.apply(1003)

    def test_all_mask_search_locations_and_user_probe_fail_closed(self):
        import subprocess

        manager = host.Systemd(1003)
        for base in [
            "/etc/systemd/system",
            "/run/systemd/system",
            "/usr/lib/systemd/system",
            "/etc/systemd/system.control",
            "/run/systemd/system.control",
        ]:
            with (
                self.subTest(base=base),
                patch.object(manager, "get", return_value="loaded"),
                patch.object(
                    host.Path,
                    "is_symlink",
                    lambda path, base=base: str(path) == base + "/user.slice",
                ),
                patch.object(host.Path, "resolve", return_value=Path("/dev/null")),
            ):
                self.assertTrue(manager.masked(False, "user.slice"))
        for base in ["/etc/systemd/user", "/run/systemd/user", "/usr/lib/systemd/user"]:

            def run(argv, base=base, **kwargs):
                self.assertIn(base, argv)
                return subprocess.CompletedProcess(argv, 0, "1", "")

            with (
                patch.object(manager, "get", return_value="loaded"),
                patch.object(manager, "command", return_value=""),
                patch.object(host.subprocess, "run", side_effect=run),
            ):
                self.assertTrue(manager.masked(True, "codexfarm.slice"))
        with (
            patch.object(manager, "get", return_value="loaded"),
            patch.object(manager, "command", return_value=""),
            patch.object(
                host.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "bad")
            ),
        ):
            with self.assertRaisesRegex(host.Error, "safely check"):
                manager.masked(True, "codexfarm.slice")

    def test_same_one_shot_apply_is_idempotent(self):
        self.process()
        self.helper.apply(1003, targets=["123:12345"])
        before = list(self.manager.calls)
        self.helper.apply(1003, targets=["123:12345"])
        self.assertEqual(self.manager.calls, before)

    def test_lock_creation_never_atomically_replaces_lock_inode(self):
        with patch.object(
            self.helper, "write", side_effect=AssertionError("lock must use a stable inode")
        ):
            with self.helper.locked():
                self.assertTrue(self.root.joinpath(host.LOCK.lstrip("/")).exists())

    def test_remove_leaves_later_operator_timer_content_running(self):
        self.manager.values[False, host.TIMER, "ActiveState"] = "inactive"
        self.helper.apply(1003, maintenance=True)
        timer = self.root / "etc/systemd/system/codexfarm-resource-protection.timer"
        timer.write_text("# operator timer replacement\n")
        self.manager.calls.clear()
        self.helper.remove()
        self.assertFalse(any("stop" in call for call in self.manager.calls))
        self.assertEqual(timer.read_text(), "# operator timer replacement\n")

    def test_partial_install_can_be_removed_after_rollback_connection_failure(self):
        write = self.helper.write

        def fail(name, data, mode=0o600):
            if name == host.CONFIG:
                raise OSError("config disk failure")
            return write(name, data, mode)

        with (
            patch.object(self.helper, "write", side_effect=fail),
            patch.object(self.manager, "command", side_effect=RuntimeError("bus unavailable")),
        ):
            with self.assertRaisesRegex(host.Error, "rollback incomplete"):
                self.helper.apply(1003)
        self.helper.remove()
        self.assertFalse(self.root.joinpath(host.STATE.lstrip("/")).exists())

    def test_proc_parser_rejects_incomplete_identity(self):
        proc = self.process()
        (proc / "stat").write_text("")
        with self.assertRaisesRegex(host.Error, "identity"):
            self.helper.apply(1003, targets=["123:12345"])
        self.assertFalse(self.manager.calls)

    def test_process_write_failure_reports_and_keeps_restoration_journal(self):
        self.process()
        self.helper.apply(1003, maintenance=True)
        with patch.object(host.Process, "write", side_effect=PermissionError("OOM write denied")):
            with self.assertRaisesRegex(OSError, "OOM write denied"):
                self.helper.maintain()
        self.helper.maintain()
        # Simulate the journaled write reaching proc before the caller lost access.
        (self.root / "proc/123/oom_score_adj").write_text("-250")
        with patch.object(host.Process, "write", side_effect=PermissionError("OOM restore denied")):
            with self.assertRaisesRegex(host.Error, "OOM restore denied"):
                self.helper.remove()
        self.assertTrue(self.root.joinpath(host.STATE.lstrip("/")).exists())
        self.helper.remove()

    def test_remove_restores_all_unset_weights_with_empty_assignments(self):
        self.helper.apply(1003)
        state = json.loads(self.root.joinpath(host.STATE.lstrip("/")).read_text())
        weights = [item for item in state["runtime"] if item["prop"] in {"CPUWeight", "IOWeight"}]
        self.assertEqual(len(weights), 4)
        self.assertTrue(all(item["original"] == "[not set]" for item in weights))
        self.manager.calls.clear()
        self.helper.remove()
        for item in weights:
            self.assertEqual(
                self.manager.get(item["user"], item["unit"], item["prop"]), "[not set]"
            )
            self.assertIn(("set", item["user"], item["unit"], item["prop"], ""), self.manager.calls)
        self.assertFalse(self.root.joinpath(host.STATE.lstrip("/")).exists())

    def test_failed_apply_restores_unset_interactive_weights(self):
        self.manager.fail = "codexfarm-batch.slice"
        with self.assertRaisesRegex(RuntimeError, "^manager failure$"):
            self.helper.apply(1003)
        for unit in ("codexfarm-interactive.slice", "codexfarm-batch.slice"):
            for prop in ("CPUWeight", "IOWeight"):
                self.assertEqual(self.manager.get(True, unit, prop), "[not set]")
        for prop in ("CPUWeight", "IOWeight"):
            self.assertIn(
                ("set", True, "codexfarm-interactive.slice", prop, ""), self.manager.calls
            )
        self.assertFalse(self.root.joinpath(host.STATE.lstrip("/")).exists())
        self.assertFalse(self.root.joinpath(host.CONFIG.lstrip("/")).exists())

    def test_remove_preserves_idle_numeric_and_later_operator_weights(self):
        self.manager.values[True, "codexfarm-interactive.slice", "CPUWeight"] = "idle"
        self.manager.values[True, "codexfarm-interactive.slice", "IOWeight"] = "457"
        self.helper.apply(1003)
        self.manager.values[True, "codexfarm-batch.slice", "IOWeight"] = "999"
        self.manager.calls.clear()
        self.helper.remove()
        self.assertEqual(self.manager.get(True, "codexfarm-interactive.slice", "CPUWeight"), "idle")
        self.assertEqual(self.manager.get(True, "codexfarm-interactive.slice", "IOWeight"), "457")
        self.assertEqual(self.manager.get(True, "codexfarm-batch.slice", "IOWeight"), "999")
        self.assertFalse(
            any(
                call[:4] == ("set", True, "codexfarm-batch.slice", "IOWeight")
                for call in self.manager.calls
            )
        )

    def test_installed_service_uses_isolated_absolute_interpreter(self):
        self.helper.apply(1003, maintenance=True)
        service = (
            self.root / "etc/systemd/system/codexfarm-resource-protection.service"
        ).read_text()
        self.assertIn(
            "ExecStart=/usr/bin/python3 -I /usr/local/libexec/codexfarm-resource-protection.py maintain",
            service,
        )
        timer = (self.root / "etc/systemd/system/codexfarm-resource-protection.timer").read_text()
        self.assertIn("OnUnitActiveSec=30s", timer)


if __name__ == "__main__":
    unittest.main()
