import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_looper.health import Memory
from codex_looper.resource_config import ResourceSettings, write_settings

try:
    from codex_looper import resource_jobs as jobs
except ImportError:
    jobs = None

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "bin/codex-job"


class JobTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(jobs, "managed jobs module missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = os.environ.copy()
        self.env.update(
            HOME=str(self.root),
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_STATE_HOME=str(self.root / "state"),
        )
        self.env.pop("CODEXFARM_RESOURCE_PROTECTION", None)
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = jobs.JobStore()

    def run_cli(self, options, argv, **kwargs):
        return subprocess.run(
            [sys.executable, str(CLI), "run", *options, "--", *argv],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=8,
            **kwargs,
        )

    def doubles(self, mode="execute", manager=True):
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        log = self.root / "scope.log"
        systemctl = bindir / "systemctl"
        systemctl.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            + (
                "sys.exit(1)\n"
                if not manager
                else "print('InvocationID=testgeneration' if '--property=InvocationID' in sys.argv else '')\n"
            )
        )
        systemctl.chmod(0o755)
        runner = bindir / "systemd-run"
        runner.write_text(
            "#!/usr/bin/env python3\nimport os,sys,json,subprocess\n"
            f"with open({str(log)!r},'a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')\n"
            + (
                "sys.exit(1)\n"
                if mode == "fail"
                else "argv=sys.argv[sys.argv.index('--')+1:]\nsys.exit(subprocess.call(argv))\n"
            )
        )
        runner.chmod(0o755)
        self.env["PATH"] = str(bindir) + os.pathsep + self.env["PATH"]
        return log

    def test_literal_argv_scope_off_never_probes_manager(self):
        self.doubles(mode="fail")
        argv = ["$value; `data`", "a b", "", "\\", '"quoted"']
        result = self.run_cli(
            ["--scope", "off", "--memory-policy", "ignore"],
            [sys.executable, "-c", "import sys,json; print(json.dumps(sys.argv[1:]))", *argv],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), argv)
        self.assertFalse((self.root / "scope.log").exists())

    def test_agent_bypasses_queue_and_rejects_restart_and_caps(self):
        with patch.object(jobs, "read_memory", side_effect=AssertionError("agent admission")):
            self.assertTrue(jobs.admit("agent", "queue", ResourceSettings(), 0, self.store, "x"))
        for flags in [["--restartable"], ["--memory-high", "20"], ["--memory-max", "20"]]:
            result = self.run_cli(["--role", "agent", "--scope", "off", *flags], ["true"])
            self.assertEqual(result.returncode, 2, result.stderr)

    def test_queue_timeout_manual_bypass_unknown_and_small_advisory(self):
        with patch.object(jobs, "read_memory", return_value=Memory(4096, 500, 0)):
            with patch("sys.stderr") as err:
                self.assertFalse(
                    jobs.admit("batch", "queue", ResourceSettings(), 0, self.store, "none")
                )
            self.assertIn("--memory-policy ignore", str(err.write.call_args_list))
            self.assertTrue(
                jobs.admit("batch", "ignore", ResourceSettings(), 0, self.store, "none")
            )
        for sample in [None, Memory(900, 500, 0)]:
            with patch.object(jobs, "read_memory", return_value=sample):
                self.assertTrue(
                    jobs.admit("batch", "queue", ResourceSettings(), 0, self.store, "none")
                )

    def test_optional_scope_creation_failure_fallback_executes_once(self):
        log = self.doubles(mode="fail")
        count = self.root / "count"
        result = self.run_cli(
            ["--role", "agent"],
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(count)!r}).write_text('once')",
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(count.read_text(), "once")
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.assertEqual(result.stderr.count("falling back"), 1)

    def test_missing_manager_warns_before_execution_once_and_required_fails(self):
        self.doubles(manager=False)
        for flags, expected in [([], 7), (["--scope", "required"], 1), (["--memory-max", "32"], 1)]:
            result = self.run_cli(
                ["--role", "batch", "--memory-policy", "ignore", *flags],
                [sys.executable, "-c", "print('payload'); raise SystemExit(7)"],
            )
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(result.stdout.count("payload"), 1 if expected == 7 else 0)

    def test_scope_properties_unique_names_and_nonzero_child_never_rerun(self):
        log = self.doubles()
        count = self.root / "count"
        code = f"from pathlib import Path; p=Path({str(count)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); raise SystemExit(9)"
        for role in ["agent", "batch"]:
            result = self.run_cli(
                ["--role", role, "--memory-policy", "ignore"], [sys.executable, "-c", code]
            )
            self.assertEqual(result.returncode, 9, result.stderr)
            self.assertNotIn("falling back", result.stderr)
        scopes = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(count.read_text(), "xx")
        for role, args in zip(["agent", "batch"], scopes, strict=False):
            self.assertIn("--expand-environment=no", args)
            self.assertRegex(
                next(x for x in args if x.startswith("--unit=")),
                rf"^--unit=codexfarm-{role}-[a-f0-9]{{32}}\.scope$",
            )
            self.assertIn(
                f"--slice=codexfarm-{'interactive' if role == 'agent' else 'batch'}.slice", args
            )
            self.assertFalse(
                any(
                    x.startswith(
                        (
                            "CPUQuota",
                            "MemoryHigh",
                            "MemoryMax",
                            "MemorySwapMax",
                            "TasksMax",
                            "Nice",
                            "OOMScoreAdjust",
                        )
                    )
                    for x in args
                )
            )
        self.assertIn("MemoryLow=infinity", scopes[0])
        self.assertIn("CPUWeight=25", scopes[1])
        self.assertIn("IOWeight=25", scopes[1])

    def test_explicit_limits_are_batch_only_finite_and_fail_closed(self):
        self.doubles(mode="fail")
        for options in [
            ["--scope", "off", "--memory-max", "100"],
            ["--memory-high", "nan"],
            ["--memory-high", "101", "--memory-max", "100"],
            ["--memory-max", "0"],
        ]:
            result = self.run_cli(["--role", "batch", *options], ["true"])
            self.assertEqual(result.returncode, 2, result.stderr)
        result = self.run_cli(["--role", "batch", "--memory-max", "100"], ["echo", "payload"])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_inner_marker_failure_prevents_execution(self):
        record = self.store.create("agent", ["true"], str(self.root))
        with (
            patch.object(jobs, "write_private_json", side_effect=OSError("disk full")),
            patch.object(jobs.os, "execvpe") as execute,
        ):
            self.assertEqual(jobs.inner_main(self.store, record["job_id"], ["true"], None), 122)
            execute.assert_not_called()

    def test_bus_discovery_owned_socket_and_explicit_custom_environment(self):
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        bus = socket.socket(socket.AF_UNIX)
        self.addCleanup(bus.close)
        bus.bind(str(runtime / "bus"))
        result = jobs.manager_environment({}, runtime_root=runtime)
        self.assertEqual(result["XDG_RUNTIME_DIR"], str(runtime))
        self.assertEqual(result["DBUS_SESSION_BUS_ADDRESS"], "unix:path=" + str(runtime / "bus"))
        custom = {"XDG_RUNTIME_DIR": "/custom", "DBUS_SESSION_BUS_ADDRESS": "custom"}
        self.assertEqual(jobs.manager_environment(custom, runtime_root=runtime), custom)
        with patch.object(jobs.os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(jobs.manager_environment({}, runtime_root=runtime), {})
        bus.close()
        (runtime / "bus").unlink()
        (runtime / "bus").write_text("not a bus")
        self.assertEqual(jobs.manager_environment({}, runtime_root=runtime), {})

    def live_record(self, role="batch", restartable=True, workers=8):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True
        )

        def finish():
            if proc.poll() is None:
                proc.kill()
            proc.wait()

        self.addCleanup(finish)
        record = self.store.create(
            role,
            ["secret command"],
            str(self.root),
            restartable=restartable,
            worker_env="CARGO_BUILD_JOBS",
            workers=workers,
        )
        self.store.register_payload(record["job_id"], proc.pid)
        return proc, self.store.read(record["job_id"])

    def test_private_records_sanitized_metadata_and_pid_identity(self):
        proc, record = self.live_record()
        identity = self.store.identity(record["job_id"])
        self.assertTrue(self.store.validate(record["job_id"], identity))
        public = self.store.public(record["job_id"])
        self.assertNotIn("secret command", json.dumps(public))
        self.assertNotIn("argv", public)
        self.assertNotIn("cwd", public)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(self.store.record_path(record["job_id"]).stat().st_mode), 0o600
        )
        for key, value in [
            ("payload_start_ticks", record["payload_start_ticks"] + 1),
            ("uid", os.getuid() + 1),
            ("cgroup", "wrong"),
        ]:
            changed = dict(record, **{key: value})
            self.store.write(changed)
            self.assertFalse(
                self.store.validate(
                    record["job_id"], {name: changed.get(name) for name in jobs.IDENTITY_FIELDS}
                )
            )
        self.store.write(record)
        proc.terminate()
        proc.wait()
        self.assertFalse(self.store.validate(record["job_id"], identity))

    def test_default_state_directory_migrates_only_owned_real_farm_directory(self):
        state = jobs.state_directory()
        state.mkdir(parents=True, mode=0o775)
        state.chmod(0o775)
        self.store._prepare()
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o700)

    def test_default_state_directory_refuses_symlink_nonregular_and_wrong_owner(self):
        state = jobs.state_directory()
        state.parent.mkdir(parents=True)
        destination = self.root / "destination"
        destination.mkdir(mode=0o755)
        destination.chmod(0o755)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
        state.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store._prepare()
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
        self.assertFalse((destination / "resources").exists())
        state.unlink()
        state.write_text("existing file")
        with self.assertRaises((ValueError, FileExistsError)):
            self.store._prepare()
        self.assertEqual(state.read_text(), "existing file")
        state.unlink()
        state.mkdir(mode=0o755)
        state.chmod(0o755)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o755)
        with patch.object(jobs.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(ValueError):
                self.store._prepare()
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o755)
        self.assertFalse((state / "resources").exists())

    def test_public_resource_leaves_are_not_migrated(self):
        for relative in ("resources", "resources/jobs", "resources/overrides"):
            with self.subTest(relative=relative):
                target = jobs.state_directory() / relative
                target.mkdir(parents=True, mode=0o700, exist_ok=True)
                target.chmod(0o755)
                with self.assertRaises(ValueError):
                    self.store._prepare()
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
                target.chmod(0o700)

    def test_state_directory_replacement_before_open_is_not_chmodded(self):
        state = jobs.state_directory()
        state.mkdir(parents=True, mode=0o700)
        original_open = jobs.os.open
        previous = state.with_name("previous")

        def replace_before_open(path, flags, *args, **kwargs):
            if Path(path) == state:
                state.rename(previous)
                state.mkdir(mode=0o755)
                state.chmod(0o755)
                self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o755)
            return original_open(path, flags, *args, **kwargs)

        with patch.object(jobs.os, "open", side_effect=replace_before_open):
            with self.assertRaises(ValueError):
                self.store._prepare()
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(previous.stat().st_mode), 0o700)
        self.assertFalse((state / "resources").exists())

    def test_state_directory_owner_is_rechecked_on_open_fd(self):
        state = jobs.state_directory()
        state.mkdir(parents=True, mode=0o700)
        original_fstat = jobs.os.fstat

        def changed_owner(fd):
            values = list(original_fstat(fd))
            values[4] += 1
            return os.stat_result(values)

        with patch.object(jobs.os, "fstat", side_effect=changed_owner):
            with self.assertRaises(ValueError):
                self.store._prepare()
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertFalse((state / "resources").exists())

    def test_scoped_generation_and_actual_cgroup_are_revalidated(self):
        proc, record = self.live_record()
        record.update(scope="codexfarm-batch-" + "a" * 32 + ".scope", invocation_id="generation")
        self.store.write(record)
        with patch.object(jobs, "scope_invocation", return_value="different"):
            self.assertFalse(
                self.store.validate(record["job_id"], self.store.identity(record["job_id"]))
            )
        with patch.object(jobs, "scope_invocation", return_value="generation"):
            self.assertFalse(
                self.store.validate(record["job_id"], self.store.identity(record["job_id"]))
            )
        original_identity = jobs.process_identity

        def scoped_identity(pid):
            value = original_identity(pid)
            if pid == proc.pid:
                value = dict(value, cgroup="/" + record["scope"])
            return value

        with (
            patch.object(jobs, "process_identity", side_effect=scoped_identity),
            patch.object(jobs, "scope_invocation", return_value="generation"),
        ):
            record["cgroup"] = "/" + record["scope"]
            self.store.write(record)
            self.assertTrue(
                self.store.validate(record["job_id"], self.store.identity(record["job_id"]))
            )

    def test_restart_requests_batch_only_consented_at_most_once(self):
        for role, consent in [("agent", False), ("batch", False), ("batch", True)]:
            _, record = self.live_record(role=role, restartable=consent)
            identity = self.store.identity(record["job_id"])
            if role == "batch" and consent:
                self.store.request_restart(record["job_id"], identity)
                self.assertTrue(self.store.request_path(record["job_id"]).exists())
                record["restart_count"] = 1
                self.store.write(record)
            with self.assertRaises(ValueError):
                self.store.request_restart(record["job_id"], identity)
        with self.assertRaises(ValueError):
            self.store.request_restart("../../etc/passwd", {})

    def test_worker_allowlist_expiry_recipe_isolation_and_rollback(self):
        for name in ["PATH", "LD_PRELOAD", "SECRET", "", "OMP_NUM_THREADS=4"]:
            with self.assertRaises(ValueError):
                self.store.create("batch", ["true"], str(self.root), worker_env=name, workers=2)
        for count in [0, True, 100000, 1.5]:
            with self.assertRaises(ValueError):
                self.store.create(
                    "batch", ["true"], str(self.root), worker_env="OMP_NUM_THREADS", workers=count
                )
        _, record = self.live_record()
        identity = self.store.identity(record["job_id"])
        override = self.store.reduce_workers(record["job_id"], 4, 2, identity)
        recipe = record["recipe_fingerprint"]
        self.assertEqual(self.store.recipe_overrides(recipe)["workers"], 4)
        self.assertEqual(self.store.recipe_overrides("0" * 64), {})
        with self.assertRaises(ValueError):
            self.store.reduce_workers(record["job_id"], 9, 2, identity)
        self.store.delete_override(override)
        self.assertEqual(self.store.recipe_overrides(recipe), {})
        with patch.object(jobs.time, "time", return_value=100):
            self.store.reduce_workers(record["job_id"], 3, 2, identity)
            self.store.defer_job(record["job_id"], 2, identity)
        with patch.object(jobs.time, "time", return_value=103):
            self.assertEqual(self.store.recipe_overrides(recipe), {})
        self.assertEqual(list(self.store.overrides_path.glob("*.json")), [])

    def test_defer_future_recipe_only_and_ignore_bypasses(self):
        proc, record = self.live_record()
        self.store.defer_job(record["job_id"], 10, self.store.identity(record["job_id"]))
        self.assertIsNone(proc.poll())
        self.assertFalse(
            jobs.admit(
                "batch", "queue", ResourceSettings(), 0, self.store, record["recipe_fingerprint"]
            )
        )
        with patch.object(jobs, "read_memory", side_effect=AssertionError("ignore admission")):
            self.assertTrue(
                jobs.admit(
                    "batch",
                    "ignore",
                    ResourceSettings(),
                    0,
                    self.store,
                    record["recipe_fingerprint"],
                )
            )

    def test_restart_terminates_only_owned_batch_group_and_recovery_times_out(self):
        proc, record = self.live_record()
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True
        )

        def finish_unrelated():
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait()

        self.addCleanup(finish_unrelated)
        self.store.request_restart(record["job_id"], self.store.identity(record["job_id"]))
        with patch.object(jobs, "read_memory", return_value=Memory(4096, 500, 0)):
            self.assertFalse(
                jobs.handle_restart(
                    self.store, record["job_id"], proc, ResourceSettings(queue_timeout=0), "queue"
                )
            )
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(unrelated.poll())
        result = self.store.read(record["job_id"])
        self.assertEqual(result["restart_count"], 1)
        self.assertEqual(result["status"], "queued_timeout")

    def test_restart_recovery_requires_new_continuous_period(self):
        gate = jobs.HeadroomGate(ResourceSettings(recovery_seconds=3).headroom())
        with patch.object(jobs, "read_memory", return_value=Memory(4096, 2000, 0)):
            clock = iter([0, 0, 1, 2, 3])
            with (
                patch.object(jobs.time, "monotonic", side_effect=lambda: next(clock)),
                patch.object(jobs.time, "sleep"),
            ):
                self.assertTrue(
                    jobs.admit(
                        "batch",
                        "queue",
                        ResourceSettings(recovery_seconds=3),
                        10,
                        self.store,
                        "none",
                        recovery=True,
                        gate=gate,
                    )
                )

    def test_default_queue_optin_invalid_settings_fail_before_launch(self):
        write_settings(ResourceSettings(queue_background=True))
        self.assertEqual(
            jobs.memory_policy(None, "batch", ResourceSettings(queue_background=True)), "queue"
        )
        self.assertEqual(jobs.memory_policy(None, "batch", ResourceSettings()), "warn")
        path = write_settings(ResourceSettings())
        path.write_text('{"protect_agents": "yes"}')
        result = self.run_cli(["--scope", "off"], ["echo", "payload"])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_retention_bounded_and_stale_remedies_rejected(self):
        record = self.store.create("batch", ["true"], str(self.root))
        self.store.write(
            dict(
                record,
                status="finished",
                updated_at=1,
                supervisor_start_ticks=record["supervisor_start_ticks"] + 1,
            )
        )
        self.store.cleanup(now=1000000)
        self.assertFalse(self.store.record_path(record["job_id"]).exists())
        with self.assertRaises(ValueError):
            self.store.defer_job(record["job_id"], 10, {})

    def test_restart_owns_launcher_group_even_when_payload_pid_differs(self):
        pidfile = self.root / "childpid"
        code = (
            "import subprocess,time; p=subprocess.Popen(['sleep','20']); open("
            + repr(str(pidfile))
            + ", 'w').write(str(p.pid)); time.sleep(20)"
        )
        launcher = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)

        def finish():
            try:
                os.killpg(launcher.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            launcher.wait()

        self.addCleanup(finish)
        deadline = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        payload = int(pidfile.read_text())
        record = self.store.create("batch", ["true"], str(self.root), restartable=True)
        self.store.register_payload(record["job_id"], payload)
        self.store.update(
            record["job_id"],
            launch_pid=launcher.pid,
            launch_start_ticks=jobs.process_identity(launcher.pid)["start_ticks"],
        )
        self.store.request_restart(record["job_id"], self.store.identity(record["job_id"]))
        with patch.object(jobs, "read_memory", return_value=Memory(4096, 500, 0)):
            self.assertFalse(
                jobs.handle_restart(
                    self.store,
                    record["job_id"],
                    launcher,
                    ResourceSettings(queue_timeout=0),
                    "queue",
                )
            )
        self.assertIsNotNone(launcher.poll())

    def test_restart_grace_kills_term_ignoring_owned_descendants(self):
        pidfile = self.root / "descendant"
        child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(20)"
        parent_code = (
            "import subprocess,time; p=subprocess.Popen(["
            + repr(sys.executable)
            + ",'-c',"
            + repr(child)
            + "]); open("
            + repr(str(pidfile))
            + ",'w').write(str(p.pid)); time.sleep(20)"
        )
        proc = subprocess.Popen([sys.executable, "-c", parent_code], start_new_session=True)

        def finish():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

        self.addCleanup(finish)
        deadline = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        descendant = int(pidfile.read_text())
        time.sleep(0.1)  # child has installed its intentional TERM handler
        record = self.store.create("batch", ["true"], str(self.root), restartable=True)
        self.store.register_payload(record["job_id"], proc.pid)
        jobs.terminate_owned_batch(
            self.store, record["job_id"], self.store.identity(record["job_id"]), grace=0.1
        )
        proc.wait(timeout=2)
        deadline = time.monotonic() + 2
        while jobs.process_identity(descendant) is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(
            jobs.process_identity(descendant), "owned descendant survived bounded restart grace"
        )

    def test_override_retention_bound_rejects_growth(self):
        _, record = self.live_record()
        identity = self.store.identity(record["job_id"])
        with patch.object(jobs, "MAX_JOBS", 2):
            self.store.defer_job(record["job_id"], 30, identity)
            self.store.reduce_workers(record["job_id"], 4, 30, identity)
            with self.assertRaises(ValueError):
                self.store.defer_job(record["job_id"], 30, identity)

    def test_restart_refuses_supervisor_group_without_signaling(self):
        record = self.store.create("batch", ["true"], str(self.root), restartable=True)
        self.store.register_payload(record["job_id"], os.getpid())
        with self.assertRaises(ValueError), patch.object(jobs.os, "killpg") as kill:
            jobs.terminate_owned_batch(
                self.store, record["job_id"], self.store.identity(record["job_id"])
            )
        kill.assert_not_called()

    def test_real_supervisor_applies_reduction_only_on_consented_restart(self):
        output = self.root / "counts"
        code = (
            "import os,time; from pathlib import Path; p=Path(" + repr(str(output)) + "); "
            "previous=p.read_text() if p.exists() else ''; p.write_text(previous+os.environ['CARGO_BUILD_JOBS']+'\\n'); "
            "time.sleep(20 if not previous else 0)"
        )
        wrapper = subprocess.Popen(
            [
                sys.executable,
                str(CLI),
                "run",
                "--role",
                "batch",
                "--scope",
                "off",
                "--memory-policy",
                "ignore",
                "--restartable",
                "--worker-env",
                "CARGO_BUILD_JOBS",
                "--workers",
                "8",
                "--",
                sys.executable,
                "-c",
                code,
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def finish():
            if wrapper.poll() is None:
                wrapper.terminate()
            try:
                wrapper.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                wrapper.kill()
                wrapper.communicate()

        self.addCleanup(finish)
        deadline = time.monotonic() + 4
        while not output.exists() and wrapper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(output.exists(), "batch payload did not start")
        record_path = next(p for p in self.store.path.glob("*.json") if len(p.stem) == 32)
        record = self.store.read(record_path.stem)
        identity = self.store.identity(record["job_id"])
        self.store.reduce_workers(record["job_id"], 4, expectedidentity=identity)
        self.assertEqual(output.read_text().splitlines(), ["8"])
        self.store.request_restart(record["job_id"], identity)
        stdout, stderr = wrapper.communicate(timeout=6)
        self.assertEqual(wrapper.returncode, 0, stdout + stderr)
        self.assertEqual(output.read_text().splitlines(), ["8", "4"])
        final = self.store.read(record["job_id"])
        self.assertEqual(final["restart_count"], 1)
        self.assertEqual(final["status"], "finished")

    def test_main_queue_timeout_returns_124_without_payload_or_manager(self):
        with (
            patch.object(jobs, "read_memory", return_value=Memory(4096, 500, 0)),
            patch.object(jobs, "_scope_ready", side_effect=AssertionError("manager probed")),
            patch.object(jobs, "_launch", side_effect=AssertionError("payload launched")),
        ):
            self.assertEqual(
                jobs.main(
                    ["run", "--memory-policy", "queue", "--queue-timeout", "0", "--", "true"]
                ),
                124,
            )

    def test_scoped_literal_argv_and_optin_limits_properties(self):
        log = self.doubles()
        literal = "$value; `data`"
        result = self.run_cli(
            [
                "--role",
                "batch",
                "--memory-policy",
                "ignore",
                "--memory-high",
                "50",
                "--memory-max",
                "75",
            ],
            [sys.executable, "-c", "import sys;print(sys.argv[1])", literal],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, literal + "\n")
        args = json.loads(log.read_text().splitlines()[0])
        self.assertIn("MemoryHigh=52428800", args)
        self.assertIn("MemoryMax=78643200", args)

    def test_user_slice_preserves_stronger_parent_protection_and_no_unit_writes(self):
        calls = []

        def manager(args, env=None):
            calls.append(args)
            return subprocess.CompletedProcess(
                args, 0, "infinity" if "--property=MemoryLow" in args else "", ""
            )

        with patch.object(jobs, "manager_call", side_effect=manager):
            self.assertTrue(jobs._scope_ready("agent", {}))
        self.assertFalse(
            any(prop.startswith("MemoryLow=") for command in calls for prop in command)
        )
        self.assertFalse(any("unmask" in command or "enable" in command for command in calls))

    def test_missing_scope_launcher_auto_falls_back_before_payload(self):
        self.doubles()
        (self.root / "bin/systemd-run").unlink()
        manager = self.root / "bin/systemctl"
        manager.write_text("#!" + sys.executable + "\nprint('0')\n")
        self.env["PATH"] = str(self.root / "bin")
        result = self.run_cli(
            ["--role", "batch", "--memory-policy", "ignore"],
            [sys.executable, "-c", "print('payload')"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "payload\n")
        self.assertEqual(result.stderr.count("falling back"), 1)

    def test_failed_parent_property_query_does_not_lower_unknown_protection(self):
        calls = []

        def manager(args, env=None):
            calls.append(args)
            code = 1 if "--property=MemoryLow" in args else 0
            return subprocess.CompletedProcess(args, code, "", "")

        with patch.object(jobs, "manager_call", side_effect=manager):
            self.assertTrue(jobs._scope_ready("agent", {}))
        self.assertFalse(
            any(prop.startswith("MemoryLow=") for command in calls for prop in command)
        )

    def test_batch_nice_is_ten_when_already_nice_ten(self):
        result = self.run_cli(
            ["--scope", "off", "--memory-policy", "ignore"],
            [sys.executable, "-c", "import os;print(os.getpriority(os.PRIO_PROCESS,0))"],
            preexec_fn=lambda: os.nice(10),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "10")

    def test_cleanup_marks_dead_queued_supervisor_stale(self):
        record = self.store.create("batch", ["true"], str(self.root))
        record["supervisor_start_ticks"] += 1
        self.store.write(record)
        self.store.cleanup()
        self.assertEqual(self.store.read(record["job_id"])["status"], "stale")

    def test_public_listing_never_exposes_recipes(self):
        record = self.store.create("batch", ["a-secret-argument"], "/private-cwd")
        self.assertEqual(self.store.list_jobs()[0]["job_id"], record["job_id"])
        result = json.dumps(self.store.list_jobs())
        self.assertNotIn("a-secret-argument", result)
        self.assertNotIn("private-cwd", result)
        self.assertEqual(self.store.list_jobs(limit=0), [])

    def test_run_help_describes_no_caps_and_optional_permissions(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "run", "--help"],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No caps by default", result.stdout)
        self.assertIn("user manager", result.stdout)
        self.assertIn("privileged helper", result.stdout)

    def test_agent_provider_can_handle_terminal_interrupt_without_supervisor_exit(self):
        ready = self.root / "ready"
        code = (
            "import signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGINT,lambda *args: None); "
            "Path(" + repr(str(ready)) + ").write_text('ready'); time.sleep(20)"
        )
        wrapper = subprocess.Popen(
            [
                sys.executable,
                str(CLI),
                "run",
                "--role",
                "agent",
                "--scope",
                "off",
                "--",
                sys.executable,
                "-c",
                code,
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def finish():
            try:
                os.killpg(wrapper.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            wrapper.communicate()

        self.addCleanup(finish)
        deadline = time.monotonic() + 4
        while not ready.exists() and wrapper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists())
        os.killpg(wrapper.pid, signal.SIGINT)
        time.sleep(0.2)
        self.assertIsNone(wrapper.poll(), "agent supervisor exited while provider handled Ctrl-C")

    def test_agent_terminal_interrupt_retains_child_default_disposition(self):
        wrapper = subprocess.Popen(
            [
                sys.executable,
                str(CLI),
                "run",
                "--role",
                "agent",
                "--scope",
                "off",
                "--",
                "sleep",
                "20",
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def finish():
            try:
                os.killpg(wrapper.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            wrapper.communicate()

        self.addCleanup(finish)
        deadline = time.monotonic() + 4
        while (
            not list(self.store.path.glob("*.started.json"))
            and wrapper.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        self.assertTrue(list(self.store.path.glob("*.started.json")))
        time.sleep(0.05)
        os.killpg(wrapper.pid, signal.SIGINT)
        wrapper.communicate(timeout=3)
        self.assertEqual(
            wrapper.returncode, 130, "caught supervisor handler leaked as ignored child SIGINT"
        )

    def test_post_spawn_record_failure_never_reexecutes_payload(self):
        self.doubles()
        count = self.root / "executions"
        script = (
            "from codex_looper import resource_jobs as j; "
            "original=j.JobStore.update; "
            "exec(\"def update(self,job_id,**changes):\\n if 'launch_pid' in changes: raise OSError('record unavailable')\\n return original(self,job_id,**changes)\\n\"); "
            "j.JobStore.update=update; "
            "raise SystemExit(j.main("
            + repr(
                [
                    "run",
                    "--role",
                    "agent",
                    "--memory-policy",
                    "ignore",
                    "--",
                    sys.executable,
                    "-c",
                    "open(" + repr(str(count)) + ",'a').write('x')",
                ]
            )
            + "))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=8,
        )
        self.assertEqual(count.read_text(), "x", result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("falling back", result.stderr)

    def test_unavailable_job_store_preserves_optional_agent_access_but_required_fails(self):
        resources = self.root / "state/codexfarm/resources"
        resources.mkdir(parents=True, mode=0o700)
        resources.chmod(0o755)
        for options, expected in [
            (["--role", "agent"], 0),
            (["--role", "agent", "--scope", "required"], 2),
            (["--role", "batch", "--memory-max", "32"], 2),
        ]:
            result = self.run_cli(options, [sys.executable, "-c", "print('payload')"])
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(result.stdout, "payload\n" if expected == 0 else "")
            if expected == 0:
                self.assertIn("private job registration unavailable", result.stderr)

    def test_unrecorded_managed_launcher_disables_remedies(self):
        record = self.store.create(
            "batch", ["true"], str(self.root), restartable=True, managed=True
        )
        process = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(20)"], start_new_session=True
        )

        def finish():
            if process.poll() is None:
                process.kill()
            process.wait()

        self.addCleanup(finish)
        self.store.register_payload(record["job_id"], process.pid)
        identity = self.store.identity(record["job_id"])
        self.assertFalse(self.store.validate(record["job_id"], identity))
        with self.assertRaises(ValueError):
            self.store.request_restart(record["job_id"], identity)

    def test_restart_grace_cleans_late_forked_term_handler_descendant(self):
        pidfile = self.root / "late-descendant"
        code = (
            "import os,signal,subprocess,time\n"
            "def cleanup(*args):\n"
            " p=subprocess.Popen(['sleep','20'])\n"
            " open(" + repr(str(pidfile)) + ",'w').write(str(p.pid))\n"
            " raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM,cleanup)\n"
            "print('ready',flush=True)\ntime.sleep(20)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True
        )

        def finish():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()

        self.addCleanup(finish)
        self.assertEqual(process.stdout.readline(), "ready\n")
        record = self.store.create("batch", ["true"], str(self.root), restartable=True)
        self.store.register_payload(record["job_id"], process.pid)
        jobs.terminate_owned_batch(
            self.store, record["job_id"], self.store.identity(record["job_id"]), grace=0.1
        )
        process.wait(timeout=2)
        self.assertTrue(pidfile.exists())
        descendant = int(pidfile.read_text())
        self.assertIsNone(
            jobs.process_identity(descendant), "late TERM cleanup descendant survived termination"
        )

    def test_memory_low_reaches_both_interactive_ancestors(self):
        calls = []

        def manager(args, env=None):
            calls.append(args)
            return subprocess.CompletedProcess(
                args, 0, "0" if "--property=MemoryLow" in args else "", ""
            )

        with patch.object(jobs, "manager_call", side_effect=manager):
            self.assertTrue(jobs._scope_ready("agent", {}))
        for unit in ("codexfarm.slice", "codexfarm-interactive.slice"):
            self.assertIn(["set-property", "--runtime", unit, "MemoryLow=1024M"], calls)

    def test_slice_siblings_apply_cross_role_cpu_io_preferences(self):
        for role in ("agent", "batch"):
            calls = []

            def manager(args, env=None, recorded_calls=calls):
                recorded_calls.append(args)
                return subprocess.CompletedProcess(
                    args, 0, "infinity" if "--property=MemoryLow" in args else "", ""
                )

            with patch.object(jobs, "manager_call", side_effect=manager):
                self.assertTrue(jobs._scope_ready(role, {}))
            self.assertIn(
                [
                    "set-property",
                    "--runtime",
                    "codexfarm-interactive.slice",
                    "CPUWeight=200",
                    "IOWeight=200",
                ],
                calls,
            )
            self.assertIn(
                [
                    "set-property",
                    "--runtime",
                    "codexfarm-batch.slice",
                    "CPUWeight=25",
                    "IOWeight=25",
                ],
                calls,
            )
            self.assertFalse(
                any(
                    prop.startswith(("CPUQuota=", "MemoryMax=", "MemoryHigh=", "TasksMax="))
                    for command in calls
                    for prop in command
                )
            )

    def test_slice_memory_low_preserves_each_stronger_or_unknown_ancestor(self):
        for parent, child in [
            ("infinity", "2147483648"),
            ("2147483648", "infinity"),
            ("unknown", "0"),
        ]:
            calls = []

            def manager(args, env=None, recorded_calls=calls, parent_low=parent, child_low=child):
                recorded_calls.append(args)
                if "--property=MemoryLow" in args:
                    return subprocess.CompletedProcess(
                        args, 0, parent_low if args[1] == "codexfarm.slice" else child_low, ""
                    )
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.object(jobs, "manager_call", side_effect=manager):
                self.assertTrue(jobs._scope_ready("agent", {}))
            low_changes = [
                command
                for command in calls
                if any(prop.startswith("MemoryLow=") for prop in command)
            ]
            expected = (
                [["set-property", "--runtime", "codexfarm-interactive.slice", "MemoryLow=1024M"]]
                if child == "0"
                else []
            )
            self.assertEqual(low_changes, expected)

    def test_manager_query_outage_does_not_make_live_record_permanently_stale(self):
        _, record = self.live_record()
        record.update(scope="codexfarm-batch-" + "a" * 32 + ".scope", invocation_id="generation")
        self.store.write(record)
        original = jobs.process_identity

        def scoped(pid):
            value = original(pid)
            if value and pid == record["payload_pid"]:
                value = dict(value, cgroup="0::/" + record["scope"])
            return value

        record["cgroup"] = "0::/" + record["scope"]
        self.store.write(record)
        with (
            patch.object(jobs, "process_identity", side_effect=scoped),
            patch.object(jobs, "scope_invocation", return_value=None),
        ):
            self.store.cleanup(now=100)
            self.assertEqual(self.store.read(record["job_id"])["status"], "running")
            self.store.cleanup(now=100 + 8 * 86400)
            self.assertTrue(self.store.record_path(record["job_id"]).exists())
        with (
            patch.object(jobs, "process_identity", side_effect=scoped),
            patch.object(jobs, "scope_invocation", return_value="generation"),
        ):
            self.assertTrue(
                self.store.validate(record["job_id"], self.store.identity(record["job_id"]))
            )

    def test_previously_stale_live_payload_is_never_pruned(self):
        _, record = self.live_record()
        record.update(
            status="stale",
            updated_at=1,
            supervisor_start_ticks=record["supervisor_start_ticks"] + 1,
        )
        self.store.write(record)
        self.store.cleanup(now=8 * 86400)
        self.assertTrue(
            self.store.record_path(record["job_id"]).exists(), "live payload record was pruned"
        )

    def test_terminal_record_with_live_launcher_is_never_pruned(self):
        process, record = self.live_record()
        record.update(
            status="finished",
            updated_at=1,
            payload_start_ticks=record["payload_start_ticks"] + 1,
            supervisor_start_ticks=record["supervisor_start_ticks"] + 1,
            launch_pid=process.pid,
            launch_start_ticks=jobs.process_identity(process.pid)["start_ticks"],
        )
        self.store.write(record)
        self.store.cleanup(now=8 * 86400)
        self.assertTrue(self.store.record_path(record["job_id"]).exists())

    def test_unreadable_process_identity_is_retained_conservatively(self):
        _, record = self.live_record()
        record.update(status="stale", updated_at=1)
        self.store.write(record)
        with patch.object(jobs, "process_identity", return_value=None):
            self.store.cleanup(now=8 * 86400)
        self.assertTrue(self.store.record_path(record["job_id"]).exists())

    def test_termination_refuses_known_member_with_unreadable_existing_identity(self):
        process, record = self.live_record()
        member = jobs.process_identity(process.pid)
        terminated = False
        original_identity = jobs.process_identity

        def identity(pid):
            if terminated and pid == process.pid:
                return None
            return original_identity(pid)

        def term(_group, _signum):
            nonlocal terminated
            terminated = True

        with (
            patch.object(
                jobs, "_group_members", side_effect=lambda group: [] if terminated else [member]
            ),
            patch.object(jobs, "process_identity", side_effect=identity),
            patch.object(jobs.os, "killpg", side_effect=term),
            patch.object(jobs.signal, "pidfd_send_signal") as send,
        ):
            with self.assertRaises(ValueError):
                jobs.terminate_owned_batch(
                    self.store, record["job_id"], self.store.identity(record["job_id"]), grace=0.01
                )
        send.assert_not_called()
        self.assertIsNone(process.poll())

    def test_current_detectable_group_member_with_unreadable_identity_is_not_ignored(self):
        process, _record = self.live_record()
        original = jobs.process_identity
        with patch.object(
            jobs,
            "process_identity",
            side_effect=lambda pid: None if pid == process.pid else original(pid),
        ):
            with self.assertRaises(ValueError):
                jobs._group_members(process.pid)

    def test_concurrent_expired_override_reads_are_idempotent(self):
        import concurrent.futures
        import threading

        _, record = self.live_record()
        key = self.store.defer_job(record["job_id"], 1, self.store.identity(record["job_id"]))
        path = self.store.overrides_path / (key + ".json")
        value = jobs.read_private_json(path)
        value["expires_at"] = 1
        jobs.write_private_json(path, value)
        first_read, second_read, release = threading.Event(), threading.Event(), threading.Event()
        guard = threading.Lock()
        count = 0
        original = jobs.read_private_json

        def read(target):
            nonlocal count
            value = original(target)
            if target == path:
                with guard:
                    count += 1
                    ordinal = count
                if ordinal == 1:
                    first_read.set()
                    release.wait(2)
                else:
                    second_read.set()
            return value

        with (
            patch.object(jobs, "read_private_json", side_effect=read),
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
        ):
            one = pool.submit(self.store.recipe_overrides, record["recipe_fingerprint"])
            self.assertTrue(first_read.wait(2))
            two = pool.submit(self.store.recipe_overrides, record["recipe_fingerprint"])
            second_read.wait(0.1)
            release.set()
            self.assertEqual(one.result(timeout=3), {})
            self.assertEqual(two.result(timeout=3), {})
        self.assertFalse(path.exists())

    def test_override_disappearing_between_listing_and_read_is_benign(self):
        _, record = self.live_record()
        key = self.store.defer_job(record["job_id"], 10, self.store.identity(record["job_id"]))
        path = self.store.overrides_path / (key + ".json")
        original = jobs.read_private_json

        def remove_and_read(target):
            if target == path:
                path.unlink(missing_ok=True)
            return original(target)

        with patch.object(jobs, "read_private_json", side_effect=remove_and_read):
            self.assertEqual(self.store.recipe_overrides(record["recipe_fingerprint"]), {})
        path.symlink_to(self.root / "missing-target")
        with self.assertRaises(ValueError):
            self.store.recipe_overrides(record["recipe_fingerprint"])

    def test_reduce_workers_locked_operation_finishes_within_bounded_timeout(self):
        _, record = self.live_record()
        script = (
            "from codex_looper.resource_jobs import JobStore; s=JobStore(); job="
            + repr(record["job_id"])
            + '; s.reduce_workers(job,4,expectedidentity=s.identity(job)); print(s.recipe_overrides(s.read(job)["recipe_fingerprint"])["workers"])'
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=4,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "4")

    def test_main_recovery_error_never_falls_back_or_executes_second_payload(self):
        import threading

        for exception in (OSError("memory sample unavailable"), ValueError("override unavailable")):
            with self.subTest(exception=type(exception).__name__):
                output = self.root / ("execution-" + type(exception).__name__)
                code = (
                    "import time; from pathlib import Path; p=Path(" + repr(str(output)) + "); "
                    "old=p.read_text() if p.exists() else ''; p.write_text(old+'x'); time.sleep(20 if not old else 0)"
                )
                calls, errors, processes = [], [], []
                launch = jobs._launch

                def private_launch(
                    store,
                    record,
                    args,
                    env,
                    scoped,
                    attempt_calls=calls,
                    original_launch=launch,
                    owned_processes=processes,
                ):
                    attempt_calls.append(scoped)
                    process = original_launch(store, record, args, env, False)
                    owned_processes.append(process)
                    return process

                admission = jobs.admit

                def failing_recovery(
                    *args, failure=exception, original_admission=admission, **kwargs
                ):
                    if kwargs.get("recovery"):
                        raise failure
                    return original_admission(*args, **kwargs)

                def request(output_path=output, payload_code=code, request_errors=errors):
                    try:
                        deadline = time.monotonic() + 5
                        while not output_path.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        for path in self.store._records():
                            record = self.store.read(path.stem)
                            if record["status"] == "running" and record["argv"][-1] == payload_code:
                                self.store.request_restart(
                                    record["job_id"], self.store.identity(record["job_id"])
                                )
                                return
                        raise AssertionError("running private payload not found")
                    except BaseException as error:
                        request_errors.append(error)

                requester = threading.Thread(target=request)
                requester.start()
                try:
                    with (
                        patch.object(jobs, "_scope_ready", return_value=True),
                        patch.object(jobs, "_launch", side_effect=private_launch),
                        patch.object(jobs, "admit", side_effect=failing_recovery),
                    ):
                        result = jobs.main(
                            [
                                "run",
                                "--role",
                                "batch",
                                "--restartable",
                                "--memory-policy",
                                "ignore",
                                "--",
                                sys.executable,
                                "-c",
                                code,
                            ]
                        )
                finally:
                    requester.join(timeout=6)
                    for process in processes:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                self.assertFalse(requester.is_alive())
                self.assertEqual(errors, [])
                self.assertNotEqual(result, 0)
                self.assertEqual(calls, [True])
                self.assertEqual(output.read_text(), "x")

    def test_override_lookup_and_rollback_complete_consistently(self):
        import concurrent.futures
        import threading

        _, record = self.live_record()
        key = self.store.defer_job(record["job_id"], 10, self.store.identity(record["job_id"]))
        path = self.store.overrides_path / (key + ".json")
        enumerated, release, rolled_back = threading.Event(), threading.Event(), threading.Event()
        original = jobs.read_private_json
        first_thread = None

        def read(target):
            nonlocal first_thread
            if target == path and first_thread is None:
                first_thread = threading.get_ident()
                enumerated.set()
                release.wait(2)
            return original(target)

        def rollback():
            self.store.delete_override(key)
            rolled_back.set()

        with (
            patch.object(jobs, "read_private_json", side_effect=read),
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
        ):
            reader = pool.submit(self.store.recipe_overrides, record["recipe_fingerprint"])
            self.assertTrue(enumerated.wait(2))
            remover = pool.submit(rollback)
            rolled_back.wait(0.1)
            release.set()
            snapshot = reader.result(timeout=3)
            remover.result(timeout=3)
            self.assertIn("defer_until", snapshot)
        self.assertEqual(self.store.recipe_overrides(record["recipe_fingerprint"]), {})

    def test_confirmed_zombie_member_is_dead_without_pidfd(self):
        process, _record = self.live_record()
        member = jobs.process_identity(process.pid)
        process.terminate()
        deadline = time.monotonic() + 2
        while jobs.process_identity(process.pid) is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(jobs._captured_member_state(member, None), "dead")

    def test_live_original_member_changing_uid_and_group_refuses_restart(self):
        process, record = self.live_record()
        member = jobs.process_identity(process.pid)
        transferred = False
        original = jobs.process_identity

        def identity(pid):
            value = original(pid)
            if transferred and pid == process.pid:
                return dict(
                    value, uid=member["uid"] + 1, pgid=member["pgid"] + 1, sid=member["sid"] + 1
                )
            return value

        def term(_group, _signal):
            nonlocal transferred
            transferred = True

        with (
            patch.object(jobs, "process_identity", side_effect=identity),
            patch.object(
                jobs, "_group_members", side_effect=lambda group: [] if transferred else [member]
            ),
            patch.object(jobs.os, "killpg", side_effect=term),
            patch.object(jobs.signal, "pidfd_send_signal") as send,
        ):
            with self.assertRaises(ValueError):
                jobs.terminate_owned_batch(
                    self.store, record["job_id"], self.store.identity(record["job_id"]), grace=0.01
                )
        send.assert_not_called()
        self.assertIsNone(process.poll())

    def test_cleanup_retains_same_start_ticks_with_changed_uid(self):
        process, record = self.live_record()
        record.update(
            status="stale",
            updated_at=1,
            supervisor_start_ticks=record["supervisor_start_ticks"] + 1,
        )
        self.store.write(record)
        original = jobs.process_identity

        def changed_identity(pid):
            value = original(pid)
            return dict(value, uid=value["uid"] + 1) if pid == process.pid and value else value

        with patch.object(jobs, "process_identity", side_effect=changed_identity):
            self.store.cleanup(now=8 * 86400)
        self.assertTrue(self.store.record_path(record["job_id"]).exists())

    def test_expired_lookup_winning_before_concurrent_rollback_is_benign(self):
        import concurrent.futures
        import threading

        _, record = self.live_record()
        key = self.store.defer_job(record["job_id"], 1, self.store.identity(record["job_id"]))
        path = self.store.overrides_path / (key + ".json")
        value = jobs.read_private_json(path)
        value["expires_at"] = 1
        jobs.write_private_json(path, value)
        read_ready, release, rollback_started = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        original = jobs.read_private_json
        held = False

        def read(target):
            nonlocal held
            result = original(target)
            if target == path and not held:
                held = True
                read_ready.set()
                release.wait(2)
            return result

        def rollback():
            rollback_started.set()
            self.store.rollback_override(key)

        with (
            patch.object(jobs, "read_private_json", side_effect=read),
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
        ):
            reader = pool.submit(self.store.recipe_overrides, record["recipe_fingerprint"])
            self.assertTrue(read_ready.wait(2))
            remover = pool.submit(rollback)
            self.assertTrue(rollback_started.wait(2))
            release.set()
            self.assertEqual(reader.result(timeout=3), {})
            self.assertIsNone(remover.result(timeout=3))
        self.assertFalse(path.exists())

    def test_missing_rollback_and_disappearance_are_benign_but_existing_unsafe_rejected(self):
        key = "e" * 32
        self.assertIsNone(self.store.rollback_override(key))
        path = self.store.overrides_path / (key + ".json")
        jobs.write_private_json(path, {"kind": "defer"})
        original = jobs.read_private_json

        def read_then_remove(target):
            result = original(target)
            if target == path:
                path.unlink()
            return result

        with patch.object(jobs, "read_private_json", side_effect=read_then_remove):
            self.assertIsNone(self.store.delete_override(key))
        path.symlink_to(self.root / "missing-target")
        with self.assertRaises(ValueError):
            self.store.rollback_override(key)
        path.unlink()
        jobs.write_private_json(path, {"kind": "defer"})
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.delete_override(key)
        with self.assertRaises(ValueError):
            self.store.delete_override("../bad")
