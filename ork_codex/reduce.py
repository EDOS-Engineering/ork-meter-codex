"""Conservative deterministic reduction; diagnostics have a soft budget."""
import ast
import re
import sqlite3
import hashlib
import json
import uuid
from dataclasses import asdict
from .policy import Policy
from .text import lines as physical_lines
from .delivery import DeliveryInfo
from .evidence_formats import unsupported_line_format
from .tokens import savings

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
ERROR = re.compile(r"error|fail|panic|traceback|exception|fatal|denied|timeout|warning|expected|assert", re.I)
PASS = re.compile(r"^\s*(?:--- PASS: .+|test \S+ \.\.\. ok|\S+\.py(?:::\S+)?\s+PASSED(?:\s+\[\s*\d+%\])?|PASS\s+\S+|[✓✔√]\s+.+|\.{8,}\s+\[\s*\d+%\])\s*$")
GO_RUN = re.compile(r"^\s*=== RUN\s+(.+?)\s*$")
GO_PASS = re.compile(r"^\s*--- PASS:\s+(.+?)\s+\([^)]*\)\s*$")
BUILD_SUCCESS = re.compile(r"^\s*(?:Compiling|Building|Bundling|Transpiling) .+:\s*(?:success|ok|done)\s*$", re.I)
NINJA_PROGRESS = re.compile(r"^\[\d+/\d+\] (?:Building|Linking|Generating|Compiling)\b")
NON_PASS = re.compile(r"\b(?:skip|skipped|ignored|xfail|xfailed|xpass|pending|todo)\b", re.I)
KINDS = {"test", "build", "log", "search", "git", "source", "read"}
SEARCH_HIT = re.compile(r"^(.+?):([1-9][0-9]*):(.*)$")
READ_ANCHOR = re.compile(r"^\s{0,4}(?:async\s+def|def|class|func|function|export)\b|^#{1,6}\s")


def compact_search(data, lines, policy):
    """Keep every matched file visible in a plain, line-numbered rg result."""
    groups = {}
    last_path = None
    for index, line in enumerate(lines):
        match = SEARCH_HIT.fullmatch(line.rstrip("\r\n"))
        if not match:
            return data, {"reduced": False, "reason": "search output is not plain path:line:text"}
        path = match.group(1)
        if path != last_path and path in groups:
            return data, {"reduced": False, "reason": "search output interleaves files"}
        groups.setdefault(path, []).append(index)
        last_path = path
    if not groups:
        return data, {"reduced": False, "reason": "empty search output"}
    out = []
    omitted = 0
    for path, indices in groups.items():
        out.append(lines[indices[0]])
        if len(indices) > 2:
            count = len(indices) - 2
            out.append(f"[omitted {count} additional matches in {path}]\n")
            omitted += count
        if len(indices) > 1:
            out.append(lines[indices[-1]])
    candidate = "".join(out).encode()
    if not omitted or len(candidate) > policy.budget or len(candidate) + 256 >= len(data):
        return data, {"reduced": False, "reason": "file coverage or recovery overhead exceeds search budget"}
    return candidate, {"reduced": True, "total_lines": len(lines), "omitted_lines": omitted,
                       "search_files_preserved": len(groups), "search_matches_omitted": omitted,
                       "diagnostic_context_retained": False}


def compact_source(data, lines, policy):
    """Expose module boundaries and short symbol previews with exact line ranges."""
    try:
        tree = ast.parse(data.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError, ValueError):
        return data, {"reduced": False, "reason": "source is not parseable Python"}
    selected = set(range(min(12, len(lines))))
    selected.update(range(max(0, len(lines) - 12), len(lines)))
    symbols = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols += 1
            start = max(0, node.lineno - 2)
            selected.update(range(start, min(len(lines), node.lineno + 4)))
    if not symbols:
        return data, {"reduced": False, "reason": "no top-level Python symbols"}
    out = []
    omitted = 0
    gap_start = None
    for index, line in enumerate(lines):
        if index not in selected:
            omitted += 1
            if gap_start is None:
                gap_start = index + 1
            continue
        if gap_start is not None:
            out.append(f"[omitted source lines {gap_start}-{index}]\n")
            gap_start = None
        out.append(line)
    if gap_start is not None:
        out.append(f"[omitted source lines {gap_start}-{len(lines)}]\n")
    candidate = "".join(out).encode("utf-8")
    if not omitted or len(candidate) > policy.budget or len(candidate) + 256 >= len(data):
        return data, {"reduced": False, "reason": "symbol previews or recovery overhead exceed source budget"}
    return candidate, {"reduced": True, "total_lines": len(lines), "omitted_lines": omitted,
                       "source_symbols_preserved": symbols, "diagnostic_context_retained": False}


