import http.server
import json
import os
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_looper.resource_config import ResourceSettings

try:
    from codex_looper import resource_investigator as investigator
except ImportError:
    investigator = None


class InvestigatorTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(investigator, "isolated Codex investigator missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = self.root / "auth.json"
        self.auth.write_text('{"synthetic":"DO_NOT_READ_OR_PRINT"}')
        self.auth.chmod(0o600)
        self.binary = self.root / "fake-codex"
        self.binary.write_text(
            "#!"
            + sys.executable
            + "\n"
            + """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args == ['exec', '--help']:
    print('--ignore-user-config --ignore-rules --ephemeral --sandbox --skip-git-repo-check --json --output-schema --output-last-message')
elif args == ['features', 'list']:
    print('skip_host_skill_discovery stable false\\nshell_tool stable true\\nunified_exec stable true\\nunbounded_connection_retries stable false')
elif args == ['debug', 'models', '--bundled']:
    print(json.dumps({'models': [{'slug': 'gpt-6.1-sol', 'shell_type': 'shell_command', 'tool_mode': 'code_mode', 'multi_agent_version': 'v2', 'experimental_supported_tools': ['exec'], 'apply_patch_tool_type': 'freeform', 'base_instructions': 'do not use default instructions', 'preserved': 42}]}))
else:
    assert os.environ.get('OPENAI_API_KEY') is None
    assert os.environ.get('SECRET_PARENT') is None
    assert Path(os.environ['HOME']).stat().st_mode & 0o777 == 0o700
    assert Path(os.environ['CODEX_HOME'], 'auth.json').is_symlink()
    configs = dict(args[i+1].split('=', 1) for i, x in enumerate(args) if x == '-c')
    catalog = json.loads(Path(json.loads(configs['model_catalog_json'])).read_text())['models'][0]
    assert catalog['shell_type'] == 'disabled'
    assert catalog['tool_mode'] is None
    assert catalog['multi_agent_version'] is None
    assert catalog['experimental_supported_tools'] == []
    assert catalog['preserved'] == 42
    assert '--ignore-user-config' in args and '--ignore-rules' in args
    assert '--ephemeral' in args and args[args.index('--sandbox')+1] == 'read-only'
    assert 'model_instructions_file' in configs
    assert json.loads(configs['mcp_servers']) == {}
    prompt = sys.stdin.read()
    assert len(prompt.encode()) <= 2500
    answer = {'diagnosis': 'fixture', 'evidence': [], 'proposed_fixes': [], 'actions': []}
    Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(answer))
    print(json.dumps({'type': 'turn.completed'}))
"""
        )
        self.binary.chmod(0o700)

    def settings(self):
        return ResourceSettings(investigator="codex", investigator_binary=str(self.binary))

    def test_isolated_fake_cli_final_json_and_auth_only_link(self):
        raw = investigator.investigate(
            "{}", self.settings(), auth_path=self.auth, temporary_parent=self.root
        )
        self.assertEqual(json.loads(raw)["diagnosis"], "fixture")
        self.assertEqual(self.auth.read_text(), '{"synthetic":"DO_NOT_READ_OR_PRINT"}')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["auth.json", "fake-codex"])

    def test_unsafe_auth_and_unsupported_capabilities_fail_closed(self):
        self.auth.chmod(0o644)
        with self.assertRaises(ValueError):
            investigator.investigate("{}", self.settings(), auth_path=self.auth)
        self.auth.chmod(0o600)
        self.binary.write_text("#!" + sys.executable + '\nprint("unsupported")\n')
        with self.assertRaises(ValueError):
            investigator.investigate("{}", self.settings(), auth_path=self.auth)

    def test_oversize_input_fails_before_invocation(self):
        with self.assertRaises(ValueError):
            investigator.investigate("x" * 2501, self.settings(), auth_path=self.auth)

    def test_bounded_output_and_timeout_kill_only_owned_process_group(self):
        with self.assertRaises(ValueError):
            investigator.bounded_run(
                [sys.executable, "-c", 'print("x"*100000)'],
                env=dict(os.environ),
                cwd=self.root,
                deadline=time.monotonic() + 5,
                output_limit=1024,
            )
        marker = self.root / "child.pid"
        script = (
            'import subprocess,time,pathlib; p=subprocess.Popen(["'
            + sys.executable
            + '","-c","import time; time.sleep(60)"]); pathlib.Path('
            + repr(str(marker))
            + ").write_text(str(p.pid)); time.sleep(60)"
        )
        with self.assertRaises(ValueError):
            investigator.bounded_run(
                [sys.executable, "-c", script],
                env=dict(os.environ),
                cwd=self.root,
                deadline=time.monotonic() + 0.4,
                output_limit=1024,
            )
        pid = int(marker.read_text())
        for _ in range(50):
            status = Path(f"/proc/{pid}/stat")
            if not status.exists() or ") Z " in status.read_text():
                break
            time.sleep(0.01)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("owned descendant survived timeout")

    def test_catalog_normalization_preserves_metadata_and_private_large_file(self):
        record = {"slug": "gpt-6.1-sol", "metadata": "x" * 87000, "shell_type": "local"}
        result = investigator.normalized_catalog(json.dumps({"models": [record]}), "gpt-6.1-sol")
        path = investigator.ephemeral_file(
            self.root / "catalog.json", json.dumps(result), limit=1048576
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(result["models"][0]["metadata"], record["metadata"])
        with self.assertRaises(ValueError):
            investigator.normalized_catalog('{"models":[]}', "missing")


def frame(event):
    return ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()


def envelope(output):
    return {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "output": output,
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


def forced_tool(kind):
    item = {"id": "callitem_fixture", "call_id": "call_fixture", "type": "function_call"}
    if kind == "input":
        item.update(
            name="request_user_input",
            arguments=json.dumps(
                {
                    "questions": [
                        {
                            "header": "Fixture",
                            "id": "fixture",
                            "question": "Choose a fixture option.",
                            "options": [
                                {"label": "One", "description": "First fixture option."},
                                {"label": "Two", "description": "Second fixture option."},
                            ],
                        }
                    ]
                }
            ),
        )
    elif kind == "collaboration":
        item.update(name="list_agents", namespace="collaboration", arguments="{}")
    else:
        raise ValueError(kind)
    return [
        frame({"type": "response.output_item.added", "output_index": 0, "item": item}),
        frame({"type": "response.output_item.done", "output_index": 0, "item": item}),
        frame({"type": "response.completed", "response": envelope([item])}),
    ]


def final_message():
    content = {
        "type": "output_text",
        "text": '{"diagnosis":"fixture","evidence":[],"proposed_fixes":[],"actions":[]}',
        "annotations": [],
    }
    message = {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [content],
    }
    context = {"item_id": message["id"], "output_index": 0, "content_index": 0}
    return [
        frame(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": dict(message, status="in_progress", content=[]),
            }
        ),
        frame({"type": "response.content_part.added", **context, "part": dict(content, text="")}),
        frame({"type": "response.output_text.delta", **context, "delta": content["text"]}),
        frame({"type": "response.output_text.done", **context, "text": content["text"]}),
        frame({"type": "response.content_part.done", **context, "part": content}),
        frame({"type": "response.output_item.done", "output_index": 0, "item": message}),
        frame({"type": "response.completed", "response": envelope([message])}),
    ]


def tool_names(request):
    result = []

    def visit(items, prefix=""):
        for item in items or []:
            name = item.get("name", "")
            if item.get("type") == "namespace":
                visit(item.get("tools"), prefix + name + ".")
            elif name:
                result.append(prefix + name)

    visit(request.get("tools"))
    for item in request.get("input", []):
        if item.get("type") == "additional_tools":
            visit(item.get("tools"))
    return sorted(result)


@unittest.skipUnless(
    os.environ.get("CODEXFARM_TEST_NATIVE_CODEX"),
    "set CODEXFARM_TEST_NATIVE_CODEX for optional native loopback matrix",
)
class NativeInvestigatorTests(unittest.TestCase):
    def test_native_final_tools_dispatch_context_and_real_input_budget(self):
        for kind in (
            "final",
            "input",
            "exec",
            "exec_command",
            "patch",
            "collaboration",
            "positive",
        ):
            with self.subTest(kind=kind):
                self.native_case(kind)

    def native_case(self, kind):
        with tempfile.TemporaryDirectory(prefix="resource-native-test-") as temporary:
            root = Path(temporary)
            origin = root / "origin"
            origin.mkdir(mode=0o700)
            auth = origin / "auth.json"
            auth.write_text(
                json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "synthetic-private-key"})
            )
            auth.chmod(0o600)
            (origin / "AGENTS.md").write_text("ORIGINAL_CONTEXT_SENTINEL_DO_NOT_LOAD")
            skill = origin / "skills/fixture/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("ORIGINAL_SKILL_SENTINEL_DO_NOT_LOAD")
            sentinel = root / "FORBIDDEN_SIDE_EFFECT"
            requests = []

            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_POST(self):
                    requests.append(
                        json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    )
                    if kind == "final" or len(requests) > 1:
                        content = b"".join(final_message())
                    elif kind in ("input", "collaboration", "positive"):
                        content = b"".join(
                            forced_tool("collaboration" if kind == "positive" else kind)
                        )
                    else:
                        item = {"id": "callitem_fixture", "call_id": "call_fixture"}
                        if kind == "exec":
                            item.update(
                                type="custom_tool_call",
                                name="exec",
                                namespace="functions",
                                input='await tools.exec_command({cmd: "touch '
                                + str(sentinel)
                                + '"})',
                            )
                        elif kind == "exec_command":
                            item.update(
                                type="function_call",
                                name="exec_command",
                                arguments=json.dumps({"cmd": "touch " + str(sentinel)}),
                            )
                        else:
                            item.update(
                                type="custom_tool_call",
                                name="apply_patch",
                                input="*** Begin Patch\n*** Add File: "
                                + str(sentinel)
                                + "\n+forbidden\n*** End Patch",
                            )
                        content = b"".join(
                            [
                                frame(
                                    {
                                        "type": "response.output_item.added",
                                        "output_index": 0,
                                        "item": item,
                                    }
                                ),
                                frame(
                                    {
                                        "type": "response.output_item.done",
                                        "output_index": 0,
                                        "item": item,
                                    }
                                ),
                                frame({"type": "response.completed", "response": envelope([item])}),
                            ]
                        )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                wrapper = root / "codex-test-fixture"
                transport = {
                    "model_provider": "fixture",
                    "model_providers.fixture.name": "Local test fixture",
                    "model_providers.fixture.base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "model_providers.fixture.requires_openai_auth": True,
                    "model_providers.fixture.wire_api": "responses",
                    "model_providers.fixture.supports_websockets": False,
                }
                extra = []
                for name, value in transport.items():
                    extra.extend(["-c", name + "=" + json.dumps(value)])
                wrapper.write_text(
                    "#!"
                    + sys.executable
                    + '\nimport os,sys\na=sys.argv[1:]\nif a and a[0]=="exec" and "--help" not in a: a=a[:-1]+'
                    + repr(extra)
                    + "+a[-1:]\nos.execv("
                    + repr(os.environ["CODEXFARM_TEST_NATIVE_CODEX"])
                    + ", ["
                    + repr(os.environ["CODEXFARM_TEST_NATIVE_CODEX"])
                    + "]+a)\n"
                )
                wrapper.chmod(0o700)
                settings = ResourceSettings(investigator="codex", investigator_binary=str(wrapper))
                from codex_looper.resource_reports import model_projection

                prompt = model_projection(
                    {
                        "memory": {
                            "total_mib": 65536,
                            "available_mib": 1000,
                            "swap_used_mib": 20000,
                            "pressure_percent": 25,
                            "io_pressure_percent": 10,
                        },
                        "jobs": [
                            {
                                "job_id": f"{i:032x}",
                                "payload_pid": 1000 + i,
                                "role": "batch",
                                "status": "running",
                                "workers": 4096,
                                "worker_env": "CMAKE_BUILD_PARALLEL_LEVEL",
                                "restartable": True,
                                "restart_count": 0,
                            }
                            for i in range(20)
                        ],
                        "consumers": [
                            {
                                "pid": 1000 + i,
                                "uid": 1000,
                                "label": "untrusted" * 12,
                                "rss_mib": 1024,
                                "pss_mib": 512,
                                "growth_mib": 100,
                            }
                            for i in range(20)
                        ],
                    }
                )
                self.assertGreater(len(prompt.encode()), 2000)
                self.assertLessEqual(len(prompt.encode()), 2500)
                normalize = investigator.normalized_catalog

                def catalog(raw, model):
                    if kind == "positive":
                        return {
                            "models": [
                                next(
                                    item
                                    for item in json.loads(raw)["models"]
                                    if item["slug"] == model
                                )
                            ]
                        }
                    return normalize(raw, model)

                with (
                    patch.object(investigator, "normalized_catalog", side_effect=catalog),
                    patch.dict(os.environ, {"HOME": str(origin), "CODEX_HOME": str(origin)}),
                ):
                    answer = investigator.investigate(
                        prompt, settings, auth_path=auth, temporary_parent=root
                    )
                self.assertEqual(
                    json.loads(answer),
                    {"diagnosis": "fixture", "evidence": [], "proposed_fixes": [], "actions": []},
                )
                self.assertTrue(requests)
                if kind != "positive":
                    self.assertLessEqual(len(json.dumps(requests[0]).encode()), 16384)
                text = json.dumps(requests)
                self.assertNotIn("ORIGINAL_CONTEXT_SENTINEL", text)
                self.assertNotIn("ORIGINAL_SKILL_SENTINEL", text)
                self.assertFalse(sentinel.exists())
                names = tool_names(requests[0])
                if kind != "positive":
                    self.assertEqual(names, ["functions.request_user_input"])
                outputs = [
                    item.get("output")
                    for req in requests
                    for item in req.get("input", [])
                    if item.get("type") in ("function_call_output", "custom_tool_call_output")
                ]
                if kind == "input":
                    self.assertIn("unavailable in Default mode", json.dumps(outputs))
                elif kind not in ("final", "positive"):
                    self.assertIn("unsupported", json.dumps(outputs).lower())
                elif kind == "positive":
                    self.assertIn("collaboration.list_agents", names)
                    self.assertNotIn("unsupported", json.dumps(outputs).lower())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
