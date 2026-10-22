import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import signal
import time
import threading
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from workspace_temp import WorkspaceTemp
import plus_fixture
from ork_codex.audit import audit
from ork_codex.mcp import Service
from ork_codex.policy import Policy
from ork_codex.reduce import deliver as prepare_delivery
from ork_codex.retrieval import Sources, artifact as prepare_artifact
from ork_codex.runner import run
from ork_codex.delivery import OutputDeliveryError
from ork_codex.mcp import serve
from ork_codex.storage import Store, default_state

def setUpModule():
    plus_fixture.install()  # savings levers need Ork Meter Plus

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts/ork_codex.py"


def deliver(*args, **kwargs):
    output, info = prepare_delivery(*args, **kwargs)
    sink = io.BytesIO()
    accepted = sink.write(output)
    sink.flush()
    if hasattr(info, "confirm"):
        info.confirm(accepted)
    return output, info


def artifact(*args, **kwargs):
    result = prepare_artifact(*args, **kwargs)
    sink = io.BytesIO()
    accepted = sink.write(json.dumps(result, ensure_ascii=False).encode())
    sink.flush()
    result.receipt.confirm(accepted)
    return result


def log():
    return ("Starting checks\n" + "".join(f"test case_{i} ... ok\n" for i in range(1200))
            + "--- FAIL: payment_expiry\nexpected: 402\nreceived: 500\nstack frame: payment.py:72\n"
            + "SUMMARY: 1200 passed, 1 failed\n").encode()


