"""Integrated tracking privacy, native snapshot and delivery regressions."""
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote_plus

from ork_codex import tracking as t

REF = "a" * 32


def record():
    return {"id": REF, "origin": t.ORIGINS[True], "source": "openai", "client": "codex",
            "created_at": (t.now() - timedelta(minutes=1)).isoformat(),
            "device_secret": "omp_" + "a" * 43, "token": "omc_" + "b" * 43,
            "acknowledged_until": (t.now() + timedelta(days=1)).isoformat()}


def event():
    values = {k: i + 1 for i, k in enumerate(t.COUNTERS)}
    values["prompt"] = "PRIVATE_PROMPT"
    return {"type": "event_msg", "timestamp": t.now().isoformat(), "payload": {
        "type": "token_count", "info": {"total_token_usage": values, "last_token_usage": values,
                                          "private": "PRIVATE_METADATA"}}}


class Tracking(unittest.TestCase):
    def test_public_proposal_excludes_secrets_and_roundtrips_fragment(self):
        r = record()
        p = t.public(r)
        proposal = json.loads(unquote_plus(p["approval_url"].split("#proposal=")[1]))
        self.assertEqual(list(proposal), ["id", "source", "client", "device_sha256", "token_sha256"])
        self.assertEqual(proposal["client"], "codex")
        for secret in (r["device_secret"], r["token"]):
            self.assertNotIn(secret, json.dumps(p))
        changed = t.public({**r, "origin": t.ORIGINS[False]})
        self.assertNotEqual(p["fingerprint"], changed["fingerprint"])

    def test_prepare_requires_durable_os_storage_and_never_returns_secrets(self):
        backend = Mock()
        stored = {}
        backend.get_password.side_effect = lambda service, ref: stored.get(ref)
        backend.set_password.side_effect = lambda service, ref, raw: stored.update({ref: raw})
        with patch.object(t, "vault", return_value=backend):
            public = t.prepare(True)
        self.assertTrue(public["approval_url"].startswith(t.ORIGINS[True]))
        self.assertEqual(len(stored), 1)
        self.assertNotIn("omc_", json.dumps(public))
        backend.set_password.side_effect = RuntimeError("PRIVATE_KEYRING_ERROR")
        with patch.object(t, "vault", return_value=backend), self.assertRaisesRegex(ValueError, "OS credential store"):
            t.prepare(True)

    def test_load_rejects_wrong_origin_scope_and_corrupt_secrets(self):
        for mutation in ({"origin": "https://evil.test"}, {"client": "chatgpt"}, {"source": "claude"}, {"token": "bad"}):
            backend = Mock()
            backend.get_password.return_value = json.dumps({**record(), **mutation})
            with patch.object(t, "vault", return_value=backend), self.assertRaises(ValueError):
                t.load(REF)

    def test_snapshot_only_projects_counters_and_id_is_stable(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "PRIVATE_PATH.jsonl"
            path.write_text(json.dumps({"type": "response_item", "payload": "PRIVATE_TRANSCRIPT"}) + "\n" + json.dumps(event()) + "\n")
            observation = t.snapshot(path, REF)
            self.assertEqual(observation, t.snapshot(path, REF))
            self.assertNotEqual(observation["id"], t.snapshot(path, "b" * 32)["id"])
        self.assertEqual([s["kind"] for s in observation["samples"]], ["thread_total_snapshot", "thread_last_snapshot"])
        self.assertEqual(set(observation), {"id", "client", "captured_at", "samples"})
        self.assertNotIn("PRIVATE", json.dumps(observation))
        self.assertEqual(observation["samples"][0]["counters"]["input_token_count"], "1")

    def test_malformed_and_stale_counters_never_sent(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "usage.jsonl"
            for invalid in (-1, True, "3", 10**12 + 1, None):
                e = event()
                e["payload"]["info"]["total_token_usage"]["input_tokens"] = invalid
                path.write_text(json.dumps(e) + "\n")
                with self.assertRaises(ValueError):
                    t.snapshot(path, REF)
            e = event()
            e["timestamp"] = (t.now() - timedelta(days=2)).isoformat()
            path.write_text(json.dumps(e) + "\n")
            with self.assertRaisesRegex(ValueError, "stale"):
                t.snapshot(path, REF)

    def test_partial_tail_and_empty_info_do_not_erase_native_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "usage.jsonl"
            e = event()
            path.write_text(json.dumps(e) + '\n{"type":"event_msg","payload":{"type":"token_count","info":null}}\n{"partial":')
            self.assertEqual(t.snapshot(path, REF)["captured_at"], e["timestamp"])

    def test_session_lookup_refuses_paths_outside_profile(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            session = home / "sessions/2026/rollout-" / (REF + ".jsonl")
            session.parent.mkdir(parents=True)
            session.touch()
            self.assertEqual(t.session_path(home, REF), session.resolve())
            other = home / "private.jsonl"
            other.touch()
            with self.assertRaises(ValueError):
                t.session_path(home, transcript=other)
            with self.assertRaises(ValueError):
                t.session_path(home, "../../*")

    def test_sync_has_no_helper_and_duplicate_delivery_is_suppressed(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            path = home / "sessions" / (REF + ".jsonl")
            path.parent.mkdir()
            path.write_text(json.dumps(event()) + "\n")
            with patch.object(t, "load", return_value=record()), patch.object(t, "exchange", return_value={"created": True}) as send:
                first = t.sync(home, REF, REF)
                second = t.sync(home, REF, REF)
            self.assertTrue(first["sent"])
            self.assertTrue(second["already_delivered"])
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[1], "/v1/collectors/observations")
            self.assertNotIn("PRIVATE", json.dumps(send.call_args.args[2]))

    def test_failed_delivery_can_retry_same_identity(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sessions" / (REF + ".jsonl")
            path.parent.mkdir()
            path.write_text(json.dumps(event()) + "\n")
            with patch.object(t, "load", return_value=record()), patch.object(t, "exchange", side_effect=[ValueError("network"), {"created": False}]) as send:
                with self.assertRaises(ValueError):
                    t.sync(root, REF, REF)
                self.assertTrue(t.sync(root, REF, REF)["sent"])
                self.assertEqual(send.call_args_list[0].args[2], send.call_args_list[1].args[2])

    def test_automatic_delivery_kills_slow_worker_and_backs_off(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            command = [sys.executable, "-c", "import time;time.sleep(30)"]
            with patch.object(t, "delivery_command", return_value=command) as build, \
                    patch.object(t, "HOOK_DELIVERY_SECONDS", 0.1):
                started = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired):
                    t.automatic_sync(home, {})
                self.assertLess(time.monotonic() - started, 3)
                t.automatic_sync(home, {})
                self.assertEqual(build.call_count, 1)
            retry = json.loads((home / "ork-tracking/delivery-retry.json").read_text())
            self.assertEqual(retry["failures"], 1)
            self.assertGreater(t.timestamp(retry["after"]), t.now())

    def test_slow_http_retries_identical_snapshot_after_timeout(self):
        received = []
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                release.wait(5)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"created":false}')
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as root:
                home = Path(root)
                path = home / "sessions" / (REF + ".jsonl")
                path.parent.mkdir()
                path.write_text(json.dumps(event()) + "\n")
                synthetic = {**record(), "origin": "http://127.0.0.1:" + str(server.server_port)}
                source = ("import sys;sys.path.insert(0," + repr(str(Path(t.__file__).resolve().parent.parent)) + ");"
                          "from ork_codex import tracking as t;"
                          "t.ORIGINS[True]=" + repr(synthetic["origin"]) + ";"
                          "t.load=lambda ref:" + repr(synthetic) + ";"
                          "t.sync(" + repr(root) + "," + repr(REF) + "," + repr(REF) + ",refresh_entitlement=False)")
                with patch.object(t, "delivery_command", return_value=[sys.executable, "-I", "-c", source]):
                    with patch.object(t, "HOOK_DELIVERY_SECONDS", 0.5):
                        with self.assertRaises(subprocess.TimeoutExpired):
                            t.automatic_sync(home, {})
                    self.assertEqual(len(received), 1)
                    t.automatic_sync(home, {})
                    self.assertEqual(len(received), 1)
                    self.assertFalse(list((home / "ork-tracking").glob("sent-*.json")))
                    release.set()
                    t.write_json(home / "ork-tracking/delivery-retry.json", {
                        "failures": 1, "after": (t.now() - timedelta(seconds=1)).isoformat()})
                    t.automatic_sync(home, {})
                    self.assertEqual(received[0], received[1])
                    t.automatic_sync(home, {})
                    self.assertEqual(len(received), 2)
                    self.assertEqual(len(list((home / "ork-tracking").glob("sent-*.json"))), 1)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_automatic_delivery_serializes_and_recovers_after_backoff(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            state = t.private_directory(home / "ork-tracking")
            with t.delivery_lock(state) as acquired, patch.object(t.subprocess, "run") as run:
                self.assertTrue(acquired)
                t.automatic_sync(home, {})
                run.assert_not_called()
            t.write_json(state / "delivery-retry.json", {
                "failures": 2, "after": (t.now() - timedelta(seconds=1)).isoformat()})
            t.write_json(state / "last-error.json", {"delivery_verified": False})
            with patch.object(t.subprocess, "run") as run:
                t.automatic_sync(home, {"session_id": REF})
                run.assert_called_once()
            self.assertFalse((state / "delivery-retry.json").exists())
            self.assertFalse((state / "last-error.json").exists())

    def test_automatic_worker_does_not_spawn_entitlement_children(self):
        command = t.delivery_command(Path("/synthetic"), {"session_id": REF, "tool_input": "PRIVATE"})
        self.assertIn("refresh_entitlement=False", command[4])
        self.assertNotIn("PRIVATE", " ".join(command))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sessions" / (REF + ".jsonl")
            path.parent.mkdir()
            path.write_text(json.dumps(event()) + "\n")
            with patch.object(t, "load", return_value=record()), \
                    patch.object(t, "exchange", return_value={"created": True}), \
                    patch("ork_codex.entitlement.refresh") as refresh:
                t.sync(root, REF, REF, refresh_entitlement=False)
                refresh.assert_not_called()

    def test_claim_codes_cannot_enter_agent_tool_input(self):
        with patch.object(t.sys, "stdin", io.StringIO("SECRET")), patch.object(t, "exchange") as send:
            with self.assertRaisesRegex(ValueError, "own terminal"):
                t.claim(REF)
            send.assert_not_called()

    def test_authorization_must_bind_origin_reference_and_scope(self):
        r = record()
        auth = {"id": REF, "source": "openai", "client": "codex", "feed": REF,
                "audience": r["origin"], "scope": "usage:ingest", "expires_at": r["acknowledged_until"]}
        t.validate_authorization(r, auth)
        for mutation in ({"scope": "account:admin"}, {"audience": t.ORIGINS[False]}, {"feed": "b" * 32},
                         {"expires_at": (t.now() - timedelta(seconds=1)).isoformat()}):
            with self.assertRaises(ValueError):
                t.validate_authorization(r, {**auth, **mutation})

    def test_status_requires_hosted_receipt(self):
        r = record()
        auth = {"id": REF, "source": "openai", "client": "codex", "feed": REF,
                "audience": r["origin"], "scope": "usage:ingest", "expires_at": r["acknowledged_until"]}
        with patch.object(t, "load", return_value=r), patch.object(t, "exchange", return_value={"authorization": auth}):
            self.assertFalse(t.status(REF)["delivery_verified"])

    def test_redirects_refused_and_transport_errors_do_not_leak_credentials(self):
        self.assertIsNone(t.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.test"))
        with patch.object(t, "build_opener", side_effect=RuntimeError("SECRET")):
            with self.assertRaises(ValueError) as caught:
                t.exchange(record(), "/v1/collectors/observations", {})
        self.assertNotIn("SECRET", str(caught.exception))

    def test_ambiguous_json_and_private_timestamp_errors_are_refused(self):
        with self.assertRaises(ValueError):
            t.decode('{"scope":"usage:ingest","scope":"account:admin"}')
        with self.assertRaises(ValueError) as caught:
            t.timestamp("PRIVATE_SERVER_DIAGNOSTIC")
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_invalid_existing_hooks_are_left_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "hooks.json"
            for document in ({"hooks": []}, {"hooks": {"Stop": "invalid"}}, {"hooks": {"PostToolUse": [{"hooks": [False]}]}}):
                path.write_text(json.dumps(document))
                before = path.read_bytes()
                with patch.object(t, "load", return_value=record()), self.assertRaises(ValueError):
                    t.install(root, REF)
                self.assertEqual(path.read_bytes(), before)

    def test_install_preserves_unrelated_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            other = {"hooks": [{"type": "command", "command": "existing", "statusMessage": "other"}]}
            (home / "hooks.json").write_text(json.dumps({"hooks": {"PostToolUse": [other]}, "extra": True}))
            with patch.object(t, "load", return_value=record()), patch("ork_codex.entitlement.refresh"):
                t.install(home, REF)
                first = (home / "hooks.json").read_bytes()
                t.install(home, REF)
            self.assertEqual(first, (home / "hooks.json").read_bytes())
            config = json.loads(first)
            self.assertEqual(config["hooks"]["PostToolUse"][0], other)
            self.assertTrue(config["extra"])
            self.assertEqual(len(config["hooks"]["Stop"]), 1)
            self.assertNotIn("trusted_hash", first.decode())
            self.assertNotIn("omc_", first.decode())


if __name__ == "__main__":
    unittest.main()
