from __future__ import annotations

import unittest

from codex_looper.health import Memory
from codex_looper.resource_policy import HeadroomGate, HeadroomSettings


class HeadroomPolicyTests(unittest.TestCase):
    def test_default_settings_and_healthy_first_sample(self):
        settings = HeadroomSettings()
        self.assertEqual(
            (settings.reserve_mib, settings.recovery_mib, settings.recovery_seconds),
            (1024, 1536, 30),
        )
        gate = HeadroomGate(settings)
        self.assertTrue(gate.sample(Memory(8192, 1400, 4000), now=0))
        self.assertTrue(gate.reason)

    def test_exact_reserve_enters_wait_and_exact_recovery_starts_timer(self):
        gate = HeadroomGate()
        self.assertFalse(gate.sample(Memory(8192, 1024, 0), now=0))
        self.assertFalse(gate.sample(Memory(8192, 1536, 0), now=10))
        self.assertFalse(gate.sample(Memory(8192, 1536, 0), now=39.99))
        self.assertTrue(gate.sample(Memory(8192, 1536, 0), now=40))
        self.assertTrue(gate.reason)

    def test_waiting_midband_does_not_admit(self):
        gate = HeadroomGate()
        self.assertFalse(gate.sample(Memory(8192, 1000, 0), now=0))
        self.assertFalse(gate.sample(Memory(8192, 1400, 0), now=100))
        self.assertFalse(gate.sample(Memory(8192, 1535, 0), now=200))

    def test_critical_pressure_alone_enters_wait(self):
        gate = HeadroomGate()
        self.assertFalse(gate.sample(Memory(8192, 4096, 0, 25), now=0))
        self.assertFalse(gate.sample(Memory(8192, 4096, 0, 10), now=100))
        self.assertFalse(gate.sample(Memory(8192, 4096, 0, 9.99), now=110))
        self.assertTrue(gate.sample(Memory(8192, 4096, 0, 9.99), now=140))

    def test_new_available_dip_resets_recovery_timer(self):
        gate = HeadroomGate()
        for now, available in ((0, 1024), (10, 2048), (35, 1400), (36, 2048), (65, 2048)):
            self.assertFalse(gate.sample(Memory(8192, available, 0), now=now))
        self.assertTrue(gate.sample(Memory(8192, 2048, 0), now=66))

    def test_warning_pressure_resets_recovery_timer(self):
        gate = HeadroomGate()
        for now, pressure in ((0, 25), (10, 0), (35, 10), (36, 0), (65, 0)):
            self.assertFalse(gate.sample(Memory(8192, 2048, 0, pressure), now=now))
        self.assertTrue(gate.sample(Memory(8192, 2048, 0), now=66))

    def test_clock_rollback_resets_recovery_timer(self):
        gate = HeadroomGate()
        self.assertFalse(gate.sample(Memory(8192, 1024, 0), now=0))
        self.assertFalse(gate.sample(Memory(8192, 2048, 0), now=100))
        self.assertFalse(gate.sample(Memory(8192, 2048, 0), now=90))
        self.assertFalse(gate.sample(Memory(8192, 2048, 0), now=119))
        self.assertTrue(gate.sample(Memory(8192, 2048, 0), now=120))

    def test_unknown_invalid_and_tiny_readings_admit_and_clear_waiting(self):
        for memory in (
            None,
            Memory(0, 0, 0),
            Memory(1536, 100, 0, 99),
            Memory(8192, -1, 0),
            Memory(8192, 9000, 0),
            Memory(float("nan"), 2048, 0),
            Memory(8192, float("inf"), 0),
            Memory(8192, 2048, 0, float("nan")),
            Memory(8192, 2048, 0, 101),
            Memory(True, 0, 0),
            Memory(8192, 2048, 0, -1),
        ):
            with self.subTest(memory=memory):
                gate = HeadroomGate()
                self.assertFalse(gate.sample(Memory(8192, 1024, 0), now=0))
                self.assertTrue(gate.sample(memory, now=1))
                self.assertIn("advisory", gate.reason.lower())
                self.assertTrue(gate.sample(Memory(8192, 1400, 0), now=2))

    def test_small_host_uses_custom_recovery_threshold(self):
        gate = HeadroomGate(HeadroomSettings(256, 512, 5))
        self.assertTrue(gate.sample(Memory(512, 100, 0), now=0))
        self.assertFalse(gate.sample(Memory(1024, 256, 0), now=1))
        self.assertFalse(gate.sample(Memory(1024, 512, 0), now=2))
        self.assertTrue(gate.sample(Memory(1024, 512, 0), now=7))

    def test_zero_recovery_seconds_admits_on_first_recovered_sample(self):
        gate = HeadroomGate(HeadroomSettings(recovery_seconds=0))
        self.assertFalse(gate.sample(Memory(8192, 1024, 0), now=0))
        self.assertTrue(gate.sample(Memory(8192, 1536, 0), now=1))

    def test_admitted_gate_can_enter_wait_again(self):
        gate = HeadroomGate(HeadroomSettings(recovery_seconds=0))
        self.assertFalse(gate.sample(Memory(8192, 1024, 0), now=0))
        self.assertTrue(gate.sample(Memory(8192, 1536, 0), now=1))
        self.assertFalse(gate.sample(Memory(8192, 1024, 0), now=2))

    def test_invalid_settings_are_rejected(self):
        for field in ("reserve_mib", "recovery_mib", "recovery_seconds"):
            for value in (True, "1024", None, float("nan"), float("inf"), -1):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    HeadroomSettings(**{field: value})
        for values in ({"reserve_mib": 0}, {"recovery_mib": 1024}, {"recovery_mib": 512}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                HeadroomSettings(**values)


if __name__ == "__main__":
    unittest.main()