class Core(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.policy = Policy(budget=1024, threshold=1024)

    def tearDown(self):
        self.temp.cleanup()

    def test_reduction_preserves_failure_and_exact_original(self):
        raw = log()
        output, info = deliver(raw, "test", self.store, self.policy)
        self.assertLess(len(output), len(raw))
        for expected in (b"payment_expiry", b"expected: 402", b"received: 500", b"payment.py:72", b"1 failed"):
            self.assertIn(expected, output)
        self.assertEqual(self.store.get(info["artifact"]), raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), info["artifact"])

    def test_complete_failing_block_and_diagnostic_soft_budget(self):
        raw = b"PASS suite\n" * 900 + b"FAILURES\n" + b"causal context without markers\n" * 800
        output, _ = deliver(raw, "test", self.store, self.policy)
        self.assertIn(b"FAILURES\n" + b"causal context without markers\n" * 800, output)

    def test_passing_rows_after_failure_are_filtered_without_losing_diagnostics(self):
        raw = (b"FAILURES\ncausal context without markers\n" +
               b"test case ... ok\n" * 900 +
               b"Traceback (most recent call last):\nAssertionError: expected 1000\n" +
               b"FAILED (failures=1)\n")
        output, info = deliver(raw, "test", self.store, Policy(budget=1024*1024, threshold=1024))
        self.assertTrue(info["reduced"])
        self.assertGreater(info["passing_lines_omitted"], 850)
        self.assertIn(b"causal context without markers", output)
        self.assertIn(b"AssertionError: expected 1000", output)
        self.assertIn(b"FAILED (failures=1)", output)
        self.assertEqual(self.store.get(info["artifact"]), raw)

    def test_passing_names_with_failure_words_do_not_hide_real_failure(self):
        raw = ("".join(f"tests/test_failure_mode.py::test_failure_case_{i} PASSED [ 50%]\n"
                       for i in range(600)) +
               "FAILURES\nAssertionError: expected 1, actual 2\n1 failed, 600 passed\n").encode()
        output, info = deliver(raw, "test", self.store, self.policy)
        self.assertTrue(info["reduced"])
        self.assertEqual(info["passing_lines_omitted"], 600)
        self.assertIn(b"AssertionError: expected 1, actual 2", output)
        self.assertIn(b"1 failed, 600 passed", output)
        self.assertEqual(self.store.get(info["artifact"]), raw)

    def test_go_run_rows_are_removed_only_when_the_same_subtest_passed(self):
        raw = ("".join(f"=== RUN   TestPayments/pass_{i:04d}\n" for i in range(200)) +
               "=== RUN   TestPayments/failure\n" +
               "".join(f"    --- PASS: TestPayments/pass_{i:04d} (0.00s)\n" for i in range(200)) +
               "    --- FAIL: TestPayments/failure (0.00s)\n" +
               "    payments_test.go:9: expected 1000, actual 100\nFAIL\n").encode()
        output, info = deliver(raw, "test", self.store, Policy(budget=128, threshold=1024))
        self.assertTrue(info["reduced"])
        self.assertEqual(info["passed_go_run_lines_omitted"], 200)
        self.assertIn(b"=== RUN   TestPayments/failure", output)
        self.assertIn(b"expected 1000, actual 100", output)
        self.assertNotIn(b"=== RUN   TestPayments/pass_", output)
        self.assertEqual(self.store.get(info["artifact"]), raw)

    def test_pytest_progress_omits_only_all_pass_rows(self):
        clean = b"................................ [ 40%]\n" * 600
        mixed = b"..........F..................... [ 80%]\n"
        raw = b"collecting tests\n" + clean + mixed + b"FAILED test_case.py::test_value\n1 failed, 599 passed\n"
        output, info = deliver(raw, "test", self.store, Policy(budget=1024*1024, threshold=1024))
        self.assertTrue(info["reduced"])
        self.assertGreater(info["passing_lines_omitted"], 580)
        self.assertIn(mixed, output)
        self.assertIn(b"FAILED test_case.py::test_value", output)
        self.assertEqual(self.store.get(info["artifact"]), raw)

    def test_skipped_ignored_expected_failures_and_pending_remain_visible(self):
        special = [b"test optional ... ignored", b"test_optional.py::quota SKIPPED", b"test_optional.py::known_bug XFAIL", b"test_optional.py::surprise XPASS", b"pending: unavailable service"]
        raw = b"test normal ... ok\n" * 800 + b"\n".join(special) + b"\n" + b"test normal ... ok\n" * 800 + b"SUMMARY: 1600 passed, 5 non-passing\n"
        result, info = deliver(raw, "test", self.store, self.policy)
        self.assertTrue(info["reduced"])
        for line in special:
            self.assertIn(line, result)

    def test_binary_unknown_structured_small_off_and_full_pass_through(self):
        for raw, kind, policy, full in ((b"small\n", "test", self.policy, False),
                                      (log(), "unknown", self.policy, False),
                                      (b"{\"x\":1}\n" * 900, "log", self.policy, False),
                                      (b"\x00\xff" * 3000, "log", self.policy, False),
                                      (log(), "test", Policy(mode="off"), False),
                                      (log(), "test", self.policy, True)):
            with self.subTest(kind=kind, full=full):
                output, info = deliver(raw, kind, self.store, policy, full)
                self.assertEqual(output, raw)
                self.assertFalse(info["reduced"])

    def test_storage_or_stats_failure_returns_original(self):
        for failure in (OSError("disk full"), sqlite3.OperationalError("disk full")):
            with patch.object(self.store, "put", side_effect=failure):
                output, _ = deliver(log(), "test", self.store, self.policy)
                self.assertEqual(output, log())
        with patch.object(self.store, "event", side_effect=sqlite3.OperationalError("locked")):
            output, _ = deliver(log(), "test", self.store, self.policy)
            self.assertEqual(output, log())
        output, _ = deliver(log(), "test", None, self.policy)
        self.assertEqual(output, log())

    def test_shadow_delivers_full_and_claims_zero_avoided(self):
        output, info = deliver(log(), "test", self.store, Policy(mode="shadow", budget=1024))
        self.assertEqual(output, log())
        self.assertTrue(info["shadow"])
        self.assertEqual(self.store.report()["modes"]["shadow"]["net_bytes_avoided"], 0)

    def test_recovery_charged_and_may_erase_estimated_savings(self):
        output, info = deliver(log(), "test", self.store, self.policy)
        artifact(self.store, info["artifact"], full=True)
        report = self.store.report()
        self.assertLess(report["modes"]["compact"]["net_bytes_avoided"], 0)
        self.assertIsNone(report["measured_usage"])
        self.assertIsNone(report["actual_cost_savings"])

    def test_shadow_recovery_cannot_fabricate_compact_events(self):
        _, info = deliver(log(), "test", self.store, Policy(mode="shadow"))
        artifact(self.store, info["artifact"], count=1)
        report = self.store.report()
        self.assertEqual(set(report["modes"]), {"shadow"})
        self.assertGreater(report["modes"]["shadow"]["bytes_recovered"], 0)

    def test_same_artifact_across_modes_and_attempts_needs_delivery_identity(self):
        _, compact = deliver(log(), "test", self.store, self.policy)
        other = Store(self.root / "state", run_id="attempt-B")
        _, shadow = deliver(log(), "test", other, Policy(mode="shadow"))
        self.assertEqual(compact["artifact"], shadow["artifact"])
        result = artifact(self.store, compact["artifact"], count=1)
        self.assertEqual(result["recovery_attribution"]["mode"], "unknown")
        result = artifact(self.store, shadow["artifact"], count=1, intervention=shadow["intervention"])
        self.assertEqual(result["recovery_attribution"]["mode"], "shadow")
        self.assertEqual(result["recovery_attribution"]["run_id"], "attempt-B")
        result = artifact(self.store, compact["artifact"], count=1, intervention=compact["intervention"])
        self.assertEqual(result["recovery_attribution"]["mode"], "compact")
        with self.assertRaises(ValueError):
            artifact(self.store, compact["artifact"], intervention="wrong-delivery")

    def test_artifacts_without_intervention_history_have_unknown_attribution(self):
        handle = self.store.put(b"historical original\n")
        result = artifact(self.store, handle, count=1)
        self.assertEqual(result["recovery_attribution"]["mode"], "unknown")
        self.assertEqual(set(self.store.report()["modes"]), {"unknown"})

    def test_artifact_exact_lines_search_counts_and_json_projection(self):
        raw = "α\r\nneedle\r\nthird\nneedle\n".encode()
        handle = self.store.put(raw)
        result = artifact(self.store, handle, start=2, count=1)
        self.assertEqual(result["content"], "needle\r\n")
        self.assertFalse(result["complete"])
        result = artifact(self.store, handle, "search", query="needle", count=1)
        self.assertEqual(result["total_matches"], 2)
        self.assertTrue(result["truncated"])
        handle = self.store.put(b'{"items":[{"a/b":3}],"secret":"not selected"}')
        selected = artifact(self.store, handle, "json", pointers=["/items/0/a~1b"])
        self.assertEqual(selected["selected"], {"/items/0/a~1b": 3})
        self.assertNotIn("secret", json.dumps(selected))
        with self.assertRaises(ValueError):
            artifact(self.store, handle, "json", pointers=["/items/-1"])
        with self.assertRaises(ValueError):
            artifact(self.store, handle, "json", pointers=["/~2"])

    def test_corrupt_missing_and_traversal_artifacts_refused(self):
        with self.assertRaises(ValueError):
            self.store.get("../stats.sqlite3")
        handle = self.store.put(b"original")
        self.store.path(handle).write_bytes(b"tampered")
        with self.assertRaises(OSError):
            self.store.get(handle)
        with self.assertRaises(OSError):
            self.store.get("f" * 64)

    def test_recovery_survives_stats_read_and_write_failure_without_claiming_zero(self):
        raw = b"original evidence\n"
        handle = self.store.put(raw)
        with patch.object(self.store, "event", side_effect=sqlite3.OperationalError("locked")):
            result = artifact(self.store, handle, full=True)
            self.assertEqual(result["content"].encode(), raw)
            self.assertFalse(result.receipt.recorded)
        with patch.object(self.store, "connection", side_effect=sqlite3.OperationalError("locked")):
            result = artifact(self.store, handle, full=True)
            self.assertEqual(result["content"].encode(), raw)
            self.assertEqual(result["recovery_attribution"]["basis"], "statistics unavailable")
            self.assertFalse(self.store.report()["accounting_complete"])
        self.assertFalse(Store(self.root / "state").report()["accounting_complete"])

    def test_corrupt_stats_cannot_block_raw_cli_recovery_of_valid_bytes(self):
        raw = b"exact\r\nevidence\n"
        handle = self.store.put(raw)
        self.store.db.write_bytes(b"not a SQLite database")
        result = subprocess.run([sys.executable, "-I", "-B", str(ENTRY), "--state", str(self.store.root),
                                 "artifact", handle, "--op", "raw"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, raw)
        self.assertIn(b"accounting is unavailable", result.stderr)

    def test_concurrent_content_addressing_and_statistics(self):
        def task(i):
            data = f"result {i}".encode()
            handle = self.store.put(data)
            self.store.event("log", 100, 50, mode="compact")
            return self.store.get(handle) == data
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            self.assertTrue(all(pool.map(task, range(40))))
        self.assertEqual(self.store.report()["modes"]["compact"]["events"], 40)

    def test_identical_writers_never_publish_partial_digest_files(self):
        started, release = threading.Event(), threading.Event()
        gate_lock = threading.Lock()
        seen = []
        original = os.fdopen
        class Gate:
            def __init__(self, out):
                self.out = out
            def __enter__(self):
                with gate_lock:
                    first = not seen
                    seen.append(True)
                if first:
                    started.set()
                    release.wait(4)
                return self.out.__enter__()
            def __exit__(self, *args):
                return self.out.__exit__(*args)
        data = b"same immutable output\n" * 300
        with patch("os.fdopen", side_effect=lambda *a, **k: Gate(original(*a, **k))):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.store.put, data)
                self.assertTrue(started.wait(2))
                try:
                    winner = pool.submit(self.store.put, data).result(timeout=2)
                    self.assertEqual(self.store.get(winner), data)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=2), winner)
        self.assertEqual(list(self.store.artifacts.glob(".pending-*")), [])

    def test_interrupted_writer_leaves_no_published_or_partial_artifact(self):
        original = os.fdopen
        class Interrupted:
            def __init__(self, out):
                self.out = out
            def __enter__(self):
                self.out.__enter__()
                return self
            def write(self, data):
                self.out.write(data[:3])
                raise OSError("synthetic interrupted write")
            def __exit__(self, *args):
                return self.out.__exit__(*args)
        with patch("os.fdopen", side_effect=lambda *a, **k: Interrupted(original(*a, **k))):
            with self.assertRaises(OSError):
                self.store.put(b"complete original")
        self.assertEqual(list(self.store.artifacts.iterdir()), [])

    def test_artifact_search_pages_use_physical_line_bounds(self):
        handle = self.store.put(b"hit one\nother\nhit two\nhit three\n")
        service = Service(self.store, self.root)
        first = service.call("artifact", {"handle": handle, "op": "search", "query": "hit", "count": 1})
        second = service.call("artifact", {"handle": handle, "op": "search", "query": "hit", "count": 1, "start": first["next_start_line"]})
        third = service.call("artifact", {"handle": handle, "op": "search", "query": "hit", "count": 1, "start": second["next_start_line"]})
        self.assertEqual([r["matches"][0]["line"] for r in (first, second, third)], [1, 3, 4])
        self.assertEqual(third["total_matches_entire_artifact"], 3)
        self.assertFalse(third["truncated"])
        self.assertIsNone(third["next_start_line"])

    def test_shadow_repeated_failures_preserve_both_cli_streams(self):
        environment = dict(os.environ, ORK_CODEX_MODE="shadow", ORK_CODEX_SESSION="shadow-only")
        command = [sys.executable, "-I", "-B", str(ENTRY), "--state", str(self.store.root), "run", "--kind", "test", "--", sys.executable, "-c", "import sys; print('fixture evidence'); sys.exit(7)"]
        for i in range(3):
            result = subprocess.run(command, capture_output=True, env=environment, timeout=5)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(result.stdout.replace(b"\r\n", b"\n"), b"fixture evidence\n")
            self.assertEqual(result.stderr, b"")

    def test_default_store_shared_by_project_subdirectories(self):
        (self.root / ".git").mkdir()
        sub = self.root / "nested"
        sub.mkdir()
        with patch.dict(os.environ, {}, clear=True), patch("os.getcwd", return_value=str(sub)):
            self.assertEqual(default_state(), self.root / ".local/ork-codex")
        with patch.dict(os.environ, {"ORK_CODEX_STATE": str(self.root / "chosen")}):
            self.assertEqual(default_state(), self.root / "chosen/ork-codex")

    def test_runner_executes_once_preserves_exit_argv_environment_cwd_and_streams(self):
        counter = self.root / "counter"
        code = "import sys,os; from pathlib import Path; p=Path(sys.argv[1]); p.write_text('once'); print(os.getcwd()); print(os.environ['ORK_TEST_MARKER']); print('test case ... ok\\n'*900); print('fatal: fixture error',file=sys.stderr); sys.exit(7)"
        out, err = io.BytesIO(), io.BytesIO()
        with patch.dict(os.environ, {"ORK_TEST_MARKER": "argv $() ` literal"}):
            status = run([sys.executable, "-c", code, str(counter)], "test", self.store, self.policy, stdout=out, stderr=err)
        self.assertEqual(status, 7)
        self.assertEqual(counter.read_text(), "once")
        self.assertIn(os.getcwd().encode(), out.getvalue())
        self.assertIn(b"argv $() ` literal", out.getvalue())
        self.assertIn(b"fatal: fixture error", err.getvalue())
        self.assertIn(b"command exit: 7", err.getvalue())

    def test_write_flush_and_partial_delivery_failures_cannot_claim_savings(self):
        for failure in ("write", "flush", "partial"):
            with self.subTest(failure=failure):
                store = Store(self.root / failure)
                marker = self.root / (failure + "-executed")
                class Sink:
                    def write(self, data):
                        if failure == "write":
                            raise BrokenPipeError("closed output")
                        return 1 if failure == "partial" else len(data)
                    def flush(self):
                        if failure == "flush":
                            raise BrokenPipeError("flush failed")
                command = [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('once'); print('test fixture ... ok\\n'*1000); print('SUMMARY: passed')", str(marker)]
                with self.assertRaises(OutputDeliveryError) as captured:
                    run(command, "test", store, self.policy, stdout=Sink(), stderr=io.BytesIO())
                self.assertEqual(marker.read_text(), "once")
                self.assertEqual(captured.exception.exit_code, 0)
                self.assertIn(b"SUMMARY: passed", store.get(captured.exception.artifact))
                report = store.report()
                self.assertEqual(report["modes"], {})
                expected = "flush_failed" if failure == "flush" else "write_failed"
                self.assertEqual(report["excluded_delivery_states"][expected], 1)
                self.assertFalse(report["accounting_complete"])

    def test_output_survives_failed_final_accounting_and_remains_unclaimed(self):
        out, err = io.BytesIO(), io.BytesIO()
        with patch.object(self.store, "finish_delivery", side_effect=sqlite3.OperationalError("locked")):
            status = run([sys.executable, "-c", "print('test fixture ... ok\\n'*1000); print('SUMMARY: passed')"],
                         "test", self.store, self.policy, stdout=out, stderr=err)
        self.assertEqual(status, 0)
        self.assertIn(b"artifact:", out.getvalue())
        self.assertEqual(self.store.report()["modes"], {})
        self.assertFalse(self.store.report()["accounting_complete"])

    def test_cli_storage_failure_still_executes_exactly_once(self):
        invalid = self.root / "invalid"
        invalid.write_text("not a directory")
        counter = self.root / "counter"
        result = subprocess.run([sys.executable, "-I", "-B", str(ENTRY), "--state", str(invalid),
                                 "run", "--kind", "test", "--", sys.executable, "-c",
                                 "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('once'); print('test case ... ok\\n'*900); sys.exit(5)", str(counter)], capture_output=True)
        self.assertEqual(result.returncode, 5)
        self.assertEqual(counter.read_text(), "once")
        self.assertNotIn(b"artifact:", result.stdout)

    def test_loop_warning_is_scoped_resettable_and_never_stops_execution(self):
        with patch.dict(os.environ, {"ORK_CODEX_SESSION": "context-A"}):
            for i in range(3):
                out, err = io.BytesIO(), io.BytesIO()
                self.assertEqual(run([sys.executable, "-c", "import sys; sys.exit(9)"], store=self.store, stdout=out, stderr=err), 9)
            self.assertIn(b"three times", err.getvalue())
        self.assertEqual(self.store.failure("other-context", True), 1)
        self.assertEqual(self.store.failure("other-context", False), 0)
        self.assertEqual(self.store.failure("other-context", True), 1)


class Retrieval(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.root = Path(self.temp.name)
        self.source = Sources(self.root)
        self.code = "@decorate\ndef target(x):\n    # side effect matters\n    result = x + 1\n    return result\n\nclass Service:\n    def action(self):\n        return 7\n"
        (self.root / "sample.py").write_bytes(self.code.encode())

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_ast_symbol_decorators_and_nested_names(self):
        outline = self.source.outline("sample.py")
        self.assertEqual(outline["method"], "python_ast")
        result = self.source.symbol("sample.py", "target", outline["sha256"])
        self.assertEqual(result["content"], self.code.split("\n\n")[0] + "\n")
        self.assertIn("Service.action", [s["name"] for s in outline["symbols"]])

    def test_changed_file_rejected_and_fresh_full_read_is_exact(self):
        digest = self.source.outline("sample.py")["sha256"]
        updated = self.code.replace("x + 1", "x + 2")
        (self.root / "sample.py").write_bytes(updated.encode())
        with self.assertRaises(ValueError):
            self.source.symbol("sample.py", "target", digest)
        result = self.source.read("sample.py", full=True)
        self.assertEqual(result["content"], updated)
        self.assertTrue(result["complete"])

    def test_lexical_fallback_does_not_invent_symbol_span(self):
        (self.root / "sample.ts").write_text("export function target() { return 7; }\n")
        self.assertEqual(self.source.outline("sample.ts")["method"], "lexical_non_exhaustive")
        with self.assertRaises(ValueError):
            self.source.symbol("sample.ts", "target")

    def test_parser_and_range_coordinates_preserve_non_newline_separators(self):
        for newline in ("\n", "\r\n", "\r"):
            content = newline.join(["# heading\fpage break", 'heading = "separator\u2028inside string"', "@decorate", "def target():", "    return 7", ""])
            (self.root / "physical.py").write_bytes(content.encode())
            span = self.source.symbol("physical.py", "target")
            self.assertEqual(span["content"], newline.join(["@decorate", "def target():", "    return 7", ""]))
            self.assertEqual(span["total_lines"], 5)
            search = self.source.search("return 7", ["physical.py"])
            self.assertEqual(search["matches"][0]["line"], 5)
            self.assertEqual(self.source.read("physical.py", 5, 1)["content"], "    return 7" + newline)

    def test_symbol_resolution_checks_entire_index_before_display_truncation(self):
        fillers = "".join(f"def filler_{i}():\n    return {i}\n" for i in range(499))
        duplicate = 'def target():\n    return "first"\n' + fillers + 'def target():\n    return "second"\n'
        (self.root / "large.py").write_bytes(duplicate.encode())
        outline = self.source.outline("large.py")
        self.assertEqual(len(outline["symbols"]), 500)
        self.assertTrue(outline["truncated"])
        with self.assertRaises(ValueError):
            self.source.symbol("large.py", "target")
        unique = duplicate.replace('def target():\n    return "second"', 'def final_target():\n    return "second"')
        (self.root / "large.py").write_bytes(unique.encode())
        final = self.source.symbol("large.py", "final_target")
        self.assertEqual(final["content"], 'def final_target():\n    return "second"\n')

    def test_escape_and_invalid_ranges_refused(self):
        for path in ("../elsewhere", str(ROOT / "plugin.json")):
            with self.assertRaises(ValueError):
                self.source.read(path)
        for start, count in ((0, 1), (1, 0), (1, 10001)):
            with self.assertRaises(ValueError):
                self.source.read("sample.py", start, count)

    def test_search_counts_scope_and_batch_errors(self):
        result = self.source.search("return", ["sample.py"], limit=1)
        self.assertEqual(result["total_matches"], 2)
        self.assertTrue(result["truncated"])
        service = Service(Store(self.root / "state"), self.root)
        result = service.call("source", {"op": "batch", "reads": [{"path": "sample.py", "count": 2}, {"path": "missing"}]})
        self.assertEqual(result["results"][0]["content"], "@decorate\ndef target(x):\n")
        self.assertIn("error", result["results"][1])

    def test_installed_service_requires_project_scope_and_keeps_two_projects_separate(self):
        one, two = self.root / "one", self.root / "two"
        one.mkdir()
        two.mkdir()
        (one / "fixture.py").write_bytes(b"def target():\n    return 1\n")
        (two / "fixture.py").write_bytes(b"def target():\n    return 2\n")
        raw = b"test fixture ... ok\n" * 800 + b"SUMMARY: passed\n"
        (one / "fixture.log").write_bytes(raw)
        with patch.dict(os.environ, {"PLUGIN_ROOT": str(ROOT), "ORK_CODEX_WORKSPACE": "", "ORK_CODEX_STATE": ""}):
            service = Service()
            with self.assertRaises(ValueError):
                service.call("source", {"op": "read", "path": "plugin.json"})
            a = service.call("source", {"op": "read", "path": "fixture.py", "workspace": str(one)})
            b = service.call("source", {"op": "read", "path": "fixture.py", "workspace": str(two)})
            self.assertIn("return 1", a["content"])
            self.assertIn("return 2", b["content"])
            compressed = service.call("source", {"op": "compact", "path": "fixture.log", "kind": "test", "workspace": str(one), "run_id": "one-run"})
            self.assertEqual(compressed["run_id"], "one-run")
            with self.assertRaises(OSError):
                service.call("artifact", {"handle": compressed["artifact"], "workspace": str(two), "full": True})
            recovered = service.call("artifact", {"handle": compressed["artifact"], "workspace": str(one), "full": True})
            self.assertEqual(recovered["content"].encode(), raw)
            off = service.call("source", {"op": "compact", "path": "fixture.log", "kind": "test", "workspace": str(one), "mode": "off"})
            self.assertEqual(off["content"].encode(), raw)

    def test_audit_never_prints_config_values(self):
        config = self.root / "codex"
        config.mkdir()
        (config / "config.toml").write_text('[mcp_servers.example]\ncommand="PRIVATE_SECRET"\n')
        (self.root / "AGENTS.md").write_text("local instructions")
        result = audit(self.root, config)
        self.assertEqual(result["mcp_server_names"], ["example"])
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))


