"""Inspect always-loaded instructions and server names, never config values."""
import json
from pathlib import Path
import tomllib


def audit(workspace, codex_home=None):
    workspace = Path(workspace).resolve()
    roots = [workspace] + list(workspace.parents)
    base = Path(codex_home) if codex_home else Path.home() / ".codex"
    instructions, servers, gaps = [], set(), []
    for directory in roots + [base]:
        for name in ("AGENTS.md", "AGENTS.override.md"):
            path = directory / name
            if path.is_file():
                size = path.stat().st_size
                instructions.append({"path": str(path), "bytes": size,
                                     "estimated_tokens": size / 4})
    for path in [base / "config.toml", workspace / ".codex/config.toml"]:
        if path.is_file():
            try:
                config = tomllib.loads(path.read_text(encoding="utf-8"))
                servers.update(config.get("mcp_servers", {}))
            except (OSError, ValueError):
                gaps.append("a Codex config could not be parsed")
    return {"instructions": sorted(instructions, key=lambda x: x["bytes"], reverse=True),
            "mcp_server_names": sorted(servers), "estimate_method": "file bytes / 4",
            "scope": "current directory and ancestor instructions, user/project Codex config",
            "gaps": gaps + ["nested task-directory instructions, installed skill bodies, hosted/managed context, and enabled server schemas are not measured"]}