def compact_read(data, lines, policy):
    """Bound large direct reads while retaining declarations and exact ranges."""
    selected = set(range(min(8, len(lines))))
    selected.update(range(max(0, len(lines) - 8), len(lines)))
    anchors = 0
    for i, line in enumerate(lines):
        if READ_ANCHOR.match(line):
            anchors += 1
            selected.update(range(max(0, i - 1), min(len(lines), i + 3)))
    if not anchors:
        return data, {"reduced": False, "reason": "no bounded-read landmarks"}
    out = []
    omitted = 0
    gap = None
    for i, line in enumerate(lines):
        if i not in selected:
            omitted += 1
            if gap is None:
                gap = i + 1
            continue
        if gap is not None:
            out.append(f"[omitted read lines {gap}-{i}]\n")
            gap = None
        out.append(line)
    if gap is not None:
        out.append(f"[omitted read lines {gap}-{len(lines)}]\n")
    candidate = "".join(out).encode("utf-8")
    if not omitted or len(candidate) > policy.budget or len(candidate) + 256 >= len(data):
        return data, {"reduced": False, "reason": "landmarks or recovery overhead exceed read budget"}
    return candidate, {"reduced": True, "total_lines": len(lines), "omitted_lines": omitted,
                       "read_landmarks_preserved": anchors, "diagnostic_context_retained": False}


def compact(data, kind, policy):
    if policy.mode == "off" or len(data) < policy.threshold or kind not in KINDS:
        return data, {"reduced": False, "reason": "off, small, or unsupported"}
    try:
        original = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, {"reduced": False, "reason": "binary or unsupported encoding"}
    format_probe = original
    if kind == "build" and NINJA_PROGRESS.match(original):
        format_probe = original.split("] ", 1)[1]
    if unsupported_line_format(format_probe):
        return data, {"reduced": False, "reason": "structured, unknown or binary-like result; use exact recovery/selection"}
    lines = physical_lines(ANSI.sub("", original))
    if kind == "search":
        return compact_search(data, lines, policy)
    if kind == "source":
        return compact_source(data, lines, policy)
    if kind == "read":
        return compact_read(data, lines, policy)
    passing = {i for i, line in enumerate(lines) if kind == "test" and policy.test_filter and
               PASS.fullmatch(line.rstrip("\r\n"))}
    routine_go_runs = set()
    if kind == "test" and policy.test_filter:
        pending_go_runs = {}
        for i, line in enumerate(lines):
            run = GO_RUN.fullmatch(line.rstrip("\r\n"))
            passed = GO_PASS.fullmatch(line.rstrip("\r\n"))
            if run:
                pending_go_runs.setdefault(run.group(1), []).append(i)
            elif passed:
                routine_go_runs.update(pending_go_runs.pop(passed.group(1), ()))
    routine_build = {i for i, line in enumerate(lines) if kind == "build" and
                     (BUILD_SUCCESS.fullmatch(line.rstrip("\r\n")) or
                      NINJA_PROGRESS.match(line.rstrip("\r\n")))}
    routine = passing | routine_go_runs | routine_build
    # Explicit evidence requests override routine-output filtering. Preserve
    # their context too: otherwise --keep can force an avoidable recovery call.
    hinted = set()
    for i, line in enumerate(lines):
        if any(query in line for query in policy.keep):
            hinted.update(range(max(0, i - 3), min(len(lines), i + 9)))
    named_passes = {i for i in passing if re.fullmatch(
        r"\s*\S+\.py::\S+\s+PASSED(?:\s+\[\s*\d+%\])?\s*", lines[i])}
    if len(named_passes) <= 8:
        hinted.update(named_passes)
    else:
        # Preserve a few ordinary regression names amid a large parameter
        # sweep, without keeping hundreds of individual sweep results.
        plain_passes = {i for i in named_passes if re.fullmatch(
            r"\s*\S+\.py::[^\s\[]+\s+PASSED(?:\s+\[\s*\d+%\])?\s*", lines[i])}
        if len(plain_passes) <= 8:
            hinted.update(plain_passes)
    routine.difference_update(hinted)
    required = set(hinted)
    for i, line in enumerate(lines):
        if i in routine:
            continue
        if ERROR.search(line) or any(query in line for query in policy.keep) or (kind == "test" and NON_PASS.search(line)):
            required.update(j for j in range(max(0, i - 3), min(len(lines), i + 9)) if j not in routine)
    if kind == "test":
        first_failure = next((i for i, line in enumerate(lines)
                              if re.search(r"^\s*(?:FAILURES|=+.*FAILURES|Traceback|--- FAIL:|FAIL:|ERROR:)", line)), None)
        if first_failure is not None:
            # Keep the entire diagnostic tail, including unmarked traceback
            # context, but recognized passing test rows carry no failure data.
            required.update(i for i in range(first_failure, len(lines))
                            if i not in routine)
    selected = set(required)
    used = sum(len(lines[i].encode()) for i in selected)
    candidates = list(range(min(12, len(lines)))) + list(range(max(0, len(lines)-12), len(lines)))
    candidates += list(range(len(lines)))
    for i in candidates:
        if i in selected or i in routine:
            continue
        size = len(lines[i].encode())
        if used + size <= policy.budget:
            selected.add(i)
            used += size
    out = []
    skipped = 0
    for i, line in enumerate(lines):
        if i not in selected:
            skipped += 1
        else:
            if skipped:
                out.append(f"[omitted {skipped} lines]\n")
                skipped = 0
            out.append(line)
    if skipped:
        out.append(f"[omitted {skipped} lines]\n")
    candidate = "".join(out).encode()
    omitted = len(lines) - len(selected)
    if not omitted or len(candidate) + 256 >= len(data):
        return data, {"reduced": False, "reason": "required evidence or recovery overhead exceeds savings"}
    return candidate, {"reduced": True, "total_lines": len(lines), "omitted_lines": omitted,
                       "passing_lines_omitted": len(passing - selected),
                       "passed_go_run_lines_omitted": len(routine_go_runs - selected),
                       "routine_build_lines_omitted": len(routine_build - selected),
                       "diagnostic_context_retained": True}


