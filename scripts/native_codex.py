"""Installed-client qualification in a separate, workspace-contained profile.

No real account, production configuration, provider key or inference is used.
CODEX_HOME identifies this test client's actual profile only in child processes;
the parent environment and the user's normal Codex profile are unchanged.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
try:
    from .model_policy import AUTHORIZED_MODELS, require_model
except ImportError:
    from model_policy import AUTHORIZED_MODELS, require_model

ROOT = Path(__file__).resolve().parents[1]


def snapshot(destination, with_mcp=False):
    destination.mkdir(parents=True)
    # Cache lives next to this snapshot, never inside the source being copied.
    # Runtime/test state and Git metadata are not plugin components.
    for name in ("plugin.json", "LICENSE"):
        shutil.copy2(ROOT / name, destination / name)
    for name in (".codex-plugin", ".claude-plugin", "ork_codex", "hooks"):
        shutil.copytree(ROOT / name, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (destination / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/ork_codex.py", destination / "scripts/ork_codex.py")
    # This qualification explicitly tests the optional MCP adapter in isolation.
    # The installed default package registers no MCP server.
    if with_mcp:
        shutil.copy2(ROOT / "docs/optional-mcp/.mcp.json", destination / ".mcp.json")
        shutil.copy2(ROOT / "docs/optional-mcp/mcp.json", destination / "mcp.json")
        manifest = destination / ".codex-plugin/plugin.json"
        config = json.loads(manifest.read_text())
        config["mcpServers"] = "./.mcp.json"
        manifest.write_text(json.dumps(config, indent=2) + "\n")
    return {str(path.relative_to(destination)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in destination.rglob("*") if path.is_file()}


class Client:
    def __init__(self, binary, environment, workspace, diagnostics):
        self.log = diagnostics.open("w", encoding="utf-8")
        self.process = subprocess.Popen([binary, "-c", "features.hooks=true", "-c", "features.plugins=true", "app-server", "--stdio"], env=environment,
                                        cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.log, text=True, encoding="utf-8")
        self.received = queue.Queue()
        self.notifications = []
        self.request_id = 0
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        for line in self.process.stdout:
            try:
                self.received.put(json.loads(line))
            except ValueError:
                continue
        self.received.put({"client_eof": True})

    def send(self, message):
        method, params = message.get("method"), message.get("params", {})
        if method in {"thread/start", "thread/resume", "thread/fork"}:
            require_model("codex", params.get("model"))
        elif method == "turn/start" and "model" in params:
            require_model("codex", params["model"])
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method, params, timeout=30):
        self.request_id += 1
        id_ = self.request_id
        self.send({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self.received.get(timeout=max(0.01, deadline-time.monotonic()))
            if message.get("client_eof"):
                raise RuntimeError("native client ended before responding; inspect its local diagnostics")
            if message.get("id") == id_:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]
            self.notifications.append(message)

    def close(self):
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.reader.join(timeout=1)
        self.process.stdout.close()
        self.log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/native-codex-latest.json")
    parser.add_argument("--diagnose-mcp", action="store_true", help="instrument the isolated snapshot only")
    parser.add_argument("--with-mcp", action="store_true", help="explicitly qualify the optional adapter")
    parser.add_argument("--mode", choices=["compact", "shadow", "off"], default="compact")
    args = parser.parse_args()
    if not args.codex.is_absolute() or not args.codex.is_file():
        parser.error("provide the absolute installed Codex executable")
    base = ROOT / ".local/native-codex" / uuid.uuid4().hex
    package, profile, work = (base / name for name in ("package", "profile", "work"))
    profile.mkdir(parents=True)
    work.mkdir()
    (profile / "config.toml").write_text('model = "gpt-6.1-sol"\nmodel_provider = "fixture"\n[model_providers.fixture]\nname = "local qualification only"\nbase_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n', encoding="utf-8")
    hashes = snapshot(package, args.with_mcp)
    if args.diagnose_mcp:
        entry = package / "scripts/ork_codex.py"
        original = entry.read_text()
        prefix = "import os,json,pathlib\npathlib.Path(DIAGNOSTIC_FILE).write_text(json.dumps({'cwd':os.getcwd(),'names':sorted(os.environ),'paths':{k:v for k,v in os.environ.items() if k in ['CODEX_CWD','CODEX_THREAD_CWD','CODEX_WORKSPACE_ROOT','MCP_WORKSPACE_ROOT','PLUGIN_ROOT','CLAUDE_PLUGIN_ROOT']}},indent=2))\n".replace("DIAGNOSTIC_FILE", repr(str(profile / "mcp-environment.json")))
        entry.write_text(prefix + original, encoding="utf-8")
        mcp = package / "ork_codex/mcp.py"
        original = mcp.read_text()
        diagnostic = '                request = json.loads(line)\n                if request.get("method") == "initialize":\n                    Path(DIAGNOSTIC_FILE).write_text(json.dumps(request,indent=2))'.replace("DIAGNOSTIC_FILE", repr(str(profile / "mcp-initialize.json")))
        mcp.write_text(original.replace('                request = json.loads(line)', diagnostic), encoding="utf-8")
    # Standard profile isolation, no sandbox or hook-trust bypass flags.
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"}}
    environment["CODEX_HOME"] = str(profile)
    environment["ORK_CODEX_RUN"] = base.name
    environment["ORK_CODEX_MODE"] = args.mode
    environment["RUST_LOG"] = "codex_rmcp_client=debug,codex_core::mcp_connection_manager=debug"
    evidence = {"schema": "ork-codex-native-install-v1", "profile": str(profile),
                "source_hashes": hashes, "diagnostic_snapshot": args.diagnose_mcp,
                "paid_requests": 0, "real_profile_changed": False, "mode": args.mode}
    def cli(*argv):
        result = subprocess.run([str(args.codex), *argv], cwd=work, env=environment,
                                capture_output=True, timeout=40)
        if result.returncode:
            (base / "cli-error.txt").write_bytes(result.stderr)
            raise RuntimeError("native CLI operation failed; see local cli-error.txt")
        return result.stdout.decode("utf-8")
    client = None
    try:
        evidence["version"] = cli("--version").strip()
        evidence["feature_defaults"] = [line for line in cli("features", "list").splitlines()
                                        if line.split() and line.split()[0] in {"hooks", "plugins"}]
        evidence["marketplace"] = json.loads(cli("plugin", "marketplace", "add", str(package), "--json"))
        evidence["installation"] = json.loads(cli("plugin", "add", "ork-meter-codex@edos-codex", "--json"))
        client = Client(str(args.codex), environment, work, base / "app-server.stderr")
        evidence["initialize"] = client.request("initialize", {"clientInfo": {"name": "ork_native_install", "version": "0.1.0"},
                                                                "capabilities": {"experimentalApi": True, "explicitGatewayOauth": True}})
        client.send({"jsonrpc": "2.0", "method": "initialized"})
        evidence["plugin_read"] = client.request("plugin/read", {"marketplacePath": str(package / ".claude-plugin/marketplace.json"), "pluginName": "ork-meter-codex"})
        assert evidence["plugin_read"]["plugin"]["skills"] == [], "plugin registered visible skills"
        assert evidence["plugin_read"]["plugin"]["hooks"] == [], "plugin registered startup instructions"
        assert evidence["plugin_read"]["plugin"]["mcpServers"] == (["ork_codex"] if args.with_mcp else []), "unexpected MCP registration"
        evidence["hooks_list"] = client.request("hooks/list", {"cwds": [str(work)]})
        (work / "fixture.py").write_bytes(b"def target():\n    return 7\n")
        evidence["thread_start"] = client.request("thread/start", {"cwd": str(work), "model": "gpt-6.1-sol", "modelProvider": "fixture", "sandbox": "workspace-write", "approvalPolicy": "never", "ephemeral": True})
        evidence["skills_list"] = client.request("skills/list", {"cwds": [str(work)], "forceReload": True})
        for entry in evidence["skills_list"]["data"]:
            assert not any(skill["name"] in {"ork-meter-codex:connect", "ork-meter-codex:disconnect", "ork-meter-codex:optimize", "ork-meter-codex:status"} or
                           "/plugins/cache/edos-codex/ork-meter-codex/" in str(skill.get("path", "")).replace("\\", "/")
                           for skill in entry["skills"]), "installed session still exposes plugin skills"
        thread_id = evidence["thread_start"]["thread"]["id"]
        evidence["mcp_status"] = client.request("mcpServerStatus/list", {"threadId": thread_id})
        if not args.with_mcp:
            assert not any(server.get("name") == "ork_codex" for server in evidence["mcp_status"].get("data", [])), "default session loaded Ork Meter schemas"
            installed = Path(evidence["installation"]["installedPath"])
            entry = installed / "scripts/ork_codex.py"
            def local(*argv):
                result = subprocess.run([shutil.which("python"), "-I", "-B", str(entry), *argv], cwd=work, env=environment, capture_output=True, timeout=15)
                assert result.returncode == 0, result.stderr
                return result.stdout
            source_read = json.loads(local("source", "--workspace", str(work), json.dumps({"op": "read", "path": "fixture.py"})))
            assert source_read["content"] == "def target():\n    return 7\n"
            raw = ("test case ... ok\n" * 1000 + "--- FAIL: fixture\nexpected 402; received 500\nstack: fixture.py:2\n").encode()
            (work / "fixture.log").write_bytes(raw)
            wrapped = local("run", "--workspace", str(work), "--kind", "test", "--", shutil.which("python"), "-c", "import sys;from pathlib import Path;sys.stdout.buffer.write(Path('fixture.log').read_bytes())")
            match = re.search(rb"artifact:([a-f0-9]{64}); delivery:([a-f0-9]{32})", wrapped)
            if args.mode == "compact":
                assert match and len(wrapped) < len(raw)
                assert b"expected 402; received 500" in wrapped
            else:
                assert wrapped == raw
            if match:
                recovered = local("artifact", match[1].decode(), "--workspace", str(work), "--intervention", match[2].decode(), "--op", "raw")
                assert recovered == raw, "installed CLI recovery changed original bytes"
            evidence["report"] = json.loads(local("savings", "--workspace", str(work)))
            evidence["default_cli_verified"] = True
            print("Installed default verified zero skills, hooks and MCP servers, plus CLI retrieval, reduction, exact recovery and reporting.")
            return
        def tool(name, arguments, task_id=thread_id):
            response = client.request("mcpServer/tool/call", {"threadId": task_id, "server": "ork_codex", "tool": name, "arguments": arguments})
            if response.get("isError"):
                raise RuntimeError(response)
            return json.loads(response["content"][0]["text"])
        evidence["source_read"] = tool("source", {"op": "read", "path": "fixture.py", "workspace": str(work)})
        assert evidence["source_read"]["content"] == "def target():\n    return 7\n"
        raw = ("test case ... ok\n" * 1000 + "--- FAIL: native_fixture\nexpected 402; received 500\nstack: fixture.py:2\n1000 passed, 1 failed\n").encode()
        (work / "fixture.log").write_bytes(raw)
        compacted = tool("source", {"op": "compact", "path": "fixture.log", "kind": "test", "workspace": str(work), "mode": args.mode, "run_id": base.name})
        if args.mode == "compact":
            assert compacted["reduced"] and not compacted["shadow"]
            assert compacted["run_id"] == base.name, "native MCP lost explicit run identity"
            assert len(compacted["content"].encode()) < len(raw)
        else:
            assert compacted["content"].encode() == raw, "native MCP ignored the explicit mode opt-out"
        evidence["reduction"] = {k: v for k, v in compacted.items() if k != "content"}
        if compacted.get("artifact"):
            recovered = tool("artifact", {"handle": compacted["artifact"], "intervention": compacted["intervention"], "full": True, "workspace": str(work)})
            assert recovered["content"].encode() == raw, "installed native recovery did not return exact original"
            evidence["recovery"] = {k: v for k, v in recovered.items() if k != "content"}
        evidence["report"] = tool("report", {"op": "savings", "workspace": str(work)})
        other = base / "other-work"
        other.mkdir()
        (other / "fixture.py").write_bytes(b"def target():\n    return 99\n")
        started_other = client.request("thread/start", {"cwd": str(other), "model": "gpt-6.1-sol", "modelProvider": "fixture", "sandbox": "workspace-write", "approvalPolicy": "never", "ephemeral": True})
        other_id = started_other["thread"]["id"]
        other_read = tool("source", {"op": "read", "path": "fixture.py", "workspace": str(other)}, other_id)
        assert other_read["content"] == "def target():\n    return 99\n"
        evidence["two_workspaces"] = {"different_source": other_read["content"] != evidence["source_read"]["content"]}
        if compacted.get("artifact"):
            wrong_scope = client.request("mcpServer/tool/call", {"threadId": other_id, "server": "ork_codex", "tool": "artifact", "arguments": {"handle": compacted["artifact"], "workspace": str(other), "full": True}})
            assert wrong_scope.get("isError"), "native artifact state crossed project boundaries"
            evidence["two_workspaces"]["artifact_isolation"] = True
        unicode_text = "# π and 界\ndef target():\n    return 7\n"
        (work / "unicode.py").write_bytes(unicode_text.encode("utf-8"))
        assert tool("source", {"op": "read", "path": "unicode.py", "workspace": str(work)})["content"] == unicode_text
        evidence["unicode_transport"] = True
        if args.mode == "compact":
            installed = Path(evidence["installation"]["installedPath"])
            wrapped = subprocess.run([shutil.which("python"), "-I", "-B", str(installed / "scripts/ork_codex.py"),
                                      "run", "--workspace", str(work), "--kind", "test", "--", shutil.which("python"),
                                      "-c", "import sys; from pathlib import Path; sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())", "fixture.log"],
                                     cwd=work, env=environment, capture_output=True, timeout=15)
            assert wrapped.returncode == 0, wrapped.stderr
            match = re.search(rb"artifact:([a-f0-9]{64}); delivery:([a-f0-9]{32})", wrapped.stdout)
            assert match, "native installed CLI wrapper did not produce a recovery handle"
            shared = tool("artifact", {"handle": match[1].decode(), "intervention": match[2].decode(), "workspace": str(work), "full": True})
            assert shared["content"].encode() == raw
            evidence["shell_mcp_shared_recovery"] = True
        evidence["hooks_supported_in_this_client"] = bool(evidence["hooks_list"]["data"][0]["hooks"])
        evidence["native_tools_verified"] = True
        effective = client.request("config/read", {"includeLayers": True, "cwd": str(work)})
        evidence["effective_mcp_config"] = effective.get("config", {}).get("mcp_servers", {})
        evidence["notifications"] = client.notifications
        print("Installed Codex verified no packaged skills or startup hooks, project retrieval, mode control, exact recovery and reporting.")
    finally:
        if client:
            client.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
