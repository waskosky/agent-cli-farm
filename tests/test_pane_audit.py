from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_BIN = REPO_ROOT / "bin" / "codex-audit-panes.py"
SESSION_ID = "019e1659-3a2f-7a40-95cf-5ac9dd7fe5d4"
SECOND_ID = "123e4567-e89b-42d3-a456-426614174001"


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class PaneAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pane-audit-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.state_file = self.root / "panes.json"
        self.calls_file = self.root / "calls.jsonl"
        self.manifest = self.root / "manifest.tsv"
        self.save_helper = self.fake_bin / "codex-save"
        self.state = {
            "panes": [{"pane": "%1", "pid": "101", "provider": "codex", "id": SESSION_ID}]
        }
        self.manifest.write_text(
            f"name\tdir\tcmd\targs\nproject\t/tmp/project\tcodex\tresume {SESSION_ID}\n",
            encoding="utf-8",
        )
        make_executable(
            self.fake_bin / "tmux",
            """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
calls = Path(os.environ['AUDIT_TEST_CALLS'])
previous = calls.read_text().splitlines() if calls.exists() else []
with calls.open('a') as handle:
    handle.write(json.dumps(['tmux', *args]) + '\\n')
state = json.loads(Path(os.environ['AUDIT_TEST_STATE']).read_text())
if args and args[0] == 'list-panes':
    if state.get('enumeration_status', 0):
        print(state.get('error', 'pane enumeration unavailable'), file=sys.stderr)
        sys.exit(state['enumeration_status'])
    if 'enumeration_output' in state:
        print(state['enumeration_output'], end='')
        sys.exit(0)
    already_listed = any(json.loads(call)[:2] == ['tmux', 'list-panes'] for call in previous)
    panes = state.get('panes_after', state['panes']) if already_listed else state['panes']
    for pane in panes:
        print(pane['pane'] + '\\t' + pane['pid'] + '\\t' + pane.get('utility', ''))
    sys.exit(0)
print('unexpected tmux command', file=sys.stderr)
sys.exit(73)
""",
        )
        make_executable(
            self.save_helper,
            """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['AUDIT_TEST_CALLS']).open('a') as handle:
    handle.write(json.dumps(['save', *args]) + '\\n')
state = json.loads(Path(os.environ['AUDIT_TEST_STATE']).read_text())
pane = next((pane for pane in state['panes'] if pane['pane'] == args[-1]), {})
if pane.get('helper_error'):
    print(pane['helper_error'], file=sys.stderr)
if args[0] == '--inspect-provider':
    provider = pane.get('provider', '')
    if provider:
        print(provider)
    sys.exit(pane.get('provider_status', 0 if provider else 1))
if args[0] == '--inspect-pane':
    if 'identity_output' in pane:
        print(pane['identity_output'])
    elif pane.get('id'):
        print(pane['provider'] + '\\t' + pane['id'])
    sys.exit(pane.get('identity_status', 0 if pane.get('id') else 1))
if args[0] == '--inspect-shared':
    if pane.get('shared'):
        previous = Path(os.environ['AUDIT_TEST_CALLS']).read_text().splitlines()
        count = sum(json.loads(call) == ['save', '--inspect-shared', pane['pane']] for call in previous)
        print(pane.get('shared_after', pane['shared']) if count > 1 else pane['shared'])
        sys.exit(0)
    sys.exit(1)
print('unexpected saver command', file=sys.stderr)
sys.exit(73)
""",
        )
        self.env = {
            **os.environ,
            "HOME": str(self.root),
            "PATH": f"{self.fake_bin}:{os.environ.get('PATH', '')}",
            "CODEX_SAVE_BIN": str(self.save_helper),
            "AUDIT_TEST_CALLS": str(self.calls_file),
            "AUDIT_TEST_STATE": str(self.state_file),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }

    def run_audit(self, *, executable: Path = AUDIT_BIN) -> subprocess.CompletedProcess[str]:
        self.state_file.write_text(json.dumps(self.state), encoding="utf-8")
        before = self.manifest.read_bytes() if self.manifest.exists() else None
        result = subprocess.run(
            [sys.executable, executable, "--session", "test", self.manifest],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(self.manifest.read_bytes() if self.manifest.exists() else None, before)
        self.assertNotIn(SESSION_ID, result.stdout + result.stderr)
        self.assertNotIn(SECOND_ID, result.stdout + result.stderr)
        return result

    def test_complete_coverage_inspects_all_panes_by_id_without_mutation(self) -> None:
        self.state["panes"].insert(0, {"pane": "%0", "pid": "100"})

        result = self.run_audit()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("live pane recovery coverage is complete", result.stdout)
        calls = [json.loads(line) for line in self.calls_file.read_text().splitlines()]
        tmux_calls = [call for call in calls if call[0] == "tmux"]
        self.assertTrue(tmux_calls)
        self.assertTrue(all(call[1] == "list-panes" for call in tmux_calls))
        self.assertTrue(all("-s" in call and "=test" in call for call in tmux_calls))
        self.assertTrue(all("#{pane_pid}" in call[-1] for call in tmux_calls))
        self.assertIn(["save", "--inspect-provider", "%0"], calls)
        self.assertIn(["save", "--inspect-pane", "%1"], calls)

    def test_home_provider_missing_from_manifest_is_reported(self) -> None:
        self.state["panes"].insert(
            0, {"pane": "%0", "pid": "100", "provider": "codex", "id": SECOND_ID}
        )

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("pane %0", result.stdout)
        self.assertIn("missing from manifest", result.stdout)

    def test_secondary_provider_missing_from_manifest_is_reported(self) -> None:
        self.state["panes"].append(
            {"pane": "%2", "pid": "102", "provider": "claude", "id": SECOND_ID}
        )

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("pane %2", result.stdout)
        self.assertIn("missing from manifest", result.stdout)

    def test_identity_must_match_provider_as_well_as_uuid(self) -> None:
        self.state["panes"][0]["provider"] = "gemini"

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("missing from manifest", result.stdout)

    def test_covered_providers_with_duplicate_window_names_are_accepted(self) -> None:
        self.state["panes"].append(
            {"pane": "%2", "pid": "102", "provider": "claude", "id": SECOND_ID}
        )
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write(f"project\t/tmp/other\tclaude\t--resume {SECOND_ID}\n")

        result = self.run_audit()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("2 provider pane(s)", result.stdout)

    def test_unknown_conversation_id_is_reported(self) -> None:
        self.state["panes"][0].pop("id")

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unknown conversation ID", result.stdout)

    def shared_inventory(self) -> None:
        helper = self.fake_bin / "shared-meta"
        make_executable(
            helper,
            """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = json.loads(Path(os.environ['AUDIT_TEST_STATE']).read_text())
saved = Path(sys.argv[-1]).read_text()
sys.exit(1 if state.get('shared_error') or any(value not in saved for value in state['shared_ids']) else 0)
""",
        )
        self.env["CODEX_SHARED_META_BIN"] = str(helper)
        self.state["shared_ids"] = [SESSION_ID, SECOND_ID]
        self.state["panes"] = [
            {
                "pane": "%1",
                "pid": "101",
                "provider": "codex",
                "identity_status": 4,
                "shared": "/tmp/codex-server.sock",
            },
            {"pane": "%2", "pid": "102", "provider": "codex", "shared": "/tmp/codex-server.sock"},
        ]

    def test_complete_shared_server_inventory_covers_unmapped_panes(self) -> None:
        self.shared_inventory()
        with self.manifest.open("a") as handle:
            handle.write(f"second\t/tmp/project\tcodex\tresume {SECOND_ID}\n")
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "2 pane(s) covered by complete local shared-server inventories", result.stdout
        )
        self.assertNotIn("unverified legacy binding", result.stdout)

    def test_partial_shared_inventory_cannot_be_reported_healthy(self) -> None:
        self.shared_inventory()
        result = self.run_audit()
        self.assertEqual(result.returncode, 1)
        self.assertIn("complete local shared-server inventory", result.stdout)
        self.assertNotIn("coverage is complete", result.stdout)

    def test_shared_socket_change_during_audit_prevents_healthy_result(self) -> None:
        self.shared_inventory()
        self.state["shared_ids"] = [SESSION_ID]
        self.state["panes"][0]["shared_after"] = "/tmp/different-server.sock"
        result = self.run_audit()
        self.assertEqual(result.returncode, 1)
        self.assertIn("shared-server coverage changed", result.stdout)

    def test_legacy_static_binding_requires_normal_exit_and_current_id(self) -> None:
        self.state["panes"][0]["identity_status"] = 4

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unverified legacy binding", result.stdout)
        self.assertIn("currently displayed", result.stdout)
        self.assertIn("exit normally", result.stdout)
        self.assertIn("codex-add", result.stdout)
        self.assertIn("CODEX_SESSION=test codex-save", result.stdout)

    def test_idle_explicit_history_picker_can_be_excluded(self) -> None:
        self.state["panes"].append(
            {
                "pane": "%2",
                "pid": "102",
                "provider": "codex",
                "utility": "history-picker",
                "identity_status": 5,
            }
        )

        result = self.run_audit()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 idle history picker(s) excluded", result.stdout)

    def test_marked_shared_server_with_unknown_identity_cannot_be_excluded(self) -> None:
        self.state["panes"][0].pop("id")
        self.state["panes"][0].update(utility="history-picker", identity_status=1)

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unknown conversation ID", result.stdout)
        self.assertIn("0 idle history picker(s) excluded", result.stdout)

    def test_nonprovider_marker_does_not_claim_proven_idle_picker(self) -> None:
        self.state["panes"].append({"pane": "%2", "pid": "102", "utility": "history-picker"})

        result = self.run_audit()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 idle history picker(s) excluded", result.stdout)

    def test_positive_idle_result_requires_explicit_marker(self) -> None:
        self.state["panes"][0].pop("id")
        self.state["panes"][0]["identity_status"] = 5

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("0 idle history picker(s) excluded", result.stdout)

    def test_positive_idle_result_with_private_output_is_not_excluded(self) -> None:
        self.state["panes"][0].update(utility="history-picker", identity_status=5)
        for output in (f"codex\t{SESSION_ID}", " "):
            with self.subTest(output=output):
                self.state["panes"][0]["identity_output"] = output

                result = self.run_audit()

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("0 idle history picker(s) excluded", result.stdout)

    def test_positive_idle_result_with_private_diagnostics_is_not_excluded(self) -> None:
        self.state["panes"][0].pop("id")
        self.state["panes"][0].update(
            utility="history-picker", identity_status=5, helper_error=SESSION_ID
        )

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("0 idle history picker(s) excluded", result.stdout)

    def test_picker_with_verified_conversation_requires_manifest_coverage(self) -> None:
        self.state["panes"].append(
            {
                "pane": "%2",
                "pid": "102",
                "provider": "codex",
                "id": SECOND_ID,
                "utility": "history-picker",
            }
        )

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("missing from manifest", result.stdout)

    def test_picker_with_legacy_binding_cannot_be_excluded(self) -> None:
        self.state["panes"][0].update(utility="history-picker", identity_status=4)

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unverified legacy binding", result.stdout)

    def test_picker_with_ambiguous_identity_cannot_be_excluded(self) -> None:
        self.state["panes"][0].pop("id")
        self.state["panes"][0].update(utility="history-picker", identity_status=3)

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unknown conversation ID", result.stdout)
        self.assertIn("0 idle history picker(s) excluded", result.stdout)

    def test_arbitrary_utility_marker_does_not_hide_unknown_conversation(self) -> None:
        self.state["panes"][0].pop("id")
        self.state["panes"][0]["utility"] = "custom"

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unknown conversation ID", result.stdout)

    def test_failed_pane_enumeration_is_reported_without_echoing_private_output(self) -> None:
        self.state.update(enumeration_status=1, error=SESSION_ID)

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unable to enumerate panes", result.stdout)

    def test_empty_or_malformed_pane_output_cannot_prove_coverage(self) -> None:
        for output in (
            "",
            "garbage\n",
            "%1\n",
            "%1\t\n",
            "%1\t0\t\n",
            "%1\t\t\n",
            "%1\t101\t\n%1\t101\t\n",
        ):
            with self.subTest(output=output):
                self.state["enumeration_output"] = output

                result = self.run_audit()

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("pane enumeration", result.stdout)
                self.assertNotIn("coverage is complete", result.stdout)

    def test_pane_changes_during_inspection_prevent_complete_coverage(self) -> None:
        self.state["panes_after"] = [*self.state["panes"], {"pane": "%2", "pid": "102"}]

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("panes changed during audit", result.stdout)

    def test_respawn_inside_same_pane_cannot_use_previous_verified_identity(self) -> None:
        self.state["panes_after"] = [{**self.state["panes"][0], "pid": "9001"}]

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("panes changed during audit", result.stdout)

    def test_missing_inspection_helper_is_reported(self) -> None:
        self.env["CODEX_SAVE_BIN"] = str(self.root / "missing-save")

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("pane inspection helper unavailable", result.stdout)

    def test_provider_inspection_failure_is_reported(self) -> None:
        self.state["panes"][0].update(provider_status=2, helper_error=SESSION_ID)

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("provider inspection failed", result.stdout)

    def test_identity_inspection_failure_is_reported_without_private_diagnostics(self) -> None:
        self.state["panes"][0].update(identity_status=2, helper_error=SESSION_ID)

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("identity inspection failed", result.stdout)

    def test_invalid_verified_identity_is_rejected(self) -> None:
        self.state["panes"][0]["identity_output"] = "codex\tnot-a-uuid"

        result = self.run_audit()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("invalid identity inspection result", result.stdout)

    def test_missing_or_invalid_manifest_is_reported(self) -> None:
        for content in (None, "bad-header\n", "name\tdir\tcmd\targs\n"):
            with self.subTest(content=content):
                if content is None:
                    self.manifest.unlink()
                else:
                    self.manifest.write_text(content, encoding="utf-8")

                result = self.run_audit()

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("unable to read a valid manifest", result.stdout)

    def test_installed_audit_uses_adjacent_saver_when_override_is_unset(self) -> None:
        installed = self.root / "installed"
        installed.mkdir()
        shutil.copy2(AUDIT_BIN, installed / AUDIT_BIN.name)
        shutil.copy2(REPO_ROOT / "bin/codex-manifest.py", installed / "codex-manifest.py")
        shutil.copy2(self.save_helper, installed / "codex-save")
        self.env.pop("CODEX_SAVE_BIN")

        result = self.run_audit(executable=installed / AUDIT_BIN.name)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("coverage is complete", result.stdout)


if __name__ == "__main__":
    unittest.main()
