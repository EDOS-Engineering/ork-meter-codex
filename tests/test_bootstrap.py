"""The installer pins one release and installs only a wheel whose digest matches it."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "scripts/ork_codex.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class Server:
    def __init__(self, files):
        self.files = files
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                body = outer.files.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ReleaseFile(unittest.TestCase):
    def test_release_json_pins_a_wheel_on_orkmeter_com(self):
        data = bootstrap.release()
        self.assertEqual(data["base"], "https://orkmeter.com/releases/codex/")
        self.assertEqual(data["wheel"], f"ork_meter_codex-{data['version']}-py3-none-any.whl")
        self.assertRegex(data["sha256"], r"^[0-9a-f]{64}$")
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], data["version"])


class Install(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "codex"
        self.wheel = b"PK synthetic wheel bytes"
        self.data = {"version": "9.9.9", "wheel": "ork_meter_codex-9.9.9-py3-none-any.whl",
                     "sha256": hashlib.sha256(self.wheel).hexdigest(), "base": "https://orkmeter.com/releases/codex/"}
        self.server = Server({"/9.9.9/ork_meter_codex-9.9.9-py3-none-any.whl": self.wheel})
        self.addCleanup(self.server.close)
        self.addCleanup(self.temp.cleanup)

    def environment(self, base=None):
        return patch.dict(os.environ, {"CODEX_HOME": str(self.home), "ORK_CODEX_RELEASE_BASE": base or self.server.url})

    def test_digest_mismatch_installs_nothing(self):
        with self.environment(), self.assertRaisesRegex(ValueError, "SHA-256"):
            bootstrap.install(dict(self.data, sha256="0" * 64), log=lambda *a: None)
        self.assertFalse((self.home / "ork-meter-codex" / "package" / "9.9.9" / "installed.json").exists())
        self.assertFalse(list((self.home / "ork-meter-codex" / "package" / "9.9.9").glob(".wheel-*")) if (self.home / "ork-meter-codex" / "package" / "9.9.9").exists() else [])

    def test_plain_http_is_refused_unless_loopback(self):
        with self.environment(base="http://example.invalid"), self.assertRaisesRegex(ValueError, "not HTTPS"):
            bootstrap.install(self.data, log=lambda *a: None)
        self.assertEqual(self.server.requests, [])

    def test_installed_record_is_trusted_only_for_the_pinned_digest(self):
        with self.environment():
            directory = self.home / "ork-meter-codex" / "package" / "9.9.9"
            command = bootstrap.command_in(directory / "venv")
            command.parent.mkdir(parents=True)
            command.write_text("#!/bin/sh\n")
            (directory / "installed.json").write_text(json.dumps({"wheel": self.data["wheel"], "sha256": self.data["sha256"]}))
            self.assertEqual(bootstrap.installed(self.data), command)
            self.assertIsNone(bootstrap.installed(dict(self.data, sha256="1" * 64)))
            command.unlink()
            self.assertIsNone(bootstrap.installed(self.data))

    def test_verified_wheel_is_installed_into_a_private_environment(self):
        calls = []

        def fake_run(command, capture_output, text):
            calls.append(command)
            bootstrap.command_in(Path(command[0]).parents[1]).parent.mkdir(parents=True, exist_ok=True)
            bootstrap.command_in(Path(command[0]).parents[1]).write_text("#!/bin/sh\n")
            return type("R", (), {"returncode": 0, "stderr": ""})()

        with self.environment(), patch.object(bootstrap.venv.EnvBuilder, "create", lambda self, path: Path(path).mkdir(parents=True)), \
             patch.object(bootstrap.subprocess, "run", fake_run):
            command = bootstrap.install(self.data, log=lambda *a: None)
        self.assertTrue(command.is_file())
        self.assertEqual(len(calls), 1)
        self.assertIn("--only-binary=:all:", calls[0])
        self.assertTrue(calls[0][-1].endswith(self.data["wheel"]))
        record = json.loads((self.home / "ork-meter-codex" / "package" / "9.9.9" / "installed.json").read_text())
        self.assertEqual(record["sha256"], self.data["sha256"])
        self.assertFalse((self.home / "ork-meter-codex" / "package" / "9.9.9" / self.data["wheel"]).exists(), "the wheel is not kept")
        self.assertEqual(self.server.requests, ["/9.9.9/ork_meter_codex-9.9.9-py3-none-any.whl"])
        with self.environment():
            self.assertEqual(bootstrap.installed(self.data), command)


if __name__ == "__main__":
    unittest.main()
