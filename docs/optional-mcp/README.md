# Optional MCP adapter

The default package registers no MCP servers. The CLI works without MCP.

Only when explicitly requested, copy both `.mcp.json` and `mcp.json` from this directory to the plugin root and set `mcpServers` to `./.mcp.json` in `.codex-plugin/plugin.json`. Paths in the example are relative to the plugin root. Reload the plugin or start a fresh session. This adds tool discovery/schema overhead; benchmark its complete-task benefit before enabling it routinely.

`scripts/native_codex.py` verifies the default installation has zero skills, hooks and MCP servers and exercises the installed CLI. Pass `--with-mcp` to enable the adapter in an isolated test snapshot. `scripts/probe_codex.py` also verifies default component discovery. Do not copy optional registration into the installed package by default.
