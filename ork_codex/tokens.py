"""Offline, pinned payload token gate; never a provider-usage measurement.

The vocabulary ships as public package data. Loading it does not fetch data,
change environment variables, or resolve a model name. Missing dependencies or
corrupt data disable reduction, preserving the original output.
"""
import base64
from functools import lru_cache
import gzip
import hashlib
import importlib.metadata
from pathlib import Path

VERSION = "0.9.0"
VOCAB_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
VOCABULARY = Path(__file__).with_name("data") / "o200k_base.tiktoken.gz"
PATTERN = (
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?"
    r"|[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?"
    r"|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n/]*|\s*[\r\n]+|\s+(?!\S)|\s+"
)


@lru_cache(maxsize=1)
def encoding():
    if importlib.metadata.version("tiktoken") != VERSION:
        raise ValueError("pinned tiktoken dependency unavailable")
    raw = gzip.decompress(VOCABULARY.read_bytes())
    if hashlib.sha256(raw).hexdigest() != VOCAB_SHA256:
        raise ValueError("token vocabulary integrity check failed")
    ranks = {base64.b64decode(token, validate=True): int(rank)
             for token, rank in (line.split() for line in raw.splitlines())}
    import tiktoken
    return tiktoken.Encoding("o200k_base", pat_str=PATTERN, mergeable_ranks=ranks,
                            special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018})


def count(data):
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    # Very long pre-tokenization pieces can make BPE quadratic. Such outputs
    # remain exact rather than stalling a command's completed delivery.
    if len(text) > 16 * 1024 * 1024:
        raise ValueError("output exceeds offline tokenization bound")
    encoder = encoding()
    import regex
    if any(match.end()-match.start() > 16384 for match in regex.finditer(PATTERN, text)):
        raise ValueError("output exceeds offline tokenization bound")
    return len(encoder.encode_ordinary(text))


def savings(before, after, reserve):
    try:
        original, candidate = count(before), count(after)
    except (ImportError, OSError, ValueError, RuntimeError, EOFError):
        return None
    return {"tokenizer": "o200k_base", "original_tokens": original,
            "candidate_tokens": candidate, "reserved_tokens": reserve,
            "eligible": original - candidate >= reserve and original > candidate}
