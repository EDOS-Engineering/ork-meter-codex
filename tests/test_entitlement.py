"""Ork Meter Plus gate (ork_codex/entitlement.py, ork-meter #265)."""
import calendar
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import plus_fixture
from ork_codex import entitlement
from ork_codex.policy import Policy

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks/post_tool_use.py"
NOW = calendar.timegm(time.strptime("2026-10-14T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
BASE = {"optimize": True, "state": "trial", "until": "2026-10-21T12:00:00Z", "checked_at": "2026-10-14T12:00:00Z"}


def at(text):
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


class Status(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name

    def put(self, answer):
        return plus_fixture.write(self.home, answer)

    def test_plus_states_and_offline_grace(self):
        self.assertEqual(entitlement.status(self.home, NOW), entitlement.NOT_CONNECTED)
        for state in ("promo", "trial", "active", "past_due"):
            self.put(dict(BASE, state=state))
            self.assertEqual(entitlement.status(self.home, NOW), entitlement.ENTITLED, state)
        self.put(dict(BASE, optimize=False, state="free"))
        self.assertEqual(entitlement.status(self.home, NOW), entitlement.NOT_ENTITLED, "free is visibility only")
        self.put(BASE)
        self.assertTrue(entitlement.entitled(self.home, at("2026-10-22T12:00:00Z")), "within 24 h past until")
        self.assertFalse(entitlement.entitled(self.home, at("2026-10-22T12:00:01Z")), "then fail closed")
        self.put(dict(BASE, until=None))
        self.assertTrue(entitlement.entitled(self.home, at("2026-10-15T11:59:59Z")))
        self.assertFalse(entitlement.entitled(self.home, at("2026-10-15T12:00:01Z")), "null until: bounded by checked_at")

    def test_only_the_exact_contract_file_counts(self):
        raw = json.dumps(BASE)
        for body in (dict(BASE, state="free"), dict(BASE, state="expired"), dict(BASE, optimize="true"),
                     dict(BASE, until="soon"), dict(BASE, checked_at=None), dict(BASE, extra=1),
                     {k: v for k, v in BASE.items() if k != "until"}, [], "not json",
                     raw[:-1] + ', "state": "active"}', " " * 2000):
            self.put(body)
            self.assertEqual(entitlement.status(self.home, NOW), entitlement.NOT_ENTITLED, body)
        if hasattr(os, "getuid"):
            os.chmod(self.put(BASE), 0o644)
            self.assertEqual(entitlement.status(self.home, NOW), entitlement.NOT_ENTITLED, "not owner-only")

    def test_recovery_does_not_upsell_an_unverified_account(self):
        self.assertEqual(entitlement.message(self.home, NOW), entitlement.CONNECT_MESSAGE)
        plus_fixture.write(self.home, None, reference="public-ref")
        self.assertEqual(entitlement.message(self.home, NOW), entitlement.UNVERIFIED_MESSAGE)
        self.put("broken cache")
        self.assertEqual(entitlement.message(self.home, NOW), entitlement.UNVERIFIED_MESSAGE)
        self.put(BASE)
        self.assertIsNone(entitlement.message(self.home, NOW))
        self.assertEqual(entitlement.message(self.home, at("2026-10-23T00:00:00Z")),
                         entitlement.UNVERIFIED_MESSAGE)
        self.put(dict(BASE, optimize=False, state="free"))
        self.assertEqual(entitlement.message(self.home, NOW), entitlement.MESSAGE)
        self.assertEqual(entitlement.message(self.home, NOW + 901), entitlement.UNVERIFIED_MESSAGE)
        self.assertEqual(entitlement.message(self.home, NOW - 301), entitlement.UNVERIFIED_MESSAGE)

    def test_gate_turns_levers_off_and_opt_outs_still_apply(self):
        policy = Policy()
        self.assertEqual(entitlement.gate(policy, self.home).mode, "off", "not connected")
        self.put(dict(BASE, until="2099-01-01T00:00:00Z"))
        self.assertIs(entitlement.gate(policy, self.home), policy)
        self.assertEqual(entitlement.gate(Policy(mode="off"), self.home).mode, "off", "opt-out on top")
        self.put(dict(BASE, optimize=False, state="free"))
        self.assertEqual(entitlement.gate(Policy(mode="shadow"), self.home).mode, "off")


class Refresh(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name

    def test_missing_pairing_clears_the_cache_without_the_helper(self):
        path = plus_fixture.write(self.home, BASE, reference=None)
        with patch("subprocess.Popen") as popen:
            self.assertFalse(entitlement.refresh(self.home))
        popen.assert_not_called()
        self.assertFalse(path.exists())

    def test_due_refresh_runs_the_helper_detached_at_most_every_15_minutes(self):
        path = plus_fixture.write(self.home, BASE, reference="public-ref")
        with patch("ork_codex.meter.binary", return_value="helper"), patch("subprocess.Popen") as popen:
            self.assertFalse(entitlement.refresh(self.home), "fresh cache: not due")
            popen.assert_not_called()
            old = time.time() - 16 * 60
            os.utime(path, (old, old))
            self.assertTrue(entitlement.refresh(self.home))
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["helper", "pair", "entitlement", "--reference", "public-ref", "--cache", str(path)])
        self.assertIs(popen.call_args.kwargs["stdout"], subprocess.DEVNULL)

    def test_foreground_refresh_waits_and_missing_helper_is_quiet(self):
        plus_fixture.write(self.home, None, reference="public-ref")
        with patch("ork_codex.meter.binary", return_value="helper"), patch("subprocess.run") as run:
            self.assertTrue(entitlement.refresh(self.home, background=False))
        self.assertEqual(run.call_args.args[0][:3], ["helper", "pair", "entitlement"])
        with patch("ork_codex.meter.binary", side_effect=ValueError("unavailable")):
            self.assertFalse(entitlement.refresh(self.home))


class Hook(unittest.TestCase):
    def run_hook(self, answer):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as data:
            plus_fixture.write(home, answer)
            original = "".join(f"test case_{i} ... ok\n" for i in range(900)) + "FAIL: critical\n"
            event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                     "tool_input": {"command": "pytest -v"}, "tool_response": original}
            env = {k: v for k, v in os.environ.items() if not k.startswith("ORK_CODEX_")}
            env.update(CODEX_HOME=home, PLUGIN_DATA=data, ORK_METER_BINARY=str(Path(home) / "absent"))
            result = subprocess.run([sys.executable, "-I", "-B", str(HOOK)], input=json.dumps(event),
                                    capture_output=True, text=True, env=env, timeout=60)
            return result.stdout

    def test_without_plus_native_output_passes_through(self):
        self.assertIn('"decision": "block"', self.run_hook(dict(BASE, until="2099-01-01T00:00:00Z")))
        self.assertEqual(self.run_hook(dict(BASE, optimize=False, state="free")), "")
        self.assertEqual(self.run_hook(None), "", "not connected")


class PairingIsolation(unittest.TestCase):
    setUp = Status.setUp
    put = Status.put
    def test_orphan_answer_cannot_enable_savings(self):
        self.put(dict(BASE, until="2099-01-01T00:00:00Z"))
        (entitlement.base(self.home) / "pairing.json").unlink()
        self.assertEqual(entitlement.status(self.home, NOW), entitlement.NOT_CONNECTED)

    def test_reconnecting_does_not_reuse_or_modify_previous_answer(self):
        old = plus_fixture.write(self.home, dict(BASE, until="2099-01-01T00:00:00Z"), reference="old-ref")
        plus_fixture.write(self.home, None, reference="new-ref")
        self.assertFalse(entitlement.entitled(self.home, NOW))
        old.write_text(json.dumps(dict(BASE, until="2099-01-01T00:00:00Z")))
        self.assertFalse(entitlement.entitled(self.home, NOW))

    def test_late_answer_after_disconnect_cannot_reenable_real_hook(self):
        from ork_codex import meter
        old = plus_fixture.write(self.home, dict(BASE, until="2099-01-01T00:00:00Z"), reference="old-ref")
        with patch.dict(os.environ, {"CODEX_HOME": self.home}), patch.object(meter, "binary", return_value="helper"), patch("subprocess.call", return_value=0):
            self.assertEqual(meter.command("forget", "old-ref", confirm=True), 0)
        self.assertIsNone(entitlement.saved_reference(self.home))
        old.write_text(json.dumps(dict(BASE, until="2099-01-01T00:00:00Z")))
        self.assertEqual(entitlement.status(self.home, NOW), entitlement.NOT_CONNECTED)
        original = "".join(f"test case_{i} ... ok\n" for i in range(900)) + "FAIL: critical\n"
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "pytest -v"}, "tool_response": original}
        env = dict(os.environ, CODEX_HOME=self.home, ORK_METER_BINARY=str(Path(self.home) / "absent"))
        result = subprocess.run([sys.executable, "-I", "-B", str(HOOK)], input=json.dumps(event), capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "", "disconnected hook must leave native output untouched")

    def test_forgetting_another_reference_preserves_current_pairing(self):
        from ork_codex import meter
        cache = plus_fixture.write(self.home, dict(BASE, until="2099-01-01T00:00:00Z"), reference="current-ref")
        before = cache.read_bytes()
        with patch.dict(os.environ, {"CODEX_HOME": self.home}), patch.object(meter, "binary", return_value="helper"), patch("subprocess.call", return_value=0):
            self.assertEqual(meter.command("forget", "other-ref", confirm=True), 0)
        self.assertEqual(entitlement.saved_reference(self.home), "current-ref")
        self.assertEqual(cache.read_bytes(), before)
        self.assertTrue(entitlement.entitled(self.home, NOW))

    def test_failed_forget_and_active_setup_preserve_pairing(self):
        from ork_codex import meter
        cache = plus_fixture.write(self.home, BASE, reference="current-ref")
        with patch.dict(os.environ, {"CODEX_HOME": self.home}), patch.object(meter, "binary", return_value="helper"), patch("subprocess.call", return_value=1) as call:
            self.assertEqual(meter.command("forget", "current-ref", confirm=True), 1)
            self.assertTrue(cache.exists())
            self.assertEqual(entitlement.saved_reference(self.home), "current-ref")
            lock = entitlement.base(self.home) / "setup.lock"
            lock.touch()
            with self.assertRaisesRegex(ValueError, "Exit the active"):
                meter.command("forget", "current-ref", confirm=True)
            self.assertEqual(call.call_count, 1)
            self.assertTrue(lock.exists())
            self.assertTrue(cache.exists())

    def test_failed_attempt_marker_avoids_spawning_another_helper(self):
        cache = plus_fixture.write(self.home, None, reference="current-ref")
        marker = cache.with_name("entitlement-attempt")
        marker.touch(mode=0o600)
        with patch("subprocess.Popen") as popen:
            self.assertFalse(entitlement.refresh(self.home))
        popen.assert_not_called()
        self.assertFalse(entitlement.entitled(self.home, NOW))
        self.assertTrue(entitlement.due(self.home, marker.stat().st_mtime + 15 * 60))

    def test_legacy_unbound_answer_requires_refresh_without_new_pairing(self):
        plus_fixture.write(self.home, None, reference="current-ref")
        legacy = entitlement.base(self.home) / "entitlement.json"
        legacy.write_text(json.dumps(BASE))
        legacy.chmod(0o600)
        self.assertFalse(entitlement.entitled(self.home, NOW))
        with patch("ork_codex.meter.binary", return_value="helper"), patch("subprocess.Popen") as popen:
            self.assertTrue(entitlement.refresh(self.home))
        self.assertEqual(popen.call_args.args[0][-1], str(entitlement.path(self.home)))
        self.assertEqual(entitlement.saved_reference(self.home), "current-ref")


if __name__ == "__main__":
    unittest.main()
