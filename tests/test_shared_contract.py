"""Run the same pinned corpus against the Codex adapter, without inference."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = "codex"


class SharedContract(unittest.TestCase):
    def test_production_adapter_passes_every_pinned_case(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "contracts/check.py"),
                                 "--adapter", ADAPTER, "--root", str(ROOT)],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(report["qualified"])
        self.assertGreaterEqual(report["tests_run"], 26)
        self.assertEqual(report["tests_run"], len(report["cases"]))
        self.assertEqual(report["inference"], "none")

    def test_changed_shared_runtime_copy_cannot_qualify(self):
        with tempfile.TemporaryDirectory(prefix="ork-contract-pin-") as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "contracts", root / "contracts")
            manifest = json.loads((root / "contracts/v1/manifest.json").read_bytes())
            module = root / manifest["shared_modules"]["evidence_formats"][ADAPTER]
            module.parent.mkdir(parents=True)
            module.write_text("# a different runtime\n", encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", "-B", str(root / "contracts/check.py"),
                                     "--adapter", ADAPTER, "--root", str(root)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("format guard differs from contract pin", result.stderr)


if __name__ == "__main__":
    unittest.main()
