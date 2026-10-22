import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import plus_fixture
from ork_codex import meter

ROOT = Path(__file__).resolve().parents[1]


class Bridge(unittest.TestCase):
    def test_prepare_uses_fixed_origin_and_native_client(self):
        with patch.object(meter, "binary", return_value="helper"), patch.object(subprocess, "call", return_value=0) as call:
            self.assertEqual(meter.command("prepare"), 0)
            self.assertEqual(call.call_args.args[0], ["helper", "pair", "prepare", "--source", "openai", "--client", "codex"])
            meter.command("prepare", dev=True)
            self.assertEqual(call.call_args.args[0][-1], "https://dev.ork-meter.edos.io")

    def test_capture_and_claim_keep_terminal_streams(self):
        with patch.object(meter, "binary", return_value="helper"), patch.object(subprocess, "call", return_value=0) as call:
            meter.command("capture", "public-ref")
            self.assertEqual(call.call_args.args[0], ["helper", "collect", "capture", "--client", "codex", "--reference", "public-ref"])
            self.assertFalse(call.call_args.kwargs)
            meter.command("claim", "public-ref")
            self.assertEqual(call.call_args.args[0], ["helper", "pair", "claim", "--reference", "public-ref"])

    def test_disconnect_requires_explicit_local_removal(self):
        with self.assertRaises(ValueError):
            meter.command("forget", "public-ref")
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"CODEX_HOME": home}), \
                patch.object(meter, "binary", return_value="helper"), patch.object(subprocess, "call", return_value=0) as call:
            cache = plus_fixture.write(home, reference="public-ref")
            meter.command("forget", "public-ref", confirm=True)
            self.assertEqual(call.call_args.args[0][-1], "--confirm-local-removal")
            self.assertFalse(cache.exists(), "not connected means not entitled")

    def test_invalid_reference_and_binary_refused(self):
        for ref in (None, "--evil", "x\n--flag", "../x"):
            # Leading hyphens are rejected to keep helper arguments unambiguous.
            with self.assertRaises(ValueError):
                meter.reference(ref)
        with patch.dict(os.environ, {"ORK_METER_BINARY": "relative-helper"}):
            with self.assertRaises(ValueError):
                meter.binary()

    def test_status_uses_hosted_receipt_and_retains_non_additive_semantics(self):
        receipt = {"client": "codex", "latest_export": {"captured_at": "2026-10-13T12:00:00Z", "received_at": "2026-10-13T12:00:01Z"}}
        result = subprocess.CompletedProcess([], 0, json.dumps(receipt).encode(), b"")
        with patch.object(meter, "binary", return_value="helper"), patch.object(subprocess, "run", return_value=result):
            self.assertEqual(meter.status("public-ref"), receipt)
        result.returncode = 1
        result.stderr = b"private backend details"
        with patch.object(meter, "binary", return_value="helper"), patch.object(subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "unverified"):
                meter.status("public-ref")


class NativeHelper(unittest.TestCase):
    def test_actual_helper_projects_only_native_counters(self):
        name = "ork-meter.exe" if os.name == "nt" else "ork-meter"
        helper = os.environ.get("ORK_TEST_METER_BINARY") or ROOT / ".local" / name
        if not Path(helper).is_file():
            self.skipTest("build the native development helper for this integration check")
        attributes = {"event.name": "codex.sse_event", "event.kind": "response.completed", "input_token_count": "17", "output_token_count": "5", "cached_token_count": "3", "reasoning_token_count": "2", "model": "PRIVATE_MODEL", "user.email": "PRIVATE_USER", "prompt": "PRIVATE_PROMPT"}
        raw = {"resourceLogs": [{"resource": {"attributes": [{"key": "path", "value": {"stringValue": "PRIVATE_PATH"}}]},
                                 "scopeLogs": [{"logRecords": [{"body": {"stringValue": "PRIVATE_BODY"},
                                                               "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attributes.items()]}]}]}]}
        result = subprocess.run([str(helper), "collect", "inspect-telemetry", "--client", "codex"], input=json.dumps(raw).encode(), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        projected = json.loads(result.stdout)
        self.assertFalse(projected["additive"])
        self.assertFalse(projected["ledger_eligible"])
        self.assertEqual(projected["samples"][0]["counters"], {"input_token_count": "17", "output_token_count": "5", "cached_token_count": "3", "reasoning_token_count": "2"})
        self.assertNotIn(b"PRIVATE_", result.stdout)


if __name__ == "__main__":
    unittest.main()