def deliver(data, kind, store, policy=None, full=False, run_id=None):
    policy = policy or Policy.environment()
    if full:
        return data, {"reduced": False, "reason": "explicit full output"}
    candidate, info = compact(data, kind, policy)
    if not info["reduced"]:
        return data, info
    return deliver_prepared(data, candidate, kind, store, policy, info, run_id)


def deliver_prepared(data, candidate, kind, store, policy, info, run_id=None):
    """Publish a checked candidate while retaining the complete original result."""
    try:
        handle = hashlib.sha256(data).hexdigest()
        intervention = uuid.uuid4().hex
        policy_id = hashlib.sha256(json.dumps(asdict(policy), sort_keys=True).encode()).hexdigest()[:16]
        footer = f"\n[Full original: artifact:{handle}; delivery:{intervention}; use artifact expand/search. {info['omitted_lines']}/{info['total_lines']} lines omitted.]\n".encode()
        result = candidate + footer
        if len(result) >= len(data):
            return data, {"reduced": False, "reason": "recovery overhead exceeds savings"}
        token_gate = savings(data, result, policy.min_tokens_saved)
        if token_gate is None or not token_gate["eligible"]:
            return data, {"reduced": False, "reason": "token savings unverified or below overhead reserve"}
        published = store.put(data)
        if published != handle:
            return data, {"reduced": False, "reason": "original artifact identity mismatch"}
        visible = data if policy.mode == "shadow" else result
        store.event(kind, len(data), len(visible), mode=policy.mode, artifact=handle,
                    intervention=intervention, policy_id=policy_id, run_id=run_id, delivery_state="prepared")
        info.update({"artifact": handle, "bytes_before": len(data), "bytes_delivered": None,
                     "token_gate": token_gate,
                     "candidate_bytes": len(result), "shadow": policy.mode == "shadow",
                     "intervention": intervention, "run_id": run_id or store.run_id, "policy_id": policy_id,
                     "delivery_status": "prepared"})
        return visible, DeliveryInfo(info, store)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, AttributeError):
        return data, {"reduced": False, "reason": "artifact storage unavailable; full output returned"}
