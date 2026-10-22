"""Automatically compact large native Bash results after the command completes."""
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

from ork_codex import entitlement
from ork_codex.policy import Policy
from ork_codex.reduce import compact, deliver, deliver_prepared
from ork_codex.storage import Store

READ_SUFFIXES = {".py", ".go", ".rs", ".js", ".ts", ".md", ".toml", ".yaml", ".yml"}


def response_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("output", "aggregatedOutput"):
            if isinstance(value.get(key), str):
                return value[key]
    return None


def clean_go_result(text):
    """Preserve passing Go output; a compact clean run prompted extra reads live."""
    return (re.search(r"(?m)^PASS\r?$", text) is not None and
            re.search(r"(?m)^ok[ \t]+\S+[ \t]+\d", text) is not None and
            re.search(r"(?m)^\s*(?:--- FAIL:|FAIL(?:[ \t]|$)|panic:|fatal error:)", text) is None)


def command_kind(command):
    try:
        # Shell separators are operators even without surrounding spaces.
        # Keep unquoted newlines as operators rather than argv whitespace.
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()`\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return None
    if not words:
        return None
    executable = Path(words[0]).name.lower()
    if executable in {"bash", "sh", "zsh"} and len(words) == 3 and words[1] in {"-c", "-lc", "-ec"}:
        return command_kind(words[2])
    if words[0] == "cd" and len(words) >= 4 and words[2] == "&&":
        if any(re.fullmatch(r"[;&|<>()`\n]+", word) or "$(" in word or "`" in word
               for word in words[3:]):
            return None
        return command_kind(shlex.join(words[3:]))
    if any(re.fullmatch(r"[;&|<>()`\n]+", word) or "$(" in word or "`" in word for word in words):
        return None
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", executable):
        position = 1
        while position < len(words) and words[position] in {"-B", "-I", "-S", "-E", "-s", "-u", "-q"}:
            position += 1
        if position < len(words) and words[position].lower().endswith(".py"):
            return "script-log"
        if words[position:position + 1] == ["-m"]:
            words = words[position + 1:]
            executable = words[0].lower() if words else ""
    tail = [word.lower() for word in words[1:]]
    if executable in {"pytest", "unittest", "vitest", "jest"}:
        return "test"
    if executable in {"cargo", "go", "dotnet"} and tail[:1] == ["test"]:
        return "test"
    if executable in {"npm", "pnpm", "yarn", "bun"} and (tail[:1] == ["test"] or tail[:2] == ["run", "test"]):
        return "test"
    if executable in {"cargo", "go", "dotnet"} and tail[:1] in (["build"], ["check"]):
        return "build"
    if executable in {"npm", "pnpm", "yarn", "bun"} and tail[:2] == ["run", "build"]:
        return "build"
    if executable in {"tsc", "make", "ninja", "msbuild"}:
        return "build"
    if executable == "cmake" and tail[:1] == ["--build"]:
        return "build"
    if executable == "journalctl" or (executable in {"docker", "kubectl"} and tail[:1] == ["logs"]):
        return "log"
    if executable == "cat" and len(words) == 2:
        suffix = Path(words[1].lower()).suffix
        if suffix == ".py":
            return "source"
        if suffix in READ_SUFFIXES:
            return "read"
    if (executable == "rg" and any(word in {"-n", "--line-number", "--vimgrep"} for word in tail) and
            "--files" not in tail and "-l" not in tail and "--files-with-matches" not in tail):
        return "search"
    return None


def compound_source(command, tool_input, text, policy, kind="source"):
    """Find one exact file in a compound shell result."""
    try:
        outer = shlex.split(command)
        body = outer[2] if len(outer) == 3 and Path(outer[0]).name.lower() in {"bash", "sh", "zsh"} and outer[1] in {"-c", "-lc", "-ec"} else command
        lexer = shlex.shlex(body, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return None
    matches = [words[i + 1] for i in range(len(words) - 1)
               if words[i] == "cat" and Path(words[i + 1].lower()).suffix in
               ({".py"} if kind == "source" else READ_SUFFIXES) and
               (i == 0 or words[i - 1] in {";", "&&"}) and
               (i + 2 == len(words) or words[i + 2] in {";", "&&"})]
    if len(matches) != 1 or len(words) <= 2:
        return None
    directory = tool_input.get("workdir") or os.getcwd()
    if not isinstance(directory, str):
        return None
    path = Path(directory) / matches[0]
    try:
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return None
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    if not source or text.count(source) != 1:
        return None
    segment = source.encode("utf-8")
    preview, info = compact(segment, kind, policy)
    if not info.get("reduced"):
        return None
    before, after = text.split(source, 1)
    candidate = before.encode("utf-8") + preview + after.encode("utf-8")
    return candidate, info


def handle(event, state_root, trace=None):
    def decline(reason):
        if trace is not None:
            trace["reason"] = reason
        return None

    if event.get("hook_event_name") != "PostToolUse" or event.get("tool_name") != "Bash":
        return decline("not Bash PostToolUse")
    text = response_text(event.get("tool_response"))
    tool_input = event.get("tool_input")
    if not text or not isinstance(tool_input, dict):
        return decline("no text response or tool input")
    if trace is not None:
        trace["response_bytes"] = len(text.encode("utf-8"))
    command = tool_input.get("command")
    if not isinstance(command, str):
        return decline("command missing")
    kind = command_kind(command)
    compound = None
    source_on = os.environ.get("ORK_CODEX_SOURCE_FILTER", "off").lower() == "on"
    read_on = os.environ.get("ORK_CODEX_BOUNDED_READ_FILTER", "off").lower() == "on"
    if kind is None and (source_on or read_on):
        compound_kind = "source" if source_on else "read"
        compound = compound_source(command, tool_input, text, Policy.environment(), compound_kind)
        if compound is not None:
            kind = compound_kind
    if kind == "source" and not source_on and read_on:
        kind = "read"
    if trace is not None:
        trace["kind"] = kind
    if kind is None:
        return decline("unsupported command")
    if kind == "search" and os.environ.get("ORK_CODEX_SEARCH_FILTER", "off").lower() != "on":
        return decline("search filter off")
    if kind == "source" and not source_on:
        return decline("source filter off")
    if kind == "read" and not read_on:
        return decline("read filter off")
    if kind == "script-log":
        if os.environ.get("ORK_CODEX_PYTHON_LOG_FILTER", "off").lower() != "on":
            return decline("script-log filter off")
        kind = "log"
    if kind == "test" and clean_go_result(text):
        return decline("clean Go result")
    policy = Policy.environment()
    if compound is None:
        output, info = deliver(text.encode("utf-8"), kind, Store(state_root), policy)
    else:
        output, info = deliver_prepared(text.encode("utf-8"), compound[0], kind,
                                        Store(state_root), policy, compound[1])
    if not info.get("reduced") or info.get("shadow"):
        return decline(info.get("reason") or "shadow or unreduced")
    replacement = output.decode("utf-8")
    # A PostToolUse feedback result replaces the original model-facing result.
    # Do not add developer context, which would alter the cached prompt prefix.
    return {"decision": "block", "reason": replacement}, info, len(output)


def main():
    trace_path = os.environ.get("ORK_CODEX_HOOK_TRACE")
    trace = {} if trace_path else None
    started = time.monotonic()
    info = None
    try:
        event = json.load(sys.stdin)
        if trace is not None:
            response = event.get("tool_response")
            trace["response_type"] = type(response).__name__
            trace["recognized_response_keys"] = sorted(
                key for key in ("output", "aggregatedOutput") if isinstance(response, dict) and key in response)
            trace["response_field_count"] = len(response) if isinstance(response, dict) else None
            trace["is_bash_post_tool_use"] = (event.get("hook_event_name") == "PostToolUse" and
                                               event.get("tool_name") == "Bash")
        # Savings are an Ork Meter Plus feature: keep the cached answer
        # current in the background, and pass output through without Plus.
        # No verified answer means no reduction.
        entitlement.refresh()
        if not entitlement.entitled():
            if trace is not None:
                trace["reason"] = "Ork Meter Plus not verified"
            return
        root = Path(os.environ.get("PLUGIN_DATA") or os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "ork-auto-state"
        result = handle(event, root, trace)
        if result is None:
            from .first_output_context import handle as enrich_first_output
            result = enrich_first_output(event, root, trace=trace)
        if result is None:
            return
        response, info, visible_bytes = result
        rendered = json.dumps(response, ensure_ascii=False) + "\n"
        if sys.stdout.write(rendered) != len(rendered):
            raise OSError("short hook output write")
        sys.stdout.flush()
        info.confirm(visible_bytes)
        if trace is not None:
            trace["decision"] = "replacement emitted"
    except Exception as exception:
        if info is not None and hasattr(info, "abandon"):
            info.abandon("write_failed")
        # Hook failure must never hide a native tool result.
        if trace is not None:
            trace["exception_type"] = type(exception).__name__
        return
    finally:
        if trace is not None:
            trace["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            try:
                descriptor = os.open(trace_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(trace, sort_keys=True) + "\n")
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    main()
