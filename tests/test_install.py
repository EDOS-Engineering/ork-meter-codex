"""The one-line installer and the versionless entry point find the release Codex installed.

A stand-in `codex` on PATH plays Codex's plugin commands in a disposable
CODEX_HOME; no network, Codex, browser or credential is used.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bootstrap_for_launcher", ROOT / "scripts/ork_codex.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)

# Records its arguments and CODEX_HOME, the way the real bootstrap would start setup.
FAKE_BOOTSTRAP = """import json, os, sys
with open(os.path.join(os.path.dirname(__file__), "ran.json"), "w") as out:
    json.dump({"argv": sys.argv[1:], "home": os.environ.get("CODEX_HOME"), "tty": os.isatty(0)}, out)
"""

FAKE_CODEX = """#!/bin/sh
echo "$*" >> "$CODEX_HOME/codex-calls.txt"
case "$*" in
  "plugin add ork-meter-codex@edos-codex --json")
    root="$CODEX_HOME/plugins/cache/edos-codex/ork-meter-codex/9.10.0"
    mkdir -p "$root/scripts"
    cp "$FAKE_BOOTSTRAP" "$root/scripts/ork_codex.py"
    printf '{"pluginId": "ork-meter-codex@edos-codex", "version": "9.10.0", "installedPath": "%s"}\\n' "$root" ;;
