"""Workspace-scoped retrieval with exact source versions and explicit scope."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
from .text import lines as physical_lines
from .delivery import ToolResult, RecoveryReceipt

MAX_SOURCE = 16 * 1024 * 1024
SKIP_DIRS = {".git", ".local", ".venv", "node_modules", "__pycache__", ".aws", ".ssh"}


class Sources:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()

    def file(self, name):
        path = (self.workspace / name).resolve()
        if not path.is_relative_to(self.workspace) or not path.is_file():
            raise ValueError("source must be a regular file inside the workspace")
        if path.stat().st_size > MAX_SOURCE:
            raise ValueError("source exceeds 16 MiB; use native ranged tools")
        data = path.read_bytes()
        text = data.decode("utf-8")
        if "\x00" in text:
            raise ValueError("binary source; use native tools")
        return path, text, hashlib.sha256(data).hexdigest()

    def read(self, path, start=1, count=200, full=False, expected_sha256=None):
        _, text, digest = self.file(path)
        if expected_sha256 and expected_sha256 != digest:
            raise ValueError("source changed; retrieve a fresh outline/read before editing")
        lines = physical_lines(text)
        if start < 1 or count < 1 or count > 10000:
            raise ValueError("invalid line range (count 1..10000)")
        end = len(lines) if full else min(len(lines), start - 1 + count)
        begin = 0 if full else start - 1
        return {"path": path, "sha256": digest, "start_line": begin + 1,
                "end_line": end, "total_lines": len(lines), "complete": begin == 0 and end == len(lines),
                "content": "".join(lines[begin:end]), "exact": True}

    def outline(self, path, display_limit=500):
        real, text, digest = self.file(path)
        symbols = []
        method = "lexical_non_exhaustive"
        if real.suffix == ".py":
            try:
                # Keep BOM in exact source while parsing the same physical lines.
                tree = ast.parse(text[1:] if text.startswith("\ufeff") else text)
            except SyntaxError:
                tree = None
                method = "lexical_fallback_incomplete_python"
            else:
                method = "python_ast"
            def visit(node, prefix=""):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        name = prefix + child.name
                        decorators = [d.lineno for d in child.decorator_list]
                        symbols.append({"name": name, "kind": type(child).__name__,
                                        "start_line": min([child.lineno] + decorators), "end_line": child.end_lineno})
                        visit(child, name + ".")
                    else:
                        visit(child, prefix)
            if tree is not None:
                visit(tree)
        if method != "python_ast":
            # Exact source remains available. These are lexical candidates,
            # not syntax-derived definitions or a semantic reference graph.
            pattern = re.compile(r"^\s*(?:(?:export|public|private|static|async)\s+)*(?:class|def|function|interface|struct|enum|fn|func)\s+(?:\([^)]*\)\s*)?([\w$]+)")
            for i, line in enumerate(physical_lines(text), 1):
                m = pattern.match(line)
                if m:
                    symbols.append({"name": m.group(1), "kind": "lexical_candidate", "start_line": i})
        return {"path": path, "sha256": digest, "method": method,
                "symbols": symbols if display_limit is None else symbols[:display_limit],
                "total_symbols": len(symbols), "truncated": display_limit is not None and len(symbols) > display_limit,
                "note": "Retrieve exact source and necessary context before editing."}

    def symbol(self, path, name, expected_sha256=None):
        outline = self.outline(path, display_limit=None)
        if expected_sha256 and outline["sha256"] != expected_sha256:
            raise ValueError("source changed; retrieve a fresh outline")
        matches = [s for s in outline["symbols"] if s["name"] == name]
        if len(matches) != 1 or "end_line" not in matches[0]:
            raise ValueError("exact symbol span unavailable; use search and ranged source read")
        span = matches[0]
        result = self.read(path, span["start_line"], span["end_line"] - span["start_line"] + 1,
                           expected_sha256=outline["sha256"])
        result["symbol"] = name
        return result

    def search(self, query, paths=None, limit=50):
        if not query or len(query) > 1000 or limit < 1 or limit > 500:
            raise ValueError("provide a literal query and a limit of 1..500")
        scope = "explicit paths" if paths is not None else "workspace excluding generated/private directories"
        if paths is None:
            paths = []
            for directory, dirs, files in os.walk(self.workspace, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not Path(directory, d).is_symlink())
                paths.extend(str(Path(directory, f).relative_to(self.workspace)) for f in sorted(files))
                if len(paths) > 10000:
                    raise ValueError("workspace search exceeds 10000 files; specify paths or use rg")
        matches, count, inspected, skipped = [], 0, 0, 0
        for path in paths:
            try:
                _, text, digest = self.file(path)
            except (ValueError, OSError, UnicodeError):
                skipped += 1
                continue
            inspected += 1
            for i, line in enumerate(physical_lines(text), 1):
                if query in line:
                    count += 1
                    if len(matches) < limit:
                        matches.append({"path": path, "line": i, "text": line.rstrip("\r\n")[:2000],
                                        "line_truncated": len(line) > 2000, "sha256": digest})
        return {"matches": matches, "total_matches": count, "truncated": count > limit,
                "files_inspected": inspected, "files_skipped": skipped,
                "scope": scope, "method": "literal_text"}


def artifact(store, handle, op="expand", start=1, count=200, query=None, pointers=None, full=False, intervention=None):
    if start < 1 or count < 1 or count > 10000:
        raise ValueError("invalid line range")
    data = store.get(handle)
    text = data.decode("utf-8")
    lines = physical_lines(text)
    if op == "expand":
        if start < 1 or count < 1 or count > 10000:
            raise ValueError("invalid line range")
        begin, end = (0, len(lines)) if full else (start-1, min(len(lines), start-1+count))
        result = {"artifact": handle, "start_line": begin+1, "end_line": end,
                  "total_lines": len(lines), "complete": begin == 0 and end == len(lines),
                  "content": "".join(lines[begin:end])}
    elif op == "search":
        if not query:
            raise ValueError("literal query required")
        all_found = [(i, line) for i, line in enumerate(lines, 1) if query in line]
        found = [(i, line) for i, line in all_found if i >= start]
        shown = found[:count]
        result = {"artifact": handle, "matches": [{"line": i, "text": line[:2000], "line_truncated": len(line) > 2000} for i, line in found[:count]],
                  "total_matches": len(found), "total_matches_entire_artifact": len(all_found),
                  "truncated": len(found) > count, "start_line": start,
                  "next_start_line": shown[-1][0] + 1 if len(found) > count else None,
                  "scope": "entire artifact" if start == 1 else f"artifact lines {start} onward", "method": "literal_text"}
    elif op == "json":
        source = json.loads(text)
        selected = {}
        if not pointers or len(pointers) > 100:
            raise ValueError("provide 1..100 JSON pointers")
        for pointer in pointers:
            value = source
            if pointer and not pointer.startswith("/"):
                raise ValueError("JSON pointer must begin with /")
            if re.search(r"~(?![01])", pointer):
                raise ValueError("invalid JSON pointer escape")
            for part in pointer.split("/")[1:] if pointer else []:
                part = part.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", part):
                        raise ValueError("invalid JSON array index")
                    value = value[int(part)]
                else:
                    value = value[part]
            selected[pointer] = value
        result = {"artifact": handle, "selected": selected, "complete": False, "method": "JSON pointer projection"}
    else:
        raise ValueError("unknown artifact operation")
    context = store.recovery_context(handle, intervention)
    result["recovery_attribution"] = context
    result["recovery_accounting_recorded"] = None
    result["accounting_status"] = "finalized only after output emission"
    return ToolResult(result, RecoveryReceipt(store, handle, context))
