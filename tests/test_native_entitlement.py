"""Native WSL pairing enables reducers without a separately paired helper."""
from email.message import Message
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from ork_codex import entitlement, tracking
from ork_codex.storage import private_directory


class NativeEntitlement(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ref = "a" * 32
        self.connection = self.home / "ork-tracking/connection.json"
        private_directory(self.connection.parent)
        self.connection.write_text(json.dumps({"reference": self.ref}))
        self.answer = {"optimize": True, "state": "promo", "until": None,
                       "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def response(self, body):
        response = io.BytesIO(body)
        response.status = 200
        response.headers = Message()
        response.headers["Content-Type"] = "application/json"
        return response

    def refresh(self, body=None, side_effect=None):
        record = {"origin": "https://orkmeter.com", "token": "test-secret",
                  "acknowledged_until": "2099-01-01T00:00:00Z"}
        with patch.object(tracking, "load", return_value=record), patch.object(entitlement, "build_opener") as opener:
            opener.return_value.open.side_effect = side_effect
            opener.return_value.open.return_value = self.response(body if body is not None else json.dumps(self.answer).encode())
            result = entitlement.refresh(self.home, background=False)
            return result, opener

    def cached(self):
        cache = entitlement.path(self.home)
        private_directory(cache.parent)
        cache.write_text(json.dumps(self.answer))
        cache.chmod(0o600)
        old = time.time() - 1000
        os.utime(cache, (old, old))
        return cache

    def test_native_pairing_gets_real_contract_without_helper(self):
        with patch("ork_codex.meter.binary", side_effect=AssertionError("must not use helper")):
            accepted, opener = self.refresh()
        self.assertTrue(accepted)
        self.assertTrue(entitlement.entitled(self.home))
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://orkmeter.com/v1/entitlement")
        self.assertEqual(request.get_header("Authorization"), "Collector test-secret")
        self.assertIsNone(request.data)
        if os.name != "nt":
            self.assertEqual(entitlement.path(self.home).stat().st_mode & 0o777, 0o600)

    def test_native_reconnect_does_not_use_helper_or_previous_cache(self):
        self.cached()
        legacy = entitlement.base(self.home) / "pairing.json"
        legacy.parent.mkdir(exist_ok=True)
        legacy.write_text('{"reference":"older-helper"}')
        self.connection.write_text(json.dumps({"reference": "b" * 32}))
        self.assertEqual(entitlement.saved_reference(self.home), "b" * 32)
        self.assertFalse(entitlement.entitled(self.home))
        self.connection.write_text('{"reference":"malformed"}')
        self.assertIsNone(entitlement.saved_reference(self.home))

    def test_fresh_answer_and_failed_attempt_are_rate_limited(self):
        self.assertTrue(self.refresh()[0])
        accepted, opener = self.refresh()
        self.assertFalse(accepted)
        opener.assert_not_called()
        entitlement.path(self.home).unlink()
        accepted, opener = self.refresh()
        self.assertFalse(accepted)
        opener.assert_not_called()

    def test_network_failure_keeps_cached_answer(self):
        cache = self.cached()
        before = cache.read_bytes()
        self.assertFalse(self.refresh(side_effect=OSError("offline"))[0])
        self.assertEqual(cache.read_bytes(), before)
        self.assertTrue(entitlement.entitled(self.home))

    def test_denied_credential_removes_cache(self):
        cache = self.cached()
        self.assertFalse(self.refresh(side_effect=HTTPError("https://orkmeter.com", 401, "denied", {}, None))[0])
        self.assertFalse(cache.exists())
        self.assertFalse(entitlement.entitled(self.home))

    def test_malformed_or_future_answers_never_replace_previous_answer(self):
        for body in (b'{"optimize":true}', b'x' * 1025,
                     json.dumps(dict(self.answer, checked_at="2099-01-01T00:00:00Z")).encode()):
            with self.subTest(body=body[:40]):
                cache = self.cached()
                cache.with_name("entitlement-attempt").unlink(missing_ok=True)
                before = cache.read_bytes()
                self.assertFalse(self.refresh(body=body)[0])
                self.assertEqual(cache.read_bytes(), before)

    def test_reconnect_during_request_cannot_install_late_answer(self):
        def reconnect(*_args, **_kwargs):
            self.connection.write_text(json.dumps({"reference": "b" * 32}))
            return self.response(json.dumps(self.answer).encode())
        self.assertFalse(self.refresh(side_effect=reconnect)[0])
        self.assertFalse(entitlement.entitled(self.home))

    def test_terminated_refresh_releases_exclusion_and_next_attempt_recovers(self):
        # Exercise a real background process. It stops after taking the refresh
        # lock, before reading any credential or contacting the platform.
        ready = self.home / "refresh-ready"
        program = """
import sys, time
from pathlib import Path
from ork_codex import entitlement, tracking
def blocked_load(_reference):
    Path(sys.argv[2]).write_text("locked")
    time.sleep(60)
    raise OSError("terminated test expected")
tracking.load = blocked_load
entitlement.refresh(sys.argv[1], background=False)
"""
        child = subprocess.Popen([sys.executable, "-B", "-c", program,
                                  str(self.home), str(ready)],
                                 cwd=Path(__file__).resolve().parents[1],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "refresh did not reach its locked operation")
            accepted, opener = self.refresh()
            self.assertFalse(accepted, "concurrent refresh must not send a second request")
            opener.assert_not_called()
        finally:
            child.kill()
            child.communicate(timeout=10)
        accepted, _ = self.refresh()
        self.assertTrue(accepted, "terminated refresh must not permanently disable Plus refresh")
        self.assertTrue(entitlement.entitled(self.home))

    def test_orphaned_legacy_refresh_marker_does_not_block_refresh(self):
        cache = entitlement.path(self.home)
        private_directory(cache.parent)
        (cache.parent / "refresh.lock").write_bytes(b"")
        self.assertTrue(self.refresh()[0])
        self.assertTrue(entitlement.entitled(self.home))

    def test_background_refresh_uses_installed_python_without_credentials_in_argv(self):
        with patch.object(entitlement.subprocess, "Popen") as process:
            self.assertTrue(entitlement.refresh(self.home))
        argv = process.call_args.args[0]
        self.assertIn("refresh_native", argv[4])
        self.assertEqual(argv[-1], self.ref)
        self.assertNotIn("test-secret", repr(argv))

    def test_native_tracking_install_includes_automatic_reducer(self):
        with patch.object(tracking, "load", return_value={}), \
                patch("ork_codex.auto_hook.install", return_value={"status": "installed"}) as install, \
                patch.object(entitlement, "refresh", return_value=True) as refresh:
            result = tracking.install(self.home, self.ref)
        install.assert_called_once_with(self.home)
        refresh.assert_called_once_with(self.home, background=False)
        self.assertEqual(result["optimization"]["status"], "installed")


if __name__ == "__main__":
    unittest.main()
