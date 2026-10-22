import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"scripts"))
from model_policy import require_model
from native_codex import Client


class Models(unittest.TestCase):
    def test_exact_pins_only(self):
        for host, model in (("codex", "gpt-6.1-sol"), ("claude", "claude-opus-5-5")):
            self.assertEqual(require_model(host, model), model)
        for host, model in (("codex", "gpt-5.4"), ("codex", "gpt-6-sol"), ("codex", None),
                            ("claude", "claude-sonnet-4-6"), ("claude", "opus"), ("claude", "fixture")):
            with self.assertRaisesRegex(ValueError, "no substitutions"):
                require_model(host, model)

    def test_native_requests_are_rejected_before_transmission(self):
        client = Client.__new__(Client)
        sink = io.StringIO()
        client.process = SimpleNamespace(stdin=sink)
        for method in ("thread/start", "thread/resume", "thread/fork", "turn/start"):
            with self.assertRaises(ValueError):
                client.send({"method": method, "params": {"model": "gpt-5.4"}})
        with self.assertRaises(ValueError):
            client.send({"method": "thread/start", "params": {}})
        self.assertEqual(sink.getvalue(), "")
        client.send({"method": "thread/start", "params": {"model": "gpt-6.1-sol"}})
        self.assertIn("gpt-6.1-sol", sink.getvalue())


if __name__ == "__main__":
    unittest.main()
