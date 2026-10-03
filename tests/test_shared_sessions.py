from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import socket
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "shared_sessions", REPO_ROOT / "bin/codex-shared-sessions.py"
)
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
ROOT_ID = "123e4567-e89b-42d3-a456-426614174000"
SECOND_ID = "123e4567-e89b-42d3-a456-426614174001"
CHILD_ID = "123e4567-e89b-42d3-a456-426614174002"


class FakeSocket:
    def __init__(self):
        self.incoming = b""
        self.calls = []
        self.closed = False
        self.loaded = [{ROOT_ID, SECOND_ID}, {ROOT_ID, SECOND_ID}]
        self.threads = {
            value: {"id": value, "cwd": "/tmp/project", "ephemeral": False, "source": "cli"}
            for value in (ROOT_ID, SECOND_ID)
        }
        self.fragmented = False
        self.peer_uid = os.getuid()

    def settimeout(self, _timeout):
        pass

    def connect(self, _endpoint):
        pass

    def getsockopt(self, *_args):
        return struct.pack("3i", os.getpid(), self.peer_uid, os.getgid())

    def recv(self, count):
        result, self.incoming = self.incoming[:count], self.incoming[count:]
        return result

    def close(self):
        self.closed = True

    @staticmethod
    def frame(data, opcode=1, final=True):
        flags = opcode | (128 if final else 0)
        if len(data) < 126:
            return bytes([flags, len(data)]) + data
        return bytes([flags, 126]) + struct.pack("!H", len(data)) + data

    def sendall(self, data):
        if data.startswith(b"GET "):
            key = next(
                line.split(b": ", 1)[1]
                for line in data.split(b"\r\n")
                if line.startswith(b"Sec-WebSocket-Key:")
            )
            accept = base64.b64encode(
                hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
            )
            self.incoming += (
                b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            return
        if data[0] & 15 == 10:
            return
        size, offset = data[1] & 127, 2
        if size == 126:
            size, offset = struct.unpack("!H", data[2:4])[0], 4
        mask = data[offset : offset + 4]
        payload = data[offset + 4 : offset + 4 + size]
        value = json.loads(bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))
        self.calls.append(value)
        method = value["method"]
        if method == "initialized":
            return
        if method == "initialize":
            result = {"userAgent": "mock"}
        elif method == "thread/loaded/list":
            result = {"data": sorted(self.loaded.pop(0)), "nextCursor": None}
        elif method == "thread/read":
            result = {"thread": self.threads[value["params"]["threadId"]]}
        else:
            raise AssertionError(f"unexpected mutating request: {method}")
        body = json.dumps({"id": value["id"], "result": result}).encode()
        if self.fragmented:
            self.incoming += (
                self.frame(body[:10], final=False)
                + self.frame(b"ping", opcode=9)
                + self.frame(body[10:], opcode=0)
            )
        else:
            self.incoming += self.frame(body)


class SharedSessionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "server.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        self.addCleanup(listener.close)
        self.fake = FakeSocket()
        self.patches = [
            patch.object(shared.socket, "socket", return_value=self.fake),
            patch.object(shared, "server_process", return_value=True),
        ]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)

    def test_snapshot_covers_every_loaded_conversation_without_turns_or_mutations(self):
        rows = shared.snapshot(str(self.path), None)
        self.assertEqual({row[3] for row in rows}, {f"resume {ROOT_ID}", f"resume {SECOND_ID}"})
        self.assertEqual(len(rows), 2)
        self.assertTrue(self.fake.closed)
        for call in self.fake.calls:
            self.assertIn(
                call["method"], {"initialize", "initialized", "thread/loaded/list", "thread/read"}
            )
            if call["method"] == "thread/read":
                self.assertIs(call["params"]["includeTurns"], False)

    def test_snapshot_preserves_saved_logical_names_and_uses_current_server_directory(self):
        manifest = self.root / "manifest.tsv"
        manifest.write_text(f"name\tdir\tcmd\targs\nproject\t/tmp/old\tcodex\tresume {ROOT_ID}\n")
        rows = shared.snapshot(str(self.path), manifest)
        row = next(row for row in rows if row[3] == f"resume {ROOT_ID}")
        self.assertEqual(row[:2], ("project", "/tmp/project"))

    def test_new_live_conversation_during_capture_fails_closed(self):
        self.fake.loaded[-1].add(CHILD_ID)
        with self.assertRaisesRegex(ValueError, "changed during capture"):
            shared.snapshot(str(self.path), None)
        self.assertTrue(self.fake.closed)

    def test_child_thread_is_covered_by_its_loaded_root_and_fork_remains_independent(self):
        for page in self.fake.loaded:
            page.add(CHILD_ID)
        self.fake.threads[CHILD_ID] = {
            "id": CHILD_ID,
            "source": {"subAgent": {"threadSpawn": {}}},
            "sessionId": ROOT_ID,
        }
        self.fake.threads[SECOND_ID]["parentThreadId"] = ROOT_ID
        rows = shared.snapshot(str(self.path), None)
        self.assertEqual({row[3] for row in rows}, {f"resume {ROOT_ID}", f"resume {SECOND_ID}"})

    def test_ephemeral_or_unrestorable_conversation_prevents_verified_inventory(self):
        self.fake.threads[ROOT_ID]["ephemeral"] = True
        with self.assertRaisesRegex(ValueError, "durable restore"):
            shared.snapshot(str(self.path), None)

    def test_other_user_or_non_server_peer_is_rejected_before_requests(self):
        self.fake.peer_uid = os.getuid() + 1
        with self.assertRaisesRegex(ValueError, "socket peer"):
            shared.snapshot(str(self.path), None)
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(self.fake.closed)

    def test_fragmented_metadata_and_ping_frames_are_supported(self):
        self.fake.fragmented = True
        self.assertEqual(len(shared.snapshot(str(self.path), None)), 2)

    def test_protocol_client_rejects_mutating_methods(self):
        client = shared.Client(str(self.path))
        with self.assertRaisesRegex(ValueError, "unsupported recovery request"):
            client.request("turn/start", {})
        client.close()

    def test_socket_endpoint_requires_reciprocal_peers_and_a_verified_server_process(self):
        self.patches[1].stop()
        proc = self.root / "proc" / "202"
        proc.mkdir(parents=True)
        (proc / "cmdline").write_bytes(b"/usr/bin/codex\0--profile\0work\0app-server\0")
        lines = (
            'u_str ESTAB 0 0 * 111 * 222 users:(("codex",pid=101,fd=37))\n'
            + f'u_str ESTAB 0 0 {self.path} 222 * 111 users:(("codex",pid=202,fd=40))\n'
        )
        result = subprocess.CompletedProcess([], 0, lines, "")
        with (
            patch.dict(os.environ, {"CODEX_PROC_ROOT": str(proc.parent)}),
            patch.object(shared.subprocess, "run", return_value=result),
        ):
            self.assertEqual(shared.shared_endpoint(101), str(self.path))
            (proc / "cmdline").write_bytes(b"/usr/bin/codex\0resume\0")
            with self.assertRaisesRegex(ValueError, "no unique verified"):
                shared.shared_endpoint(101)
