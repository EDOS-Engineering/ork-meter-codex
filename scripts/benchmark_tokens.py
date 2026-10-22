"""Pinned, fail-closed surrogate token counts. No inference or implicit downloads."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".local/tokenizer-cache"
URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
VOCAB_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
VERSION = "0.9.0"


class Tokenizer:
    def __init__(self, name="o200k_base", *, prepare=False):
        self.encoder = None
        self.identity = {"name": name, "status": "unavailable"}
        if name == "none":
            return
        if name != "o200k_base":
            raise ValueError("only the pinned o200k_base encoding is supported")
        if importlib.metadata.version("tiktoken") != VERSION:
            raise ValueError("install requirements-benchmark.txt; tokenizer version mismatch")
        os.environ["TIKTOKEN_CACHE_DIR"] = str(CACHE)
        vocabulary = CACHE / hashlib.sha1(URL.encode()).hexdigest()
        if not prepare and (not vocabulary.is_file() or
                            hashlib.sha256(vocabulary.read_bytes()).hexdigest() != VOCAB_SHA256):
            raise ValueError("pinned vocabulary missing/corrupt; run scripts/benchmark_tokens.py --prepare")
        import tiktoken
        self.encoder = tiktoken.get_encoding(name)
        fingerprint = hashlib.sha256(json.dumps({
            "pattern": self.encoder._pat_str, "special": self.encoder._special_tokens,
            "ranks": sorted((key.hex(), rank) for key, rank in self.encoder._mergeable_ranks.items())
        }, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        self.identity = {"name": name, "status": "available", "package": "tiktoken",
                         "package_version": VERSION, "vocabulary_sha256": VOCAB_SHA256,
                         "encoding_sha256": fingerprint,
                         "provider_equivalence": "surrogate; unverified for either host/model",
                         "counting": "encode_ordinary; independent messages counted separately"}

    def count(self, text):
        if self.encoder is None:
            return None
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        text.encode("utf-8")  # Never silently replace invalid source.
        return len(self.encoder.encode_ordinary(text))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="allow vocabulary download, no model calls")
    args = parser.parse_args()
    print(json.dumps(Tokenizer(prepare=args.prepare).identity, indent=2))
