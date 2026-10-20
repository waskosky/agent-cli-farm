"""Kernel-control verification uses private cgroup trees, never live scopes."""

import tempfile
import unittest
from pathlib import Path

from codex_looper.resource_jobs import kernel_limits_effective


class KernelLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scope = "codexfarm-batch-" + "a" * 32 + ".scope"
        self.directory = self.root / "parent" / self.scope
        self.directory.mkdir(parents=True)
        self.identity = {"scope": self.scope, "cgroup": "0::/parent/" + self.scope}

    def verify(self, **limits):
        return kernel_limits_effective(self.identity, cgroup_root=self.root, **limits)

    def test_requested_cpu_limit_must_exist_and_be_effective(self):
        self.assertFalse(self.verify(cpu_quota=50))
        (self.directory / "cpu.max").write_text("max 100000\n")
        self.assertFalse(self.verify(cpu_quota=50))
        (self.directory / "cpu.max").write_text("50000 100000\n")
        self.assertTrue(self.verify(cpu_quota=50))
        (self.directory / "cpu.max").write_text("25000 100000\n")
        self.assertTrue(self.verify(cpu_quota=50))
        self.assertFalse(self.verify(unlimited_cpu=True))
        (self.directory / "cpu.max").write_text("max 100000\n")
        self.assertTrue(self.verify(unlimited_cpu=True))

    def test_memory_and_thermal_controller_requirements_do_not_silently_pass(self):
        self.assertFalse(self.verify(require_cpu=True))
        (self.directory / "cpu.max").write_text("max 100000\n")
        self.assertTrue(self.verify(require_cpu=True))
        self.assertFalse(self.verify(memory_max=128))
        (self.directory / "memory.max").write_text(str(128 * 1024 * 1024))
        self.assertTrue(self.verify(memory_max=128))
        (self.directory / "memory.max").write_text("max")
        self.assertFalse(self.verify(memory_max=128))
        (self.directory / "memory.max").write_text(str(256 * 1024 * 1024))
        self.assertFalse(self.verify(memory_max=128))

    def test_invalid_or_moved_scope_cannot_prove_enforcement(self):
        for group in (
            "0::/unrelated",
            "0::/parent/../" + self.scope,
            "2:cpu:/parent/" + self.scope,
        ):
            self.identity["cgroup"] = group
            self.assertFalse(self.verify())
