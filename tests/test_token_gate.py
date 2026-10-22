"""Payload eligibility and immutable-cache regressions against real encoding."""
import copy
import hashlib
import importlib.metadata
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import plus_fixture
from ork_codex import tokens
from ork_codex.mcp import Service, TOOLS
from ork_codex.policy import Policy
from ork_codex.reduce import compact, deliver
from ork_codex.storage import Store

def setUpModule():
    plus_fixture.install()  # savings levers need Ork Meter Plus


class TokenGate(unittest.TestCase):
    def test_unavailable_counter_returns_exact_original_without_artifact(self):
        raw = b"Compiling module...\n" * 1200
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/"state")
            with patch("ork_codex.tokens.count", side_effect=ImportError("missing")):
                result, info = deliver(raw, "build", store)
            self.assertEqual(result, raw)
            self.assertFalse(info["reduced"])
            self.assertEqual(list((store.root/"artifacts").iterdir()), [])

    def test_environment_token_reserve_has_its_own_bounds(self):
        for value, expected in (("0", 0), ("128", 128), ("-5", 0), ("bad", 512)):
            with patch.dict(os.environ, {"ORK_CODEX_MIN_TOKENS_SAVED": value}):
                self.assertEqual(Policy.environment().min_tokens_saved, expected)

    def test_schema_order_and_definitions_stay_fixed_across_modes_and_edits(self):
        before = copy.deepcopy(TOOLS)
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            file = work/"sample.py"
            file.write_bytes(b"def value():\n    return 7\n")
            source = Service(workspace=work)
            first = source.call("source", {"op": "symbol", "path": "sample.py", "name": "value"})
            file.write_bytes(b"def value():\n    return 99\n")
            with self.assertRaisesRegex(ValueError, "source changed"):
                source.call("source", {"op": "symbol", "path": "sample.py", "name": "value",
                                       "expected_sha256": first["sha256"]})
            for mode in ("off", "shadow", "compact"):
                source.call("source", {"op": "compact", "path": "sample.py", "mode": mode})
            self.assertEqual(TOOLS, before)
            self.assertEqual(first["content"], "def value():\n    return 7\n")
            self.assertIn("return 99", source.call("source", {"op": "symbol", "path": "sample.py", "name": "value"})["content"])

    def test_mcp_envelope_gate_rejects_prepared_reduction_without_claiming_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            raw = b"Compiling module...\n"*1200
            (work/"build.log").write_bytes(raw)
            store = Store(work/"state")
            service = Service(store, work)
            with patch("ork_codex.reduce.savings", return_value={"eligible": True}), \
                 patch("ork_codex.mcp.token_savings", return_value={"eligible": False}):
                result = service.call("source", {"op": "compact", "path": "build.log", "kind": "build"})
            self.assertEqual(result["content"].encode(), raw)
            self.assertFalse(result["reduced"])
            self.assertNotIn("artifact", result)
            self.assertFalse(store.report()["modes"])

    def test_corrupt_vocabulary_fails_without_fetching_or_environment_changes(self):
        before = dict(os.environ)
        tokens.encoding.cache_clear()
        try:
            with patch("ork_codex.tokens.importlib.metadata.version", return_value=tokens.VERSION), \
                 patch.object(Path, "read_bytes", return_value=b"bad archive"):
                self.assertIsNone(tokens.savings(b"before", b"after", 0))
            self.assertEqual(dict(os.environ), before)
        finally:
            tokens.encoding.cache_clear()


def available():
    try:
        return importlib.metadata.version("tiktoken") == tokens.VERSION
    except importlib.metadata.PackageNotFoundError:
        return False