esac
"""


def plant(home, version, body=FAKE_BOOTSTRAP):
    scripts = home / "plugins/cache/edos-codex/ork-meter-codex" / version / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "ork_codex.py").write_text(body)
    return scripts


def run_launcher(launcher, home, path):
    env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
    env["PATH"] = path
    return subprocess.run([str(launcher), "setup", "--x"], env=env, capture_output=True, text=True, timeout=60)


NO_CODEX = os.path.join(os.environ.get("SystemRoot", ""), "System32") if os.name == "nt" else "/usr/bin:/bin"


class Launcher(unittest.TestCase):
    def write(self, home, pin):
        with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            return bootstrap.write_launcher({"version": pin})

    def test_a_rollback_runs_the_pinned_release_not_the_highest_cached_one(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            pinned = plant(home, "0.1.3")
            higher = plant(home, "0.1.10")
            plant(home, "not-a-version")
            launcher = self.write(home, "0.1.3")
            self.assertEqual(launcher.parent, home / "ork-meter-codex" / "bin")
            self.assertEqual(launcher.name, "ork-codex.cmd" if os.name == "nt" else "ork-codex")
            if os.name != "nt":
                self.assertTrue(os.access(launcher, os.X_OK))
            result = run_launcher(launcher, home, NO_CODEX)
            self.assertEqual(result.returncode, 0, result.stderr)
            ran = json.loads((pinned / "ran.json").read_text())
            self.assertEqual(ran["argv"], ["setup", "--x"])
            self.assertEqual(Path(ran["home"]).resolve(), home.resolve())
            self.assertFalse((higher / "ran.json").exists())

    @unittest.skipIf(os.name == "nt", "the stand-in codex is a shell script")
    def test_the_marketplace_pin_wins_over_the_last_run_and_the_highest_cached(self):
        # 0.1.3: a rollback below what is cached. 0.1.10: an upgrade. 9.9.9: pinned but not
        # cached yet, so the last run's version still works. The marketplace may also be unreadable.
        for pin, expected in (("0.1.3", "0.1.3"), ("0.1.10", "0.1.10"), ("9.9.9", "0.1.5"), (None, "0.1.5")):
            with self.subTest(pin=pin), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                dirs = {v: plant(home, v) for v in ("0.1.3", "0.1.5", "0.1.10")}
                launcher = self.write(home, "0.1.5")
                snapshot = home / "snapshot"
                snapshot.mkdir()
                if pin:
                    (snapshot / "release.json").write_text(json.dumps({"version": pin}))
                fake = home / "fake-bin"
                fake.mkdir()
                listing = json.dumps({"marketplaces": [{"name": "other", "root": "/nowhere"}, {"name": "edos-codex", "root": str(snapshot)}]})
                (fake / "codex").write_text("#!/bin/sh\n[ \"$*\" = 'plugin marketplace list --json' ] || exit 9\ncat <<'JSON'\n" + listing + "\nJSON\n")
                (fake / "codex").chmod(0o755)
                result = run_launcher(launcher, home, f"{fake}:/usr/bin:/bin")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([v for v, d in dirs.items() if (d / "ran.json").exists()], [expected])

    def test_launcher_without_an_installed_plugin_says_how_to_add_it(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            plant(home, "0.1.10")  # A cached release that is not the pin is not run.
            self.write(home, "0.1.3")
            env = dict(os.environ, PATH=NO_CODEX)
            result = subprocess.run([sys.executable, "-I", str(home / "ork-meter-codex/bin/ork-codex.py"), "setup"],
                                    capture_output=True, text=True, timeout=60, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("codex plugin add ork-meter-codex@edos-codex", result.stderr)

    def test_rewriting_an_unchanged_launcher_leaves_it_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            first = self.write(home, "0.1.3")
            stamp = first.stat().st_mtime_ns
            self.assertEqual(self.write(home, "0.1.3"), first)
            self.assertEqual(first.stat().st_mtime_ns, stamp)
            script = first.parent / "ork-codex.py"
            before = script.read_text()
            self.write(home, "0.1.2")
            self.assertNotEqual(script.read_text(), before, "a new pin rewrites the launcher")


@unittest.skipIf(os.name == "nt", "install.sh is the macOS and Linux installer")
class InstallScript(unittest.TestCase):
    def run_installer(self, home, with_terminal):
        bin_dir = home / "fake-bin"
        bin_dir.mkdir()
        (bin_dir / "codex").write_text(FAKE_CODEX)
        (bin_dir / "codex").chmod(0o755)
        (home / "fake_bootstrap.py").write_text(FAKE_BOOTSTRAP)
        python_dir = Path(sys.executable).parent
        env = {"PATH": f"{bin_dir}{os.pathsep}{python_dir}{os.pathsep}/usr/bin:/bin", "CODEX_HOME": str(home),
               "FAKE_BOOTSTRAP": str(home / "fake_bootstrap.py"), "HOME": str(home)}
        if (python_dir / "python3").exists() is False:
            (bin_dir / "python3").symlink_to(sys.executable)
        argv = ["sh", str(ROOT / "install.sh"), "--replace-telemetry"]
        if not with_terminal:
            return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=60, start_new_session=True)
        import pty
        import warnings
        with warnings.catch_warnings():
            # The child execs sh at once, so another test's threads cannot deadlock it.
            warnings.simplefilter("ignore", DeprecationWarning)
            pid, master = pty.fork()
        if pid == 0:
            os.execvpe("sh", argv, env)
        output = b""
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        _, status = os.waitpid(pid, 0)
        os.close(master)
        return subprocess.CompletedProcess(argv, os.waitstatus_to_exitcode(status), output.decode(errors="replace"), "")

    def test_one_line_install_adds_the_plugin_and_starts_setup_on_the_terminal(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            result = self.run_installer(home, with_terminal=True)
            self.assertEqual(result.returncode, 0, result.stdout)
            calls = (home / "codex-calls.txt").read_text().splitlines()
            self.assertEqual(calls, ["plugin marketplace add EDOS-Engineering/ork-meter-codex",
                                     "plugin marketplace upgrade edos-codex",
                                     "plugin add ork-meter-codex@edos-codex --json"])
            ran = json.loads((home / "plugins/cache/edos-codex/ork-meter-codex/9.10.0/scripts/ran.json").read_text())
            self.assertEqual(ran["argv"], ["setup", "--replace-telemetry"])
            self.assertTrue(ran["tty"], "setup reads the person's terminal, not the curl pipe")

    def test_without_a_terminal_it_says_how_to_finish(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            result = self.run_installer(home, with_terminal=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("No terminal is attached", result.stderr)
            self.assertIn("9.10.0/scripts/ork_codex.py", result.stderr)
            self.assertFalse((home / "plugins/cache/edos-codex/ork-meter-codex/9.10.0/scripts/ran.json").exists())


if __name__ == "__main__":
    unittest.main()
