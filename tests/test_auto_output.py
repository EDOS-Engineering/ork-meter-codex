import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ork_codex.auto_hook import configure, MARKER
from ork_codex.auto_output import command_kind, handle, main
from ork_codex.storage import Store
from ork_codex.policy import Policy
from ork_codex.reduce import compact
import plus_fixture


def fixture():
    return ("\n".join(f"tests/test_orders.py::test_failure_mode[{i}] PASSED" for i in range(600)) +
            "\ntests/test_orders.py::test_rounding PASSED [100%]\n" +
            "\nFAILURES\nexpected 1250, actual 1200\nAssertionError: rounding\n")


class AutomaticOutputTests(unittest.TestCase):
    def test_repeated_go_test_name_keeps_the_interrupted_execution(self):
        data = ("=== RUN   TestAgain\n--- PASS: TestAgain (0.00s)\n" +
                "".join(f"=== RUN   Test{i}\n--- PASS: Test{i} (0.00s)\n" for i in range(600)) +
                "=== RUN   TestAgain\npanic: interrupted execution\n").encode()
        output, info = compact(data, "test", Policy())
        self.assertTrue(info["reduced"])
        self.assertEqual(output.count(b"=== RUN   TestAgain\n"), 1)
        self.assertIn(b"panic: interrupted execution", output)

    def test_shell_operators_cannot_hide_compound_commands(self):
        for command in ("pytest\ncat report.txt", "pytest |cat report.txt",
                        "pytest -q&&cat report.txt", "pytest -q;cat report.txt",
                        "pytest $(cat report.txt)", "pytest `cat report.txt`",
                        'pytest "$(cat report.txt)"', 'pytest "`cat report.txt`"',
                        "bash -c 'pytest\ncat report.txt'"):
            with self.subTest(command=command):
                self.assertIsNone(command_kind(command))
        self.assertEqual(command_kind("pytest 'tests/test_math.py::test_add[a|b]'"), "test")

    def test_small_parameterized_pass_set_stays_visible_without_recovery(self):
        passes = "".join(f"tests/test_math.py::test_add[{i}] PASSED [100%]\n" for i in range(8))
        data = ("".join(f"progress {i} loading fixture\n" for i in range(1500)) + passes).encode()
        with tempfile.TemporaryDirectory() as directory:
            event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                     "tool_input": {"command": "pytest -v"}, "tool_response": data.decode()}
            reply, info, size = handle(event, Path(directory) / "private-state")
            self.assertTrue(info["reduced"])
            self.assertGreaterEqual(info["token_gate"]["original_tokens"] -
                                    info["token_gate"]["candidate_tokens"], 512)
            for line in passes.splitlines():
                self.assertIn(line, reply["reason"])
            self.assertEqual(Store(Path(directory) / "private-state").get(info["artifact"]), data)

    def test_explicit_keep_overrides_routine_build_filter_with_context(self):
        data = ("\n".join(f"Building object_{i}: success" for i in range(600)) +
                "\nfatal: linker failed\n").encode()
        ordinary, _ = compact(data, "build", Policy(budget=1024))
        hinted, info = compact(data, "build", Policy(budget=1024, keep=("object_300:",)))
        self.assertNotIn(b"object_300:", ordinary)
        self.assertTrue(info["reduced"])
        for i in (297, 300, 308):
            self.assertIn(f"Building object_{i}: success".encode(), hinted)
        self.assertIn(b"fatal: linker failed", hinted)

    def test_reduction_preserves_failure_and_named_pass_with_exact_original(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory) / "private-state"
            event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                     "tool_input": {"command": "python -I -B -m pytest -v"}, "tool_response": fixture()}
            reply, info, size = handle(event, directory)
            self.assertIn("expected 1250, actual 1200", reply["reason"])
            self.assertIn("test_rounding PASSED", reply["reason"])
            self.assertLess(size, len(fixture().encode()) // 3)
            info.confirm(size)
            self.assertEqual(Store(directory).get(info["artifact"]), fixture().encode())

    def test_unknown_compound_and_source_commands_pass_through(self):
        for command in ("pytest && cat secret.txt", "go test ./... | head"):
            self.assertIsNone(command_kind(command))
        # The source preview and script-log filters are opt-in (ORK_CODEX_SOURCE_FILTER,
        # ORK_CODEX_PYTHON_LOG_FILTER). Their commands are classified, but with the
        # default environment they still pass through untouched.
        cleared = {k: v for k, v in os.environ.items() if not k.startswith("ORK_CODEX_")}
        for command, kind, gate in (("cat source.py", "source", "source filter off"),
                                    ("python script.py", "script-log", "script-log filter off")):
            self.assertEqual(command_kind(command), kind)
            trace = {}
            with patch.dict(os.environ, cleared, clear=True), tempfile.TemporaryDirectory() as d:
                self.assertIsNone(handle({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                                          "tool_input": {"command": command}, "tool_response": fixture()},
                                         Path(d) / "private-state", trace))
            self.assertEqual(trace.get("reason"), gate)
        self.assertEqual(command_kind("cd project && go test -v ./..."), "test")
        self.assertEqual(command_kind("ninja -C build"), "build")

    def test_off_and_malformed_input_are_silent(self):
        with patch.dict(os.environ, {"ORK_CODEX_MODE": "off"}), tempfile.TemporaryDirectory() as d:
            self.assertIsNone(handle({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                                      "tool_input": {"command": "pytest"}, "tool_response": fixture()}, Path(d) / "private-state"))
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO("null")), patch("sys.stdout", output):
            main()
        self.assertEqual(output.getvalue(), "")

    def test_install_snapshot_launch_and_uninstall_preserve_unrelated_hooks(self):
        with tempfile.TemporaryDirectory(prefix="ork hook space ") as d:
            home = Path(d)
            other = {"type": "command", "command": "echo other"}
            original = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [other,
                        {"statusMessage": MARKER, "command": "old"}]}], "Stop": []}, "custom": 1}
            (home / "hooks.json").write_text(json.dumps(original))
            configure(home, "install")
            installed = json.loads((home / "hooks.json").read_text())
            self.assertEqual(installed["hooks"]["PostToolUse"][0]["hooks"], [other])
            self.assertEqual(configure(home, "install")["status"], "already-installed")
            launch = next((home / "ork-auto").glob("*/run.py"))
            import sys
            event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                     "tool_input": {"command": "pytest -v"}, "tool_response": fixture()}
            # The installed launcher runs auto_output.main directly: without a
            # verified Ork Meter Plus answer it passes the output through.
            free = subprocess.run([sys.executable, "-I", "-B", str(launch)], input=json.dumps(event),
                                  capture_output=True, text=True, env={**os.environ, "CODEX_HOME": d}, timeout=15)
            self.assertEqual((free.returncode, free.stdout), (0, ""), free.stderr)
            plus_fixture.write(d)
            result = subprocess.run([sys.executable, "-I", "-B", str(launch)], input=json.dumps(event),
                                    capture_output=True, text=True, env={**os.environ, "CODEX_HOME": d}, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("expected 1250", json.loads(result.stdout)["reason"])
            configure(home, "uninstall")
            self.assertFalse(configure(home, "status")["installed"])
            self.assertEqual(json.loads((home / "hooks.json").read_text()),
                             {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [other]}], "Stop": []}, "custom": 1})

    def test_invalid_configuration_is_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hooks.json"
            path.write_text('{"hooks": []}')
            with self.assertRaises(ValueError):
                configure(d, "install")
            self.assertEqual(path.read_text(), '{"hooks": []}')

    def test_changed_snapshot_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            configure(d, "install")
            config = (Path(d) / "hooks.json").read_bytes()
            launch = next((Path(d) / "ork-auto").glob("*/run.py"))
            launch.write_text("changed locally")
            with self.assertRaises(ValueError):
                configure(d, "install")
            self.assertEqual(launch.read_text(), "changed locally")
            self.assertEqual((Path(d) / "hooks.json").read_bytes(), config)


if __name__ == "__main__":
    unittest.main()