@unittest.skipUnless(available(), "install the pinned package dependency")
class RealEncoding(unittest.TestCase):
    def test_keep_hint_retains_exact_context_without_changing_schemas(self):
        raw = (b"Compiling module...\n"*600 + b"setup preceding evidence\nunit conversion denominator: 1000\nnext context line\n" + b"Compiling module...\n"*600)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/"state")
            ordinary, info = deliver(raw, "build", store, Policy(budget=1024))
            self.assertNotIn(b"unit conversion denominator", ordinary)
            info.abandon()
            selected, info = deliver(raw, "build", store, Policy(budget=1024, keep=("unit conversion denominator",)))
            self.assertIn(b"setup preceding evidence\nunit conversion denominator: 1000\nnext context line\n", selected)
            self.assertTrue(info["reduced"])
            self.assertGreaterEqual(tokens.count(raw)-tokens.count(selected), 512)
            info.abandon()

    def test_explicit_mcp_compaction_query_retains_evidence_and_rejects_empty_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            raw = b"Compiling module...\n"*600 + b"unit conversion denominator: 1000\n" + b"Compiling module...\n"*600
            (work/"build.log").write_bytes(raw)
            service = Service(Store(work/"state"), work)
            service.policy = Policy(budget=1024)
            result = service.call("source", {"op": "compact", "path": "build.log", "kind": "build", "query": "unit conversion"})
            self.assertIn("unit conversion denominator: 1000", result["content"])
            result.receipt.abandon()
            with self.assertRaisesRegex(ValueError, "literal text"):
                service.call("source", {"op": "compact", "path": "build.log", "kind": "build", "query": ""})

    def test_bundled_encoder_matches_independent_pinned_reference(self):
        # Independent installed definition; only its existing cache may be used.
        import tiktoken
        from tiktoken_ext import openai_public
        from unittest.mock import patch
        import base64, gzip
        raw = gzip.decompress(tokens.VOCABULARY.read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), tokens.VOCAB_SHA256)
        ranks = {base64.b64decode(token): int(rank) for token, rank in
                 (line.split() for line in raw.splitlines())}
        with patch.object(openai_public, "load_tiktoken_bpe", return_value=ranks):
            definition = openai_public.o200k_base()
        reference = tiktoken.Encoding(**definition)
        for text in ("ordinary text", "\u03b1\u03b2\u03b3\r\n\U0001f603", "<|endoftext|>", " "*1000+"\n", "error E042: source.rs:28:5"):
            self.assertEqual(tokens.count(text), len(reference.encode_ordinary(text)))
        self.assertEqual(tokens.encoding()._pat_str, reference._pat_str)
        self.assertEqual(tokens.encoding()._special_tokens, reference._special_tokens)

    def test_byte_savings_that_inflate_tokens_are_rejected_including_footer(self):
        raw = (" "*1000+"\n").encode()*5
        candidate, candidate_info = compact(raw, "log", Policy(budget=1024, threshold=1024))
        self.assertTrue(candidate_info["reduced"])
        self.assertLess(len(candidate), len(raw))
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/"state")
            # Disable the reserve to prove the footer itself cannot increase tokens.
            output, info = deliver(raw, "log", store, Policy(budget=1024, threshold=1024, min_tokens_saved=0))
            self.assertEqual(output, raw)
            self.assertFalse(info["reduced"])
            self.assertEqual(list((store.root/"artifacts").iterdir()), [])

    def test_large_output_saves_tokens_after_footer_and_overhead_reserve(self):
        raw = (b"Compiling module...\n"*1200 + b"error E042: missing field\n --> source.rs:28:5\nBuild failed\n")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/"state")
            output, info = deliver(raw, "build", store)
            self.assertTrue(info["reduced"])
            self.assertGreaterEqual(tokens.count(raw)-tokens.count(output), 512)
            self.assertIn(b"source.rs:28:5", output)
            self.assertEqual(store.get(info["artifact"]), raw)
            info.abandon()

    def test_counter_bounds_preserve_pathological_output(self):
        self.assertIsNone(tokens.savings(b"a"*16385, b"tiny", 0))
        self.assertIsNone(tokens.savings((b" "*1000+b"\n")*500, b"tiny", 0))
        # Long JSON rows with ordinary short pieces must still be countable.
        self.assertGreater(tokens.count('{"items":['+'"word",'*4000+'"last"]}'), 0)


if __name__ == "__main__":
    unittest.main()
