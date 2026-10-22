"""scripts/package_release.py ships helpers whose digests the manifest records, or nothing.

Needs a Go toolchain on PATH; the build cases are skipped without one. The
platform source is a stand-in module with the real helper's layout, never a
real ork-meter checkout.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from workspace_temp import WorkspaceTemp as TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_release", ROOT / "scripts/package_release.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)

GO = shutil.which("go")
MAIN = '''package main

import (
	"fmt"
	"os"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "Usage: ork-meter pair ... | collect ...")
		os.Exit(2)
	}
	fmt.Println("synthetic helper")
}
'''


def host_target():
    from ork_codex import helper
    return helper.target()


def write_source(root):
    (root / "cmd/ork-meter").mkdir(parents=True)
    (root / "go.mod").write_text("module example.invalid/fake-platform\n\ngo 1.21\n")
    (root / "cmd/ork-meter/main.go").write_text(MAIN)
    return root


class PackageRelease(unittest.TestCase):
    def test_manifests_must_agree_on_one_semver_version(self):
        version = package.plugin_version(ROOT)
        self.assertRegex(version, r"^\d+\.\d+\.\d+")
        with TemporaryDirectory() as temp:
            fake = Path(temp)
            (fake / ".codex-plugin").mkdir()
            (fake / "plugin.json").write_text(json.dumps({"name": "ork-meter-codex", "version": "1.2.3"}))
            (fake / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "ork-meter-codex", "version": "1.2.4"}))
            (fake / "pyproject.toml").write_text('[project]\nname = "ork-meter-codex"\nversion = "1.2.3"\n')
            with self.assertRaises(SystemExit):
                package.plugin_version(fake)

    def test_source_without_the_helper_and_non_empty_output_are_refused_before_building(self):
        with TemporaryDirectory() as temp:
            source, output = Path(temp) / "source", Path(temp) / "out"
            source.mkdir()
            with self.assertRaises(SystemExit):
                package.build(source, output, "v0", ["linux-amd64"], go="go-must-not-run", wheel=False)
            self.assertFalse(output.exists())
            write_source(source)
            output.mkdir()
            (output / "stale").write_text("earlier run\n")
            with self.assertRaises(SystemExit):
                package.build(source, output, "v0", ["linux-amd64"], go="go-must-not-run", wheel=False)
            self.assertEqual([p.name for p in output.iterdir()], ["stale"])

    @unittest.skipUnless(GO, "go is not on PATH")
    def test_helpers_are_built_with_release_flags_and_the_manifest_matches_each_digest(self):
        with TemporaryDirectory() as temp:
            source = write_source(Path(temp) / "platform source")
            output = Path(temp) / "release"
            targets = list(dict.fromkeys([host_target(), "windows-amd64"]))  # one entry on a Windows host
            record = package.build(source, output, "v9.9.9", targets, go=GO, wheel=False)
            names = sorted(p.name for p in output.iterdir())
            expected = ["manifest.json"]
            for target in targets:
                asset = package.asset_name(target)
                expected += [asset, asset + ".sha256"]
            self.assertEqual(names, sorted(expected))
            self.assertTrue(output.joinpath("ork-meter-windows-amd64.exe").is_file())
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest, record)
            self.assertEqual(manifest["plugin_version"], package.plugin_version(ROOT))
            self.assertEqual(manifest["ork_meter_ref"], "v9.9.9")
            self.assertIsNone(manifest["wheel"])
            self.assertIn("-buildid=", manifest["build"])
            self.assertIn("-buildvcs=false", manifest["build"])
            self.assertTrue(manifest["go_version"].startswith("go version"))
            for name, value in manifest["assets"].items():
                self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), value, name)
                self.assertEqual((output / (name + ".sha256")).read_text(), f"{value}  {name}\n")
            self.assertEqual({entry["asset"] for entry in manifest["helpers"].values()}, set(manifest["assets"]))
            host = output / package.asset_name(host_target())
            result = subprocess.run([str(host)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Usage:", result.stderr)
            # Stripped, no build id, no VCS stamp: the same source builds the same bytes.
            again = Path(temp) / "again"
            package.build(source, again, "v9.9.9", [host_target()], go=GO, wheel=False)
            self.assertEqual((again / host.name).read_bytes(), host.read_bytes())


if __name__ == "__main__":
    unittest.main()