class Protocol(unittest.TestCase):
    def test_cancelled_compaction_does_not_record_successful_emission(self):
        with WorkspaceTemp() as temp:
            root = Path(temp)
            store = Store(root / "state")
            (root / "fixture.log").write_bytes(b"test fixture ... ok\n" * 1000 + b"SUMMARY: passed\n")
            started, release = threading.Event(), threading.Event()
            original = Service.call
            def gated(service, name, arguments):
                started.set()
                release.wait(3)
                return original(service, name, arguments)
            request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "source", "arguments": {"op": "compact", "kind": "test", "path": "fixture.log", "workspace": temp}}}
            class Input:
                def __iter__(self):
                    yield json.dumps(request) + "\n"
                    if not started.wait(2):
                        raise AssertionError("worker did not begin")
                    yield json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}}) + "\n"
                    release.set()
            out = io.StringIO()
            with patch.object(Service, "call", gated), patch("sys.stdin", Input()), patch("sys.stdout", out):
                serve(store)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(store.report()["modes"], {})
            self.assertEqual(store.report()["excluded_delivery_states"], {"cancelled": 1})

    def test_successful_mcp_emission_counts_actual_rendered_content(self):
        with WorkspaceTemp() as temp:
            root = Path(temp)
            store = Store(root / "state")
            raw = b"test fixture ... ok\n" * 1000 + b"SUMMARY: passed\n"
            (root / "fixture.log").write_bytes(raw)
            request = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "source", "arguments": {"op": "compact", "kind": "test", "path": "fixture.log", "workspace": temp}}}
            out = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps(request) + "\n")), patch("sys.stdout", out):
                serve(store)
            reply = json.loads(out.getvalue())
            content = reply["result"]["content"][0]["text"]
            stats = store.report()["modes"]["compact"]
            self.assertEqual(stats["bytes_delivered"], len(content.encode()))
            self.assertEqual(stats["bytes_before"], len(raw))
            self.assertIsNone(json.loads(content)["bytes_delivered"])

    def test_unexpected_worker_failure_returns_one_error_without_private_details(self):
        request = {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "report", "arguments": {"op": "savings"}}}
        out = io.StringIO()
        with patch.object(Service, "call", side_effect=RuntimeError("PRIVATE_DIAGNOSTIC")), patch("sys.stdin", io.StringIO(json.dumps(request) + "\n")), patch("sys.stdout", out):
            serve()
        replies = out.getvalue().splitlines()
        self.assertEqual(len(replies), 1)
        self.assertTrue(json.loads(replies[0])["result"]["isError"])
        self.assertNotIn("PRIVATE_DIAGNOSTIC", replies[0])

    @unittest.skipUnless(os.name == "nt", "native Windows console process-tree fixture")
    def test_windows_ctrl_break_cleans_process_tree_and_keeps_partial_output(self):
        with WorkspaceTemp() as temp:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/windows_cancel_observer.py"), temp],
                                    capture_output=True, creationflags=subprocess.DETACHED_PROCESS, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(result.stdout)
            self.assertEqual(evidence["wrapper_exit"], 130)
            self.assertTrue(evidence["partial_output"])
            self.assertTrue(evidence["parent_interrupted"])
            self.assertTrue(evidence["descendant_interrupted"])

    def test_malformed_and_valid_source_calls_all_finish_on_same_stdio_process(self):
        with WorkspaceTemp() as temp:
            workspace = Path(temp)
            (workspace / "broken.py").write_text("def broken(:\n")
            (workspace / "valid.py").write_text("def valid():\n    return 7\n")
            requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}]
            for id_, arguments in ((2, {"op": "outline", "path": "broken.py"}),
                                   (3, {"op": "symbol", "path": "broken.py", "name": "broken"}),
                                   (4, {"op": "outline", "path": "valid.py"})):
                requests.append({"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": {"name": "source", "arguments": arguments}})
            requests.append({"jsonrpc": "2.0", "id": 5, "method": "ping"})
            result = subprocess.run([sys.executable, "-I", "-B", str(ENTRY), "--state", str(workspace / "state"), "serve"],
                                    input=("\n".join(json.dumps(r) for r in requests)+"\n").encode(),
                                    env=dict(os.environ, ORK_CODEX_WORKSPACE=temp), capture_output=True, cwd=ROOT, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(sorted(r["id"] for r in replies), [1, 2, 3, 4, 5])
            indexed = {r["id"]: r for r in replies}
            malformed = json.loads(indexed[2]["result"]["content"][0]["text"])
            self.assertEqual(malformed["method"], "lexical_fallback_incomplete_python")
            self.assertTrue(indexed[3]["result"]["isError"])
            valid = json.loads(indexed[4]["result"]["content"][0]["text"])
            self.assertEqual(valid["symbols"][0]["name"], "valid")

    @unittest.skipIf(os.name == "nt", "POSIX signal fixture; Windows has a separate native process-tree test")
    def test_wrapper_interrupt_keeps_partial_output_and_signal_status(self):
        with WorkspaceTemp() as temp:
            marker = Path(temp) / "started"
            proc = subprocess.Popen([sys.executable, "-I", "-B", str(ENTRY), "--state", str(Path(temp) / "state"),
                                     "run", "--kind", "log", "--", sys.executable,
                                     str(ROOT / "tests/fixtures/process_tree.py"), "parent", temp],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not (Path(temp) / "descendant.started").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((Path(temp) / "descendant.started").exists())
                proc.send_signal(signal.SIGINT)
                out, err = proc.communicate(timeout=8)
                self.assertEqual(proc.returncode, 130)
                self.assertIn(b"before interruption", out)
                self.assertTrue((Path(temp) / "parent.ended").exists())
                self.assertTrue((Path(temp) / "descendant.ended").exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_stdio_concurrent_requests_schema_errors_and_clean_stdout(self):
        with WorkspaceTemp() as temp:
            requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "source", "arguments": {"op": "read", "path": "plugin.json"}}},
                        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "source", "arguments": {"op": "read", "path": "plugin.json", "count": -1}}},
                        {"jsonrpc": "2.0", "id": 5, "method": "missing"},
                        {"jsonrpc": "2.0", "id": 6, "method": "ping"}]
            result = subprocess.run([sys.executable, "-I", "-B", str(ENTRY), "--state", str(Path(temp) / "state"), "serve"],
                                    input=("\n".join(json.dumps(r) for r in requests)+"\n").encode(), capture_output=True, cwd=ROOT, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = {r["id"]: r for r in map(json.loads, result.stdout.splitlines())}
            self.assertEqual(set(replies), {1, 2, 3, 4, 5, 6})
            self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-11-25")
            self.assertEqual(len(replies[2]["result"]["tools"]), 3)
            content = json.loads(replies[3]["result"]["content"][0]["text"])
            self.assertEqual(content["content"], (ROOT / "plugin.json").read_text())
            self.assertTrue(replies[4]["result"]["isError"])
            self.assertEqual(replies[5]["error"]["code"], -32601)

    def test_legacy_hook_is_silent_for_every_mode_and_input(self):
        cmd = [sys.executable, "-I", "-B", str(ENTRY), "hook"]
        result = subprocess.run(cmd, input=b"malformed", capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        a = subprocess.run(cmd, input=b'{"session_id":"one"}', capture_output=True).stdout
        b = subprocess.run(cmd, input=b'{"session_id":"two"}', capture_output=True).stdout
        self.assertEqual(a, b)
        self.assertEqual(a, b"")
        result = subprocess.run(cmd, input=b"{}", env=dict(os.environ, ORK_CODEX_MODE="off"), capture_output=True)
        self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
