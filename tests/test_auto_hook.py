import tempfile
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from hooks.post_tool_use import command_kind, handle
from ork_codex.auto_hook import install, MARKER
from ork_codex.storage import Store
import plus_fixture
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def run_tree(argv, cwd, env, timeout):
    """Run argv; on timeout kill its whole process tree before failing.

    subprocess.run kills only the direct child. A timed-out `go test` leaves the
    compiler and test binary running, and on Windows they keep the temporary
    directory open, so its cleanup fails with WinError 32 and hides the timeout.
    """
    child = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=os.name != "nt",
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        raise
    return subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)


class AutoHook(unittest.TestCase):


    def test_timed_out_command_tree_releases_its_directory(self):
        # A grandchild holding a file open inside the directory stands in for
        # the Go compiler and test binary that a timed-out `go test` leaves behind.
        grandchild = ("import os, sys, time\n"
                      "handle = open('held.txt', 'w')\n"
                      "open('grandchild.pid', 'w').write(str(os.getpid()))\n"
                      "time.sleep(120)\n")
        parent = ("import subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-c', %r])\n"
                  "time.sleep(120)\n" % grandchild)
        directory = tempfile.mkdtemp()
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                run_tree([sys.executable, "-c", parent], directory, dict(os.environ), timeout=5)
            pid = int((Path(directory) / "grandchild.pid").read_text())
            if os.name != "nt":
                for _ in range(100):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("grandchild survived the timeout")
        finally:
            # Raises WinError 32 on Windows if any process still holds the directory.
            shutil.rmtree(directory)


    def test_command_recognition_requires_executed_test_or_build(self):
        self.assertEqual(command_kind("/bin/bash -lc 'python -m pytest -v tests/test_many.py'"), "test")
        self.assertEqual(command_kind("cargo test --workspace"), "test")
        self.assertEqual(command_kind("npm run build"), "build")
        self.assertEqual(command_kind("ninja -C build"), "build")
        self.assertEqual(command_kind("cmake --build build"), "build")
        self.assertEqual(command_kind("cd app && python -m pytest -v tests"), "test")
        self.assertEqual(command_kind("cd 'my app' && make build"), "build")
        self.assertEqual(command_kind("/usr/bin/python3.12 -I -B -m pytest -v tests"), "test")
        self.assertEqual(command_kind("python -B -m unittest discover -s tests"), "test")
        self.assertEqual(command_kind("cat tests/test_pytest.py"), "source")
        self.assertIsNone(command_kind("rg pytest src"))
        self.assertEqual(command_kind("rg -n retry ."), "search")
        self.assertIsNone(command_kind("rg --files"))
        self.assertEqual(command_kind("python -B tools/print_pytest.py"), "script-log")
        self.assertEqual(command_kind("/bin/bash -lc 'cat pricing.py'"), "source")
        self.assertIsNone(command_kind("cat pricing.py settings.py"))
        self.assertIsNone(command_kind("pytest -v && cat src/large.py"))
        self.assertIsNone(command_kind("cd app && pytest -v && cat src/large.py"))

    def test_native_bash_test_output_is_replaced_and_recoverable(self):
        original = "".join(f"test case_{i} ... ok\n" for i in range(900)) + "FAIL: critical\nexpected: 1\nreceived: 2\n"
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "/usr/bin/python3.12 -I -B -m pytest -v"}, "tool_response": original}
        with tempfile.TemporaryDirectory() as directory:
            result = handle(event, Path(directory) / "state")
            self.assertIsNotNone(result)
            feedback, info, _ = result
            self.assertEqual(feedback["decision"], "block")
            self.assertIn("FAIL: critical", feedback["reason"])
            self.assertLess(len(feedback["reason"]), len(original) // 5)
            self.assertEqual(info["reduced"], True)

    def test_opt_in_hook_trace_records_decision_without_original_text(self):
        original = ("case passed\n" * 900) + "FAIL: private-marker-73 expected 1 actual 2\n"
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "/bin/bash -lc 'pytest -v'"},
                 "tool_response": {"aggregatedOutput": original, "private-marker-73-key": "metadata"}}
        with tempfile.TemporaryDirectory() as directory:
            trace_file = Path(directory) / "hook-trace.jsonl"
            env = {key: value for key, value in os.environ.items() if not key.startswith("ORK_CODEX_")}
            plus_fixture.write(directory)  # savings levers need Ork Meter Plus
            env.update(PLUGIN_DATA=directory, ORK_CODEX_HOOK_TRACE=str(trace_file), CODEX_HOME=directory)
            hook = Path(__file__).resolve().parents[1] / "hooks/post_tool_use.py"
            result = subprocess.run([sys.executable, "-I", "-B", str(hook)], input=json.dumps(event),
                                    capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(result.returncode, 0)
            self.assertIn("private-marker-73", result.stdout)
            trace_text = trace_file.read_text()
            self.assertNotIn("private-marker-73", trace_text)
            self.assertNotIn("pytest -v", trace_text)
            trace = json.loads(trace_text)
            self.assertEqual(trace["kind"], "test")
            self.assertEqual(trace["response_bytes"], len(original.encode()))
            self.assertEqual(trace["decision"], "replacement emitted")

    def test_clean_go_output_is_preserved_but_failed_go_output_is_reduced(self):
        rows = "".join(f"=== RUN   TestPayments/pass_{i:04d}\n    --- PASS: TestPayments/pass_{i:04d} (0.00s)\n"
                       for i in range(250))
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "/bin/bash -lc 'go test -v ./...'"},
                 "tool_response": rows + "--- PASS: TestPayments (0.01s)\nPASS\nok  \texample.com/ork-benchmark\t0.01s\n"}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            self.assertIsNone(handle(event, state))
            self.assertEqual(Store(state).report()["modes"], {})
            event["tool_response"] = (rows + "--- FAIL: TestPayments/failure (0.00s)\n"
                                      "expected 1000, actual 100\nFAIL\n"
                                      "ok  \texample.com/other-package\t0.01s\n")
            self.assertIsNotNone(handle(event, state))

    def test_line_numbered_rg_preserves_file_coverage_and_exact_recovery(self):
        original = "".join(f"src/module_{file}.py:{line}: retry record {line}\n"
                           for file in range(4) for line in range(1, 121))
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "/bin/bash -lc 'rg -n retry .'"},
                 "tool_response": original}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with patch.dict("os.environ", {"ORK_CODEX_SEARCH_FILTER": "off"}):
                self.assertIsNone(handle(event, state))
            with patch.dict("os.environ", {"ORK_CODEX_SEARCH_FILTER": "on"}):
                result = handle(event, state)
            self.assertIsNotNone(result)
            feedback, info, _ = result
            for file in range(4):
                self.assertIn(f"src/module_{file}.py:1:", feedback["reason"])
                self.assertIn(f"src/module_{file}.py:120:", feedback["reason"])
            self.assertEqual(info["search_files_preserved"], 4)
            self.assertEqual(Store(state).get(info["artifact"]), original.encode())
            event["tool_response"] = '{"path":"src/module_0.py","line":1}\n' * 400
            with patch.dict("os.environ", {"ORK_CODEX_SEARCH_FILTER": "on"}):
                self.assertIsNone(handle(event, state))

    def test_direct_python_log_reduction_requires_explicit_opt_in(self):
        original = ("".join(f"evidence_{i:04d} context context context context\n" for i in range(600)) +
                    "ERROR: update selector using the recorded choice\n")
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "/bin/bash -lc 'python tools/probe.py'"},
                 "tool_response": original}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with patch.dict("os.environ", {"ORK_CODEX_PYTHON_LOG_FILTER": "off"}):
                self.assertIsNone(handle(event, state))
            with patch.dict("os.environ", {"ORK_CODEX_PYTHON_LOG_FILTER": "on"}):
                result = handle(event, state)
            self.assertIsNotNone(result)
            feedback, info, _ = result
            self.assertIn("ERROR: update selector", feedback["reason"])
            self.assertEqual(Store(state).get(info["artifact"]), original.encode())

    def test_source_reduction_keeps_middle_symbol_and_exact_recovery(self):
        original = ("# generated rate table\n" +
                    "".join(f"RATE_{index:04d} = {index}\n" for index in range(300)) +
                    "def quote(value):\n    return int(float(value) * 100)\n" +
                    "".join(f"RATE_{index:04d} = {index}\n" for index in range(300, 600)))
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "/bin/bash -lc 'cat pricing.py'"},
                 "tool_response": original}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with patch.dict("os.environ", {"ORK_CODEX_SOURCE_FILTER": "off"}):
                self.assertIsNone(handle(event, state))
            with patch.dict("os.environ", {"ORK_CODEX_SOURCE_FILTER": "on"}):
                result = handle(event, state)
                invalid = {**event, "tool_response": original + "def broken(:\n"}
                self.assertIsNone(handle(invalid, state))
            self.assertIsNotNone(result)
            feedback, info, _ = result
            self.assertIn("def quote(value):", feedback["reason"])
            self.assertIn("[omitted source lines", feedback["reason"])
            self.assertEqual(Store(state).get(info["artifact"]), original.encode())

    def test_bounded_read_keeps_middle_declaration_and_exact_recovery(self):
        source = ("# pricing rules\n" +
                  "".join(f"RATE_{index:04d} = {index}\n" for index in range(300)) +
                  "def quote(value):\n    return int(float(value)) * 1000\n" +
                  "".join(f"RATE_{index:04d} = {index}\n" for index in range(300, 600)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pricing.py").write_text(source)
            state = root / "state"
            event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                     "tool_input": {"command": "cat pricing.py; ls", "workdir": directory},
                     "tool_response": source + "pricing.py\n"}
            with patch.dict("os.environ", {"ORK_CODEX_SOURCE_FILTER": "off",
                                                "ORK_CODEX_BOUNDED_READ_FILTER": "off"}):
                self.assertIsNone(handle(event, state))
            with patch.dict("os.environ", {"ORK_CODEX_SOURCE_FILTER": "off",
                                                "ORK_CODEX_BOUNDED_READ_FILTER": "on"}):
                result = handle(event, state)
                altered = {**event, "tool_response": event["tool_response"].replace("RATE_0001 = 1", "RATE_0001 = 2")}
                self.assertIsNone(handle(altered, state))
            self.assertIsNotNone(result)
            feedback, info, _ = result
            self.assertIn("def quote(value):", feedback["reason"])
            self.assertIn("[omitted read lines", feedback["reason"])
            self.assertIn("pricing.py\n", feedback["reason"])
            self.assertEqual(Store(state).get(info["artifact"]), event["tool_response"].encode())

    def test_compound_source_preserves_other_output_and_full_recovery(self):
        source = ("# generated rate table\n" +
                  "".join(f"RATE_{index:04d} = {index}\n" for index in range(300)) +
                  "def quote(value):\n    return int(float(value) * 100)\n" +
                  "".join(f"RATE_{index:04d} = {index}\n" for index in range(300, 600)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pricing.py").write_text(source)
            state = root / "state"
            original = "pricing.py\n" + source + "tests/test_pricing.py\n"
            event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                     "tool_input": {"command": "/bin/bash -lc 'ls; cat pricing.py; rg --files -g \"*test*\"'",
                                    "workdir": directory}, "tool_response": original}
            with patch.dict("os.environ", {"ORK_CODEX_SOURCE_FILTER": "off"}):
                self.assertIsNone(handle(event, state))
            with patch.dict("os.environ", {"ORK_CODEX_SOURCE_FILTER": "on"}):
                result = handle(event, state)
                mismatched = {**event, "tool_response": original.replace("RATE_0001 = 1", "RATE_0001 = 2")}
                self.assertIsNone(handle(mismatched, state))
                repeated = {**event, "tool_response": original + source}
                self.assertIsNone(handle(repeated, state))
                piped = {**event, "tool_input": {**event["tool_input"],
                          "command": "/bin/bash -lc 'ls; cat pricing.py | head -20; rg --files'"}}
                self.assertIsNone(handle(piped, state))
            self.assertIsNotNone(result)
            feedback, info, _ = result
            self.assertTrue(feedback["reason"].startswith("pricing.py\n"))
            self.assertIn("tests/test_pricing.py", feedback["reason"])
            self.assertIn("def quote(value):", feedback["reason"])
            self.assertEqual(Store(state).get(info["artifact"]), original.encode())

    def test_successful_build_rows_with_error_themed_names_are_compacted(self):
        original = ("".join(f"Compiling error_handler_{i:04d}: success\n" for i in range(800)) +
                    "ERROR: target payments failed: expected 1000, actual 100\n")
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                 "tool_input": {"command": "make build"}, "tool_response": original}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            result = handle(event, state)
            self.assertIsNotNone(result)
            feedback, info, _ = result
            self.assertIn("ERROR: target payments failed: expected 1000, actual 100", feedback["reason"])
            self.assertLess(len(feedback["reason"]), len(original) // 5)
            self.assertEqual(info["routine_build_lines_omitted"], 800)
            self.assertEqual(Store(state).get(info["artifact"]), original.encode())

    def test_ninja_progress_is_compacted_but_unknown_brackets_pass_through(self):
        original = ("".join(f"[{i}/800] Building CXX object unit_{i:04d}.o\n" for i in range(1, 801)) +
                    "FAILED: target payments\nfatal error: expected 1000, actual 100\n")
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                     "tool_input": {"command": "ninja -C build"}, "tool_response": original}
            result = handle(event, state)
            self.assertIsNotNone(result)
            feedback, info, _ = result
            self.assertLess(len(feedback["reason"]), len(original) // 5)
            self.assertIn("FAILED: target payments", feedback["reason"])
            self.assertIn("fatal error: expected 1000, actual 100", feedback["reason"])
            self.assertGreater(info["routine_build_lines_omitted"], 790)
            self.assertEqual(Store(state).get(info["artifact"]), original.encode())
            event["tool_response"] = "[unknown-format]\n" + original
            self.assertIsNone(handle(event, state))

    def test_small_or_unknown_results_pass_through(self):
        with tempfile.TemporaryDirectory() as directory:
            for event in ({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                           "tool_input": {"command": "echo hi"}, "tool_response": "hi"},
                          {"hook_event_name": "PostToolUse", "tool_name": "apply_patch",
                           "tool_input": {"command": "patch"}, "tool_response": "x" * 9000},
                          {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                           "tool_input": {"command": "cat source.py"}, "tool_response": "print('x')\n" * 9000}):
                self.assertIsNone(handle(event, Path(directory)))

    def test_installer_preserves_existing_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            existing = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo existing"}]}]}}
            (home / "hooks.json").write_text(json.dumps(existing))
            first = install(home)
            second = install(home)
            config = json.loads((home / "hooks.json").read_text())
            self.assertEqual(first["status"], "installed")
            self.assertEqual(second["status"], "already-installed")
            self.assertEqual(config["hooks"]["SessionStart"], existing["hooks"]["SessionStart"])
            self.assertEqual(len(config["hooks"]["PostToolUse"]), 1)
            self.assertEqual(config["hooks"]["PostToolUse"][0]["hooks"][0]["statusMessage"], MARKER)
            self.assertTrue(Path(first["hook"]).is_file())
