import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from codex_looper import resource_config as config
except ImportError:
    config = None


class ResourceSettingsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(config, "resource settings module missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "config")}
        self.patch = patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_absent_settings_are_all_off_without_writes(self):
        value = config.load_settings()
        self.assertFalse(value.protect_agents)
        self.assertFalse(value.queue_background)
        self.assertFalse(value.automatic_actions)
        self.assertEqual(value.investigator, "off")
        self.assertEqual(
            (value.reserve_mib, value.recovery_mib, value.recovery_seconds), (1024, 1536, 30)
        )
        self.assertEqual(value.queue_timeout, 300)
        self.assertFalse((self.root / "config").exists())

    def test_write_roundtrip_private_atomic_and_unrestricted_home(self):
        self.root.chmod(0o755)
        path = config.write_settings(config.ResourceSettings(protect_agents=True))
        self.assertTrue(config.load_settings().protect_agents)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(json.loads(path.read_text())["protect_agents"], True)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_invalid_explicit_types_values_unknown_fields(self):
        for fields in [
            {"protect_agents": 1},
            {"queue_background": "true"},
            {"automatic_actions": None},
            {"investigator": "on"},
            {"reserve_mib": True},
            {"reserve_mib": float("nan")},
            {"queue_timeout": -1},
            {"queue_timeout": float("inf")},
            {"recovery_seconds": -1},
            {"reserve_mib": 2048},
            {"unknown": True},
        ]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                config.ResourceSettings.from_dict(fields)

    def test_rejects_symlink_public_nonregular_and_wrong_owner(self):
        path = config.write_settings(config.ResourceSettings())
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            config.load_settings()
        path.unlink()
        target = self.root / "target"
        target.write_text("{}")
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            config.load_settings()
        path.unlink()
        path.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            config.load_settings()
        path.rmdir()
        config.write_settings(config.ResourceSettings())
        with (
            patch.object(config.os, "getuid", return_value=os.getuid() + 1),
            self.assertRaises(ValueError),
        ):
            config.load_settings()

    def test_rejects_nonprivate_or_symlink_target_directory(self):
        path = config.write_settings(config.ResourceSettings())
        path.parent.chmod(0o755)
        with self.assertRaises(ValueError):
            config.load_settings()
        config.write_settings(config.ResourceSettings())
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        path.parent.chmod(0o700)
        path.unlink()
        path.parent.rmdir()
        destination = self.root / "destination"
        destination.mkdir(mode=0o700)
        path.parent.symlink_to(destination)
        with self.assertRaises(ValueError):
            config.write_settings(config.ResourceSettings())

    def test_malformed_duplicate_oversized_json_fails_cleanly(self):
        path = config.write_settings(config.ResourceSettings())
        for data in ["{", '{"protect_agents":true,"protect_agents":false}', " " * 65537]:
            path.write_text(data)
            with self.subTest(data=data[:60]), self.assertRaises(ValueError):
                config.load_settings()

    def test_missing_file_in_existing_public_target_defaults_without_chmod(self):
        path = config.settings_path()
        path.parent.mkdir(parents=True, mode=0o775)
        path.parent.chmod(0o775)
        self.assertFalse(config.load_settings().protect_agents)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o775)

    def test_explicit_investigator_model_binary_validation(self):
        value = config.ResourceSettings.from_dict(
            {"investigator_model": "gpt-6.1-sol", "investigator_binary": "/opt/codex"}
        )
        self.assertEqual(value.investigator_model, "gpt-6.1-sol")
        for changes in [
            {"investigator_model": True},
            {"investigator_model": "../x"},
            {"investigator_model": "x\n"},
            {"investigator_binary": "codex;echo x"},
            {"investigator_binary": 1},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config.ResourceSettings.from_dict(changes)
