import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_looper.health import Limits, Memory
from codex_looper.resource_config import ResourceSettings

try:
    from codex_looper import resource_incidents as incidents
    from codex_looper import resource_reports as reports
except ImportError:
    incidents = reports = None


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(reports, "bounded resource reporting missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        (self.proc / "meminfo").write_text(
            "MemTotal: 8388608 kB\nMemAvailable: 1048576 kB\nSwapTotal: 2048 kB\nSwapFree: 1024 kB\n"
        )
        self.store = Mock()
        self.store.list_jobs.return_value = []

    def process(self, pid, ticks=42, rss=1024):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        (folder / "stat").write_text(
            f"{pid} (untrusted name) S " + " ".join(["0"] * 18 + [str(ticks)])
        )
        (folder / "status").write_text(f"Name:\tlabel\x1b[31m\nVmRSS:\t{rss} kB\n")
        (folder / "cgroup").write_text("0::/batch")
        (folder / "smaps_rollup").write_text(f"Pss: {rss // 2} kB\n")
        (folder / "cmdline").write_text("SECRET_ARGV")
        (folder / "environ").write_text("SECRET_ENV")
        return folder

    def test_report_privacy_pss_growth_and_pid_reuse(self):
        folder = self.process(100)
        first, identities = reports.collect_report(proc=self.proc, store=self.store)
        self.assertEqual(identities, {})
        self.assertEqual(first["memory"]["available_mib"], 1024)
        self.assertEqual(first["consumers"][0]["pss_mib"], 0.5)
        self.assertNotIn("SECRET", json.dumps(first))
        self.assertNotIn("\x1b", first["consumers"][0]["label"])
        self.process(100, rss=2048)
        second, _ = reports.collect_report(proc=self.proc, store=self.store, previous=first)
        self.assertEqual(second["consumers"][0]["growth_mib"], 1)
        self.process(100, ticks=43, rss=4096)
        third, _ = reports.collect_report(proc=self.proc, store=self.store, previous=second)
        self.assertIsNone(third["consumers"][0]["growth_mib"])
        (folder / "cgroup").write_text("0::/other")
        fourth, _ = reports.collect_report(proc=self.proc, store=self.store, previous=third)
        self.assertIsNone(fourth["consumers"][0]["growth_mib"])

    def test_scan_count_time_and_top_bounds(self):
        for pid in range(1, 31):
            self.process(pid, rss=pid * 1024)
        with patch.object(reports, "MAX_SCAN_PIDS", 25):
            report, _ = reports.collect_report(proc=self.proc, store=self.store)
        self.assertEqual(report["scanned_pids"], 25)
        self.assertLessEqual(len(report["consumers"]), 20)
        times = iter([0, 0, 3, 3, 3, 3, 3])
        report, _ = reports.collect_report(
            proc=self.proc, store=self.store, clock=lambda: next(times, 3)
        )
        self.assertLessEqual(report["scanned_pids"], 1)

    def test_projection_bounded_and_only_safe_known_fields(self):
        report = {
            "memory": {"available_mib": 1000},
            "consumers": [dict(pid=i, label="x" * 10000, rss_mib=100) for i in range(20)],
            "jobs": [{"job_id": "a" * 32, "role": "batch", "argv": ["SECRET"], "workers": 4}],
            "private": "SECRET",
        }
        prompt = reports.model_projection(report)
        self.assertLessEqual(len(prompt.encode()), reports.MAX_PROMPT_BYTES)
        self.assertNotIn("SECRET", prompt)
        self.assertIn("a" * 32, prompt)

    def test_cgroup_counters_and_growth_use_same_generation(self):
        self.process(100)
        group = self.root / "cgroups/batch"
        group.mkdir(parents=True)
        (group / "memory.current").write_text("1048576")
        (group / "memory.stat").write_text("anon 524288\nfile 524288\n")
        (group / "memory.events").write_text("oom 2\nhigh 1\n")
        first, _ = reports.collect_report(
            proc=self.proc, store=self.store, cgroup_root=group.parent
        )
        self.assertEqual(first["consumers"][0].get("cgroup_current_mib"), 1)
        self.assertEqual(first["consumers"][0]["memory_events"]["oom"], 2)
        (group / "memory.current").write_text("2097152")
        second, _ = reports.collect_report(
            proc=self.proc, store=self.store, cgroup_root=group.parent, previous=first
        )
        self.assertEqual(second["consumers"][0]["cgroup_growth_mib"], 1)
        second["consumers"][0]["cgroup_inode"] = 0
        third, _ = reports.collect_report(
            proc=self.proc, store=self.store, cgroup_root=group.parent, previous=second
        )
        self.assertIsNone(third["consumers"][0]["cgroup_growth_mib"])


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(incidents, "resource incident coordinator missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Mock()
        self.job = "a" * 32
        self.identity = {"job_id": self.job, "payload_pid": 123, "payload_start_ticks": 42}
        self.jobs = [
            {
                "job_id": self.job,
                "role": "batch",
                "workers": 4,
                "worker_env": "OMP_NUM_THREADS",
                "restartable": True,
                "restart_count": 0,
            }
        ]
        self.trusted = {self.job: self.identity}
        self.store.validate.return_value = True
        self.store.defer_job.return_value = "b" * 32
        self.store.reduce_workers.return_value = "c" * 32
        self.store.read.return_value = dict(self.jobs[0])

    def answer(self, actions):
        return json.dumps(
            dict(
                diagnosis="fixture",
                evidence=["RAM"],
                proposed_fixes=["manual review"],
                actions=actions,
            )
        )

    def test_output_rejects_whole_invalid_document_before_mutation(self):
        good = {"kind": "defer", "job_id": self.job, "ttl_seconds": 300}
        bad = [
            dict(good, command="rm"),
            dict(good, kind="kill"),
            dict(good, ttl_seconds=True),
            dict(good, ttl_seconds=float("nan")),
            dict(good, job_id="unknown"),
            dict(good, ttl_seconds=3601),
        ]
        for action in bad:
            with self.subTest(action=action), self.assertRaises(ValueError):
                incidents.validate_answer(self.answer([good, action]), self.jobs, self.trusted)
        for raw in [
            '{"diagnosis":"x","diagnosis":"y"}',
            self.answer([good, good]),
            self.answer([]) + " " * 32769,
        ]:
            with self.assertRaises(ValueError):
                incidents.validate_answer(raw, self.jobs, self.trusted)
        self.store.defer_job.assert_not_called()

    def test_agents_and_undeclared_workers_restart_rejected(self):
        for changes, action in [
            ({"role": "agent"}, {"kind": "defer"}),
            ({"worker_env": None}, {"kind": "reduce_workers", "workers": 2}),
            ({"restartable": False}, {"kind": "restart"}),
            ({"restart_count": 1}, {"kind": "restart"}),
            ({}, {"kind": "reduce_workers", "workers": True}),
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                incidents.validate_answer(
                    self.answer([dict(action, job_id=self.job)]),
                    [dict(self.jobs[0], **changes)],
                    self.trusted,
                )

    def test_consent_revalidation_and_only_own_rollback(self):
        answer = incidents.validate_answer(
            self.answer([{"kind": "defer", "job_id": self.job}]), self.jobs, self.trusted
        )
        before = {"available_mib": 1024, "pressure_percent": 0, "io_pressure_percent": 0}
        after = dict(before, available_mib=500)
        result = incidents.apply_actions(
            answer,
            self.trusted,
            self.store,
            consent=False,
            metrics=lambda: before,
            journal=self.root / "journal.json",
        )
        self.assertEqual(result, [])
        self.store.defer_job.assert_not_called()
        result = incidents.apply_actions(
            answer,
            self.trusted,
            self.store,
            consent=True,
            metrics=Mock(side_effect=[before, after]),
            journal=self.root / "journal.json",
        )
        self.store.defer_job.assert_called_once_with(self.job, 300, self.identity)
        self.store.rollback_override.assert_called_once_with("b" * 32)
        self.assertEqual(result[0]["rollback"], "removed")
        self.assertEqual((self.root / "journal.json").stat().st_mode & 0o777, 0o600)
        self.store.validate.return_value = False
        self.store.defer_job.reset_mock()
        incidents.apply_actions(
            answer,
            self.trusted,
            self.store,
            consent=True,
            metrics=lambda: before,
            journal=self.root / "journal2.json",
        )
        self.store.defer_job.assert_not_called()

    def test_scheduler_sustained_cooldown_low_memory_and_rollback(self):
        launcher = Mock()
        scheduler = incidents.IncidentScheduler(self.root, launch=launcher)
        settings = ResourceSettings(investigator="codex")
        low = Memory(8192, 1000, 0)
        for now in [0, 15, 30, 59]:
            scheduler.sample(low, Limits(), settings, now=now)
        launcher.assert_not_called()
        scheduler.sample(low, Limits(), settings, now=60)
        self.assertEqual(launcher.call_count, 1)
        for now in range(75, 960, 15):
            scheduler.sample(low, Limits(), settings, now=now)
        scheduler.sample(low, Limits(), settings, now=959)
        self.assertEqual(launcher.call_count, 1)
        scheduler.sample(low, Limits(), settings, now=960)
        self.assertEqual(launcher.call_count, 2)
        scheduler.sample(low, Limits(), settings, now=10)
        scheduler.sample(Memory(8192, 400, 0), Limits(), settings, now=2000)
        self.assertEqual(launcher.call_count, 2)

    def test_io_trigger_unknown_reset_off_and_shared_nonblocking_lock(self):
        launcher = Mock()
        scheduler = incidents.IncidentScheduler(self.root, launch=launcher)
        settings = ResourceSettings(investigator="codex")
        io = Memory(8192, 4096, 2048, 0, 10)
        scheduler.sample(io, Limits(), settings, now=0)
        scheduler.sample(None, Limits(), settings, now=60)
        scheduler.sample(io, Limits(), settings, now=61)
        scheduler.sample(io, Limits(), ResourceSettings(), now=121)
        launcher.assert_not_called()
        scheduler.sample(io, Limits(), settings, now=122)
        with incidents.incident_lock(self.root) as lock:
            self.assertIsNotNone(lock)
            with incidents.incident_lock(self.root) as second:
                self.assertIsNone(second)
            scheduler.sample(io, Limits(), settings, now=182)
        launcher.assert_not_called()
        scheduler.sample(io, Limits(), settings, now=183)
        launcher.assert_called_once()

    def test_retention_is_private_bounded_and_refuses_unsafe(self):
        directory = self.root / "reports"
        from codex_looper.resource_config import write_private_json

        for index in range(25):
            write_private_json(directory / f"{index:03}.json", {"sample": index})
        incidents.retain(directory)
        self.assertEqual(len(list(directory.glob("*.json"))), 20)
        self.assertEqual(min(p.name for p in directory.iterdir()), "005.json")
        (directory / "000.json").symlink_to(self.root / "missing")
        with self.assertRaises(ValueError):
            incidents.retain(directory)

    def test_report_prioritizes_running_jobs_and_maps_payload(self):
        self.store.list_jobs.return_value = [
            dict(self.jobs[0], status="finished", payload_pid=1),
            dict(self.jobs[0], job_id="b" * 32, status="running", payload_pid=222),
        ]
        self.store.identity.side_effect = lambda key: {"job_id": key}
        report, _ = reports.collect_report(proc=self.root, store=self.store)
        self.assertEqual(report["jobs"][0]["job_id"], "b" * 32)
        projected = json.loads(reports.model_projection(report))
        self.assertEqual(projected["jobs"][0]["payload_pid"], 222)
        self.assertEqual(projected["jobs"][0]["status"], "running")

    def test_large_unicode_answer_rejected_before_actions(self):
        from codex_looper.resource_config import write_private_json

        answer = json.loads(self.answer([]))
        answer["evidence"] = ["😀" * 500] * 8
        answer["proposed_fixes"] = ["😀" * 500] * 8
        with self.assertRaises(ValueError):
            incidents.reserve_report({"report": {}, "answer": answer})
        self.assertFalse((self.root / "result.json").exists())
        write_private_json(
            self.root / "result.json",
            incidents.reserve_report({"report": {}, "answer": json.loads(self.answer([]))}),
        )

    def test_execution_rejects_unknown_kind_even_when_called_directly(self):
        action = {"kind": "shell", "job_id": self.job}
        incidents.apply_actions(
            {"actions": [action]},
            self.trusted,
            self.store,
            consent=True,
            journal=self.root / "journal.json",
            metrics=lambda: {},
        )
        self.store.request_restart.assert_not_called()

    def test_diagnosis_only_and_revoked_investigator_never_apply(self):
        from codex_looper import resource_investigator

        report = {"memory": {}, "jobs": self.jobs, "consumers": []}
        enabled = ResourceSettings(investigator="codex", automatic_actions=True)
        for scheduled, actions, current in [
            (False, False, enabled),
            (True, False, ResourceSettings(automatic_actions=True)),
            (False, True, ResourceSettings(investigator="codex")),
        ]:
            with (
                self.subTest(scheduled=scheduled, actions=actions),
                patch.object(incidents, "load_settings", side_effect=[enabled, current]),
                patch.object(incidents, "read_memory", return_value=Memory(8192, 2048, 0)),
                patch.object(incidents, "collect_report", return_value=(report, self.trusted)),
                patch.object(incidents, "JobStore", return_value=self.store),
                patch.object(
                    resource_investigator,
                    "investigate",
                    return_value=self.answer([{"kind": "defer", "job_id": self.job}]),
                ),
            ):
                result = incidents._investigate(self.root, scheduled=scheduled, actions=actions)
            self.assertEqual(result["status"], "diagnosed")
            self.store.defer_job.assert_not_called()

    def test_low_memory_never_invokes_adapter(self):
        from codex_looper import resource_investigator

        with (
            patch.object(
                incidents, "load_settings", return_value=ResourceSettings(investigator="codex")
            ),
            patch.object(incidents, "read_memory", return_value=Memory(8192, 511, 0)),
            patch.object(resource_investigator, "investigate") as adapter,
        ):
            result = incidents._investigate(self.root, scheduled=True, actions=False)
        self.assertIn("deferred", result["status"])
        adapter.assert_not_called()

    def test_compact_projection_retains_cgroup_events_and_statistics(self):
        projection = json.loads(
            reports.model_projection(
                {
                    "memory": {},
                    "jobs": [],
                    "consumers": [
                        {
                            "pid": 1,
                            "cgroup": "0::/fixture",
                            "memory_stat": {"anon": 1024, "file": 512},
                            "memory_events": {"high": 2, "oom": 1},
                        }
                    ],
                }
            )
        )
        self.assertEqual(projection["consumers"][0]["memory_events"]["oom"], 1)
        self.assertEqual(projection["consumers"][0]["memory_stat"]["anon"], 1024)

    def test_reversible_actions_precede_restart_and_failure_blocks_restart(self):
        actions = [
            {"kind": "restart", "job_id": self.job},
            {"kind": "reduce_workers", "job_id": self.job, "workers": 2, "ttl_seconds": 300},
        ]
        sequence = []
        valid = [True]
        self.store.validate.side_effect = lambda *a, **k: valid[0]
        self.store.reduce_workers.side_effect = (
            lambda *a, **k: sequence.append("reduce") or "c" * 32
        )

        def restart(*_args):
            sequence.append("restart")
            valid[0] = False

        self.store.request_restart.side_effect = restart
        incidents.apply_actions(
            {"actions": actions},
            self.trusted,
            self.store,
            consent=True,
            journal=self.root / "ordered.json",
            metrics=lambda: {},
        )
        self.assertEqual(sequence, ["reduce", "restart"])
        valid[0] = True
        sequence.clear()
        self.store.reduce_workers.side_effect = ValueError("prior reduction")
        incidents.apply_actions(
            {"actions": actions},
            self.trusted,
            self.store,
            consent=True,
            journal=self.root / "failure.json",
            metrics=lambda: {},
        )
        self.assertEqual(sequence, [])

    def test_invalid_io_does_not_trigger_and_sample_gap_resets(self):
        launch = Mock()
        scheduler = incidents.IncidentScheduler(self.root, launch=launch)
        settings = ResourceSettings(investigator="codex")
        for invalid in (float("nan"), float("inf"), True, -1, 101):
            memory = Memory(8192, 1000, 0, 0, invalid)
            scheduler.sample(memory, Limits(), settings, now=0)
            scheduler.sample(memory, Limits(), settings, now=60)
        launch.assert_not_called()

    def test_worker_deadline_interrupts_and_restores_alarm(self):
        import signal
        import time

        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(incidents.WorkerTimeout), incidents.worker_deadline(seconds=0.01):
            time.sleep(0.1)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)

    def test_consent_revocation_between_actions_stops_later_mutations(self):
        actions = [
            {"kind": "defer", "job_id": self.job, "ttl_seconds": 300},
            {"kind": "restart", "job_id": self.job},
        ]
        incidents.apply_actions(
            {"actions": actions},
            self.trusted,
            self.store,
            consent=True,
            consent_check=Mock(side_effect=[True, False]),
            journal=self.root / "revoke.json",
            metrics=lambda: {},
        )
        self.store.defer_job.assert_called_once()
        self.store.request_restart.assert_not_called()


class ResourceCliTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(incidents)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(
            os.environ,
            HOME=str(self.root),
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_STATE_HOME=str(self.root / "state"),
        )

    def run_cli(self, *args):
        import subprocess
        import sys

        return subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "bin/codex-resource"),
                *args,
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_independent_configure_status_and_disable_without_services(self):
        for arguments, field in [
            (("--protect-agents",), "protect_agents"),
            (("--queue-background",), "queue_background"),
            (("--automatic-actions",), "automatic_actions"),
        ]:
            result = self.run_cli("configure", *arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)[field])
        result = self.run_cli(
            "configure",
            "--no-protect-agents",
            "--no-queue-background",
            "--no-automatic-actions",
            "--investigator",
            "off",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["automatic_actions"])
        status = json.loads(self.run_cli("status").stdout)
        self.assertEqual(status["settings"]["investigator"], "off")
        self.assertFalse((self.root / "config/systemd").exists())
        self.assertFalse((self.root / "state").exists())

    def test_invalid_configure_preserves_previous_file(self):
        self.run_cli("configure", "--protect-agents")
        result = self.run_cli("configure", "--investigator-model", "bad slug")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(json.loads(self.run_cli("status").stdout)["settings"]["protect_agents"])

    def test_health_schedules_after_status_and_hot_reloads(self):
        from codex_looper import health

        scheduler = Mock()
        settings = ResourceSettings(investigator="codex")
        events = []
        scheduler.sample.side_effect = lambda *a, **k: events.append("schedule")
        monitor = health.HealthMonitor(self.root, Limits())
        with (
            patch.object(health, "read_memory", return_value=Memory(8192, 1000, 0)),
            patch.object(health, "write_status", side_effect=lambda *a: events.append("status")),
            patch.object(health.subprocess, "run", return_value=Mock(stdout="")),
            patch("codex_looper.resource_config.load_settings", return_value=settings) as loader,
            patch.object(incidents, "IncidentScheduler", return_value=scheduler),
        ):
            monitor.tick(set(), lambda _: "", now=0)
            monitor.tick(set(), lambda _: "", now=15)
        self.assertEqual(events, ["status", "schedule", "status", "schedule"])
        self.assertEqual(loader.call_count, 2)

    def test_inherited_unlocked_descriptor_cannot_bypass_active_worker(self):
        directory = self.root / "resources"
        with incidents.incident_lock(directory):
            fd = os.open(directory / "investigator.lock", os.O_RDWR)
            try:
                with patch.object(incidents, "_investigate") as investigate:
                    result = incidents.run_investigation(directory, lock_fd=fd)
                self.assertEqual(result["status"], "investigator_busy")
                investigate.assert_not_called()
            finally:
                os.close(fd)

    def test_failed_cooldown_reservation_never_launches(self):
        launcher = Mock()
        scheduler = incidents.IncidentScheduler(self.root, launch=launcher)
        settings = ResourceSettings(investigator="codex")
        memory = Memory(8192, 1000, 0)
        scheduler.sample(memory, Limits(), settings, now=0)
        with patch.object(incidents, "write_private_json", side_effect=OSError("fixture")):
            scheduler.sample(memory, Limits(), settings, now=60)
        launcher.assert_not_called()
