"""Once-per-session AB enrichment in the existing native output hook.

No additional tool or model instruction is installed. Existing output reduction
has priority; this path handles an otherwise unchanged native result.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from ..policy import Policy
from ..storage import private_directory
from .core import SourcePolicy, collect, encoded, query, snapshot, text_values
from .packet import render

SOURCE_SUFFIXES = {'.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs', '.cs', '.java',
                   '.cpp', '.c', '.h', '.rb', '.php', '.swift', '.kt', '.sql'}
EXCLUDED_PARTS = {'node_modules', 'vendor', 'build', 'dist', 'venv', '__pycache__',
                  'evidence', 'experiments', 'benchmarks', 'secrets', 'credentials', 'profiles'}
EXCLUDED_NAMES = {'secrets.py', 'credentials.py', 'private_key.py', 'conftest.py'}
RESTRICTED = re.compile(
    r"\b(?:(?:do not|don't|never|must not)\s+(?:\w+\s+){0,2}(?:read|inspect|open|access|scan|search)\b"
    r"|only\s+(?:read|inspect|open|access|scan|search)\b|forbidden|off.limit|hidden\s+tests?\b)", re.I)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def transcript_context(event):
    """Read bounded native transcript metadata, not an arbitrary source file.

    If transcript context is missing, collection is confined to literal paths
    already named in the native command. Restrictive source instructions cause
    abstention rather than a guessed authorization policy.
    """
    path = event.get('transcript_path')
    if not isinstance(path, str) or not path:
        return '', '', False
    users, first, consumed = [], '', 0
    try:
        with open(path, encoding='utf-8') as stream:
            for _ in range(128):
                line = stream.readline(262145)
                if not line:
                    break
                consumed += len(line)
                if len(line) > 262144 or consumed > 2 * 1024 * 1024:
                    return '', '', False
                record = json.loads(line)
                item = record.get('payload', record)
                if not isinstance(item, dict):
                    continue
                kind = item.get('type')
                if kind == 'message':
                    content = item.get('content', [])
                    text = content if isinstance(content, str) else '\n'.join(
                        b.get('text', '') for b in content if isinstance(b, dict) and
                        b.get('type') in ('text', 'input_text', 'output_text'))
                    if item.get('role') == 'user':
                        users.append(text)
                    elif item.get('role') == 'assistant' and text and not first:
                        first = text
                elif kind == 'user_message':
                    users.append(item.get('message', ''))
                elif kind == 'agent_message' and not first:
                    first = item.get('message', '')
                elif kind in ('function_call', 'custom_tool_call') and not first:
                    first = item.get('arguments', item.get('input', ''))
    except (OSError, UnicodeError, ValueError, TypeError):
        return '', '', False
    return first, '\n'.join(users), bool(users)


def readable_inventory(root, target, contextual):
    """Use Git's ignored-file policy; never recursively open project sources."""
    if not contextual:
        return SourcePolicy(tuple(p for p in target['paths'] if eligible(p)))
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others',
                             '--exclude-standard', '-z'], capture_output=True, timeout=3)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        return SourcePolicy(tuple(p for p in target['paths'] if eligible(p)))
    names = {os.fsdecode(p) for p in result.stdout.split(b'\0') if p}
    terms = [term.casefold() for term in target['terms']]
    names = sorted((p for p in names if eligible(p)), key=lambda p: (
        p not in target['paths'], not any(t in p.casefold() for t in terms), p))
    return SourcePolicy(tuple(names[:256]))


def eligible(name):
    path = Path(name)
    return (not path.is_absolute() and '\\' not in name and ':' not in name and
            all(p and not p.startswith('.') and p not in EXCLUDED_PARTS and p != '..'
                for p in name.split('/')) and path.suffix in SOURCE_SUFFIXES and
            path.name.casefold() not in EXCLUDED_NAMES)


class Receipt(dict):
    """Record delivery separately from token/cost savings accounting."""
    def __init__(self, path, record):
        super().__init__(enriched=True, reduced=False, source_evidence_tokens=record['tokens'])
        self.path, self.record = path, record

    def finish(self, status, byte_count=None):
        self.record.update(status=status, emitted_bytes=byte_count)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(self.record, stream, ensure_ascii=False)

    def confirm(self, byte_count):
        self.finish('emitted', byte_count)

    def abandon(self, reason='abandoned'):
        self.finish(reason)


def handle(event, state_root, trace=None):
    """Append a compact packet without changing the command or original result."""
    if (Policy.environment().mode != 'compact' or event.get('hook_event_name') != 'PostToolUse'
            or event.get('tool_name') != 'Bash'):
        return None
    args = event.get('tool_input')
    response = event.get('tool_response')
    if isinstance(response, dict):
        response = next((response[k] for k in ('output', 'aggregatedOutput')
                         if isinstance(response.get(k), str)), None)
    session = event.get('session_id', event.get('thread_id'))
    cwd = event.get('cwd')
    if (not isinstance(args, dict) or not isinstance(args.get('command'), str) or
            not isinstance(response, str) or not isinstance(session, str) or not session or
            not isinstance(cwd, str) or not Path(cwd).is_absolute()):
        return None
    command = args['command']
    root = Path(cwd)
    call = event.get('tool_use_id') or digest(command)
    key = digest(str(root) + '\0' + session)
    directory = private_directory(Path(state_root) / 'first-output')
    path = directory / (key + '.json')
    original_hash = digest(response)
    if path.exists():
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('call') != call or record.get('original_sha256') != original_hash:
            return None
        suffix = record.get('suffix')
        if not isinstance(suffix, str):
            return None
        output = response + suffix
        return {'decision': 'block', 'reason': output}, Receipt(path, record), len(output.encode())
    first, task, contextual = transcript_context(event)
    if RESTRICTED.search(task):
        return None
    target = query(first or command, task)
    if not target['terms'] and not target['entities']:
        return None
    policy = readable_inventory(root, target, contextual)
    if not policy.readable_paths:
        return None
    # One concurrent tool result owns source collection for this native session.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    os.close(fd)
    record = {'schema': 'native-first-output-ab-v7', 'call': call,
              'original_sha256': original_hash, 'status': 'prepared', 'tokens': 0}
    receipt = Receipt(path, record)
    try:
        packet = collect(root, target, policy)
        suffix, rendering = render(packet, (command + '\n' + response,))
        if suffix is None:
            receipt.finish(rendering.get('reason', 'no_matches'))
            return None
        selected = {r['path']: r['sha256'] for rows in rendering['selected'].values() for r in rows}
        for name, expected in selected.items():
            current = snapshot(root, name)
            if current is None or current['sha256'] != expected:
                receipt.finish('source_changed_before_delivery')
                return None
        record.update(suffix=suffix, tokens=rendering['tokens'], selected=rendering['selected'])
        receipt['source_evidence_tokens'] = rendering['tokens']
        receipt.finish('prepared')
        if trace is not None:
            trace.update(first_output='AB', source_evidence_tokens=rendering['tokens'])
        output = response + suffix
        return {'decision': 'block', 'reason': output}, receipt, len(output.encode())
    except Exception:
        receipt.finish('unavailable')
        return None
