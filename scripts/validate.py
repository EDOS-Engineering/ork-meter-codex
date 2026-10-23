"""Local package validation; no downloads, global config writes or releases."""
import ast
import json
import gzip
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def validate():
    portable = json.loads((ROOT / "plugin.json").read_text())
    compat = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
    assert portable["name"] == compat["name"] == "ork-meter-codex"
    assert portable["version"] == compat["version"] == "0.1.2"
    assert "skills" not in compat, "plugin must not register skills"
    assert not (ROOT / "skills").exists(), "default skills root must not be discoverable"
    assert "hooks" not in compat, "plugin must not register startup instructions"
    assert not (ROOT / "hooks/hooks.json").exists(), "default hooks must not be discoverable"
    assert "mcpServers" not in compat and "mcpServers" not in portable, "MCP must be explicitly opted into"
    assert not (ROOT / ".mcp.json").exists(), "default MCP discovery must stay disabled"
    assert not (ROOT / "mcp.json").exists(), "portable MCP discovery must stay disabled"
    for name in ("docs/optional-mcp/mcp.json", "docs/optional-mcp/.mcp.json", ".claude-plugin/marketplace.json"):
        json.loads((ROOT / name).read_text())
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    assert marketplace["plugins"][0]["source"]["path"] == "./"
    vocabulary = ROOT / "ork_codex/data/o200k_base.tiktoken.gz"
    assert hashlib.sha256(gzip.decompress(vocabulary.read_bytes())).hexdigest() == "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d", "offline vocabulary must match the pinned encoding"
    assert (ROOT / "ork_codex/data/LICENSE-tiktoken.txt").is_file(), "bundled tokenizer needs its upstream license"
    for folder in ("ork_codex", "scripts", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print("Plugin manifests, marketplace, absence of default skills/hooks/MCP, and Python syntax validated.")


if __name__ == "__main__":
    validate()
