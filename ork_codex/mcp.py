"""Dependency-free MCP stdio server; retrieval only, no arbitrary commands."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import threading
from . import __version__
from .audit import audit
from .meter import status
from . import entitlement
from .policy import Policy
from .reduce import deliver
from .retrieval import Sources, artifact
from .storage import Store, workspace_state
from .delivery import ToolResult, OutputDeliveryError
from .tokens import savings as token_savings

PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def field(type_, **kwargs):
    return {"type": type_, **kwargs}


TOOLS = [
    {"name": "source", "description": "Read exact project source, outline symbols, search text, batch reads, or compact logs. Set workspace to the absolute active project directory when installed as a plugin.",
     "inputSchema": schema({"op": field("string", enum=["read", "outline", "symbol", "search", "batch", "compact"]),
                            "workspace": field("string"), "mode": field("string", enum=["compact", "shadow", "off"]),
                            "run_id": field("string"), "path": field("string"), "name": field("string"), "query": field("string"),
                            "start": field("integer", minimum=1), "count": field("integer", minimum=1, maximum=10000),
                            "full": field("boolean"), "expected_sha256": field("string"), "kind": field("string", enum=["test", "build", "log", "search", "git", "unknown"]),
                            "paths": field("array", items=field("string"), maxItems=100),
                            "reads": field("array", items=schema({"path": field("string"), "start": field("integer", minimum=1), "count": field("integer", minimum=1, maximum=10000), "expected_sha256": field("string")}, ["path"]), maxItems=20)}, ["op"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "artifact", "description": "Recover an original result by immutable handle: exact line ranges, literal search, or JSON pointer projection. Originals are never rerun.",
     "inputSchema": schema({"handle": field("string"), "op": field("string", enum=["expand", "search", "json"]),
                            "workspace": field("string"), "intervention": field("string"),
                            "start": field("integer", minimum=1), "count": field("integer", minimum=1, maximum=10000),
                            "full": field("boolean"), "query": field("string"), "pointers": field("array", items=field("string"), maxItems=100)}, ["handle"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "report", "description": "Show local estimated output savings, audit context overhead, or check an Ork Meter Codex receipt. Native snapshots are non-additive.",
     "inputSchema": schema({"op": field("string", enum=["savings", "audit", "status"]), "workspace": field("string"), "reference": field("string")}, ["op"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": True}}
]


def validate(value, spec):
    type_ = spec.get("type")
    checks = {"object": lambda x: isinstance(x, dict), "array": lambda x: isinstance(x, list),
              "string": lambda x: isinstance(x, str), "integer": lambda x: type(x) is int,
              "boolean": lambda x: type(x) is bool}
    if type_ in checks and not checks[type_](value):
        raise ValueError("invalid tool argument type")
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError("invalid tool operation")
    if type_ == "object":
        properties = spec["properties"]
        if any(key not in value for key in spec.get("required", [])) or any(key not in properties for key in value):
            raise ValueError("missing or unknown tool argument")
        for key in value:
            validate(value[key], properties[key])
    elif type_ == "array":
        if len(value) > spec.get("maxItems", 100):
            raise ValueError("too many tool arguments")
        for item in value:
            validate(item, spec["items"])
    elif type_ == "integer":
        if value < spec.get("minimum", value) or value > spec.get("maximum", value):
            raise ValueError("invalid tool argument range")


class Service:
    def __init__(self, store=None, workspace=None):
        self.store = store
        selected = workspace or os.environ.get("ORK_CODEX_WORKSPACE")
        # Plugin stdio processes start in the installed package, not the
        # active project; Codex 0.162.1 supplies no roots capability or cwd env.
        # Require explicit scope rather than silently reading plugin sources.
        self.sources = Sources(selected or Path.cwd()) if selected or not os.environ.get("PLUGIN_ROOT") else None
        self.policy = Policy.environment()
        self.stores = {}
        self.scope_lock = threading.Lock()

    def scope(self, workspace):
        if workspace is not None:
            path = Path(workspace)
            if not path.is_absolute() or not path.is_dir():
                raise ValueError("workspace must be the absolute existing project directory")
            return Sources(path)
        if self.sources is None:
            raise ValueError("set workspace to the absolute active Codex project directory")
        return self.sources

    def scoped_store(self, sources):
        if self.store is not None:
            return self.store
        root = workspace_state(sources.workspace, anchor=True)
        with self.scope_lock:
            if root not in self.stores:
                self.stores[root] = Store(root)
            return self.stores[root]

    def call(self, name, args):
        spec = next((t for t in TOOLS if t["name"] == name), None)
        if spec is None:
            raise ValueError("unknown tool")
        validate(args, spec["inputSchema"])
        if name == "report" and args["op"] == "status":
            return status(args.get("reference"))
        sources = self.scope(args.get("workspace"))
        if name == "artifact":
            args = dict(args)
            handle = args.pop("handle")
            args.pop("workspace", None)
            return artifact(self.scoped_store(sources), handle, **args)
        op = args["op"]
        if name == "report":
            if op == "savings":
                return self.scoped_store(sources).report()
            if op == "audit":
                return audit(sources.workspace)
        if op == "read":
            return sources.read(args["path"], args.get("start", 1), args.get("count", 200),
                                     args.get("full", False), args.get("expected_sha256"))
        if op == "outline":
            return sources.outline(args["path"])
        if op == "symbol":
            return sources.symbol(args["path"], args["name"], args.get("expected_sha256"))
        if op == "search":
            return sources.search(args["query"], args.get("paths"), min(500, args.get("count", 50)))
        if op == "batch":
            results = []
            for read in args.get("reads", []):
                try:
                    results.append(sources.read(**read))
                except (ValueError, OSError, UnicodeError) as error:
                    results.append({"path": read["path"], "error": str(error), "complete": False})
            return {"results": results}
        if op == "compact":
            path, text, digest = sources.file(args["path"])
            raw = text.encode("utf-8")
            policy = replace(self.policy, mode=args["mode"]) if "mode" in args else self.policy
            # Without Ork Meter Plus no requested mode can reduce output.
            policy = entitlement.gate(policy)
            if "query" in args:
                if not args["query"] or len(args["query"]) > 1000:
                    raise ValueError("compaction query requires 1..1000 characters of literal text")
                policy = replace(policy, keep=(args["query"],))
            content, info = deliver(raw, args.get("kind", "unknown"), self.scoped_store(sources), policy,
                                    args.get("full", False), args.get("run_id"))
            result = {"content": content.decode("utf-8"), "source_sha256": digest, "workspace": str(sources.workspace), **info}
            if info.get("reduced") and not info.get("shadow"):
                # Include both supported result serializations, so a saving in
                # the inner text cannot be erased by JSON metadata/envelopes.
                gates = [token_savings(raw, json.dumps(result, ensure_ascii=False, indent=indent) + "\n",
                                      policy.min_tokens_saved) for indent in (None, 2)]
                if any(gate is None or not gate["eligible"] for gate in gates):
                    info.abandon()
                    return {"content": text, "source_sha256": digest, "workspace": str(sources.workspace),
                            "reduced": False, "reason": "serialized result does not meet token savings reserve"}
            return ToolResult(result, info if hasattr(info, "confirm") else None)
        raise ValueError("unsupported source operation")


def serve(store=None):
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")
    service = Service(store)
    lock = threading.Lock()
    active = {}
    def emit(response, receipt=None, content_bytes=None, cancellation=None):
        with lock:
            if cancellation is not None and cancellation.is_set():
                if receipt:
                    receipt.abandon("cancelled")
                return
            wire = json.dumps(response, ensure_ascii=False) + "\n"
            phase = "write_failed"
            try:
                written = sys.stdout.write(wire)
                if written != len(wire):
                    raise OSError("short MCP output write")
                phase = "flush_failed"
                sys.stdout.flush()
            except (OSError, ValueError):
                if receipt:
                    receipt.abandon(phase)
                raise OutputDeliveryError(None)
            if receipt:
                receipt.confirm(content_bytes)
    def respond(request, cancellation):
        id_ = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        response = {"jsonrpc": "2.0", "id": id_}
        receipt, content_bytes = None, None
        if method == "initialize":
            requested = params.get("protocolVersion")
            response["result"] = {"protocolVersion": requested if requested in PROTOCOLS else "2025-11-25",
                                  "capabilities": {"tools": {"listChanged": False}},
                                  "serverInfo": {"name": "ork-meter-codex", "version": __version__}}
        elif method == "ping":
            response["result"] = {}
        elif method == "tools/list":
            response["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                result = service.call(params["name"], params.get("arguments", {}))
                rendered = json.dumps(result, ensure_ascii=False)
                receipt = getattr(result, "receipt", None)
                content_bytes = len(rendered.encode("utf-8"))
                response["result"] = {"content": [{"type": "text", "text": rendered}]}
            except (ValueError, OSError, UnicodeError, KeyError, IndexError, TypeError) as error:
                response["result"] = {"content": [{"type": "text", "text": "Retrieval unavailable: " + str(error)}], "isError": True}
            except Exception:
                response["result"] = {"content": [{"type": "text", "text": "Tool unavailable after an internal error; no result or delivery is verified."}], "isError": True}
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        emit(response, receipt, content_bytes, cancellation)
    def safe_respond(request, cancellation):
        try:
            if not cancellation.is_set():
                respond(request, cancellation)
        except OutputDeliveryError:
            # The stream may already contain part of a response. Do not send
            # a duplicate response or classify a prepared result as emitted.
            return
        except Exception:
            emit({"jsonrpc": "2.0", "id": request.get("id"),
                  "error": {"code": -32603, "message": "Internal dispatch error"}}, cancellation=cancellation)
        finally:
            with lock:
                if active.get(request.get("id")) is cancellation:
                    del active[request.get("id")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for line in sys.stdin:
            try:
                if len(line) > 1024 * 1024:
                    raise ValueError("request too large")
                request = json.loads(line)
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str) or not isinstance(request.get("params", {}), dict) or ("id" in request and not (request["id"] is None or type(request["id"]) in {str, int})):
                    raise ValueError("invalid request")
                if "id" not in request:
                    if request["method"] == "notifications/cancelled":
                        with lock:
                            event = active.get(request.get("params", {}).get("requestId"))
                            if event is not None:
                                event.set()
                    continue
                cancellation = threading.Event()
                with lock:
                    duplicate = request["id"] in active
                    if not duplicate:
                        active[request["id"]] = cancellation
                if duplicate:
                    emit({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32600, "message": "Request ID is already active"}})
                else:
                    pool.submit(safe_respond, request, cancellation)
            except (ValueError, TypeError):
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC request"}})
