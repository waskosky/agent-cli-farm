"""Reservations are serialized before payload start; never exercise live scopes."""

import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_looper.health import Memory
from codex_looper.resource_config import ResourceSettings
from codex_looper.resource_jobs import JobStore, admit


def concurrent_claim(ready, release, results):
    store = JobStore()
    record = store.create("batch", ["true"], "/private", max_parallel=1, admission_group="builds")
    ready.wait(5)
    results.put(store.reserve_admission(record["job_id"], ResourceSettings()))
    release.wait(5)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.store = JobStore()

    def job(self, **options):
        return self.store.create("batch", ["true"], "/private", **options)["job_id"]

    def test_concurrency_reservation_survives_launch_gap_and_preserves_stronger_choice(self):
        first = self.job(max_parallel=1, admission_group="builds")
        second = self.job(max_parallel=4, admission_group="builds")
        self.assertIsNone(self.store.reserve_admission(first, ResourceSettings()))
        self.assertIn("concurrency", self.store.reserve_admission(second, ResourceSettings()))
        self.store.update(first, status="finished")
        self.assertIsNone(self.store.reserve_admission(second, ResourceSettings()))

    def test_unrelated_groups_and_unselected_jobs_have_no_limit(self):
        first = self.job(max_parallel=1, admission_group="builds")
        second = self.job(max_parallel=1, admission_group="renders")
        self.assertIsNone(self.store.reserve_admission(first, ResourceSettings()))
        self.assertIsNone(self.store.reserve_admission(second, ResourceSettings()))
        self.assertIsNone(self.store.reserve_admission(self.job(), ResourceSettings()))

    def test_memory_reservations_are_atomic_and_do_not_accept_unknown_counters(self):
        first = self.job(reserve_memory_mib=2000)
        second = self.job(reserve_memory_mib=2000)
        with patch("codex_looper.resource_jobs.read_memory", return_value=Memory(8000, 4500, 0)):
            self.assertIsNone(self.store.reserve_admission(first, ResourceSettings()))
            self.assertIn("reserved", self.store.reserve_admission(second, ResourceSettings()))
            self.store.update(first, status="finished")
            self.assertIsNone(self.store.reserve_admission(second, ResourceSettings()))
        with patch("codex_looper.resource_jobs.read_memory", return_value=None):
            self.assertIn(
                "unavailable",
                self.store.reserve_admission(self.job(reserve_memory_mib=1), ResourceSettings()),
            )

    def test_memory_ignore_does_not_bypass_explicit_reservation(self):
        first = self.job(max_parallel=1, admission_group="builds")
        second = self.job(max_parallel=1, admission_group="builds")
        self.assertIsNone(self.store.reserve_admission(first, ResourceSettings()))
        self.assertFalse(
            admit("batch", "ignore", ResourceSettings(), 0, self.store, "x", job_id=second)
        )

    def test_dead_owner_frees_capacity_but_unknown_owner_preserves_it(self):
        first = self.job(max_parallel=1, admission_group="builds")
        second = self.job(max_parallel=1, admission_group="builds")
        self.assertIsNone(self.store.reserve_admission(first, ResourceSettings()))
        from codex_looper import resource_jobs

        original = resource_jobs._recorded_process_state
        for state, expected in (("unknown", False), ("dead", True)):

            def identity(record, kind, selected=state):
                return selected if record["job_id"] == first else original(record, kind)

            with patch.object(resource_jobs, "_recorded_process_state", side_effect=identity):
                result = self.store.reserve_admission(second, ResourceSettings())
                self.assertEqual(result is None, expected)

    def test_invalid_admission_options_and_agents_fail_before_registration(self):
        for values in (
            {"max_parallel": True},
            {"max_parallel": 0},
            {"admission_group": "builds"},
            {"max_parallel": 1, "admission_group": "a b"},
            {"reserve_memory_mib": float("nan")},
            {"reserve_memory_mib": -1},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.job(**values)
        with self.assertRaises(ValueError):
            self.store.create("agent", ["true"], "/private", max_parallel=1)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process identities and flock")
    def test_simultaneous_processes_cannot_claim_the_same_slot(self):
        context = multiprocessing.get_context("fork")
        ready, release, results = context.Event(), context.Event(), context.Queue()
        children = [
            context.Process(target=concurrent_claim, args=(ready, release, results))
            for _ in range(2)
        ]
        try:
            for child in children:
                child.start()
            ready.set()
            claims = [results.get(timeout=5) for _ in children]
            self.assertEqual(claims.count(None), 1, claims)
        finally:
            release.set()
            for child in children:
                child.join(5)
                if child.is_alive():
                    child.terminate()
                    child.join(2)
        self.assertTrue(all(child.exitcode == 0 for child in children))
