"""Deterministic local output replay: no model calls or claimed task-cost ROI."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ork_codex.policy import Policy
from ork_codex.reduce import deliver
from ork_codex.storage import Store
from ork_codex import tokens


def fixtures():
    yield "test-failure", "test", ("".join(f"test case_{i} ... ok\n" for i in range(1400)) + "--- FAIL: expiry\nexpected 402; received 500\nstack: checkout.py:72\n1400 passed, 1 failed\n").encode(), [b"expiry", b"received 500", b"checkout.py:72", b"1 failed"]
    yield "compiler", "build", ("Compiling module...\n" * 1200 + "error E042: missing required field\n --> source.rs:28:5\n 28 | initialize()\nBuild failed\n").encode(), [b"error E042", b"source.rs:28:5", b"Build failed"]
    yield "search", "search", "".join(f"path/{i}.py:{i+1}: function definition\n" for i in range(1400)).encode(), []
    yield "unknown-json", "log", json.dumps({"results": ["preserve" for _ in range(4000)]}).encode(), []
    yield "small", "log", b"All checks passed\n", []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tokenizer", choices=["none", "o200k_base"], default="none")
    args = parser.parse_args()
    count = tokens.count if args.tokenizer == "o200k_base" else (lambda text: None)
    tokenizer_identity = ({"name": "o200k_base", "status": "available", "package": "tiktoken",
                 "package_version": tokens.VERSION, "vocabulary_sha256": tokens.VOCAB_SHA256,
                 "source": "bundled ork_codex/data vocabulary"}
                if args.tokenizer == "o200k_base" else {"name": "none", "status": "unavailable"})
    results = []
    store = Store(ROOT / ".local" / "replay" / uuid.uuid4().hex)
    for budget in (4096, 8192, 16384):
        policy = Policy(budget=budget)
        for name, kind, raw, required in fixtures():
            identity = uuid.UUID(hashlib.sha256(f"{name}:{budget}".encode()).hexdigest()[:32])
            with patch("ork_codex.reduce.uuid.uuid4", return_value=identity):
                visible, info = deliver(raw, kind, store, policy, run_id=f"replay:{name}:{budget}")
            assert all(marker in visible for marker in required), "required diagnostic omitted"
            if info.get("artifact"):
                assert store.get(info["artifact"]) == raw, "exact recovery failed"
            if name in {"small", "unknown-json"}:
                assert visible == raw, "pass-through invariant failed"
            if hasattr(info, "abandon"):
                info.abandon()
            results.append({"fixture": name, "sha256": hashlib.sha256(raw).hexdigest(),
                            "budget_bytes": budget, "raw_bytes": len(raw), "candidate_bytes": len(visible),
                            "avoided_bytes": len(raw)-len(visible),
                            "raw_surrogate_tokens": count(raw),
                            "candidate_surrogate_tokens": count(visible),
                            "surrogate_tokens_avoided": (count(raw)-count(visible)) if args.tokenizer == "o200k_base" else None,
                            "reduced": info["reduced"], "exact_recovery": True, "required_diagnostics_retained": True})
    report = {"schema": "ork-codex-replay-v2", "results": results, "model_calls": 0,
              "tokenizer": tokenizer_identity,
              "synthetic_delivery_ids": True,
              "evidence": "synthetic local replay only; no end-to-end task quality, billed-token or economic savings claim"}
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"Verified {len(results)} replay cases; report: {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
