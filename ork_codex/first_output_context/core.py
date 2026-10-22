"""Compact AB evidence for the native automatic output path.

Derived from the measured v5 target binding and packet representation. The host
must supply its source-access policy before any read. No recursive source scan.
"""
import ast
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

SOURCES = ('definitions', 'reference_files')
MAX_FILES = 256
MAX_DIRECTORIES = 512
MAX_ENTRIES = 4096
SOURCE_QUOTA = 3500
SKIP = {'.git', '.venv', 'venv', 'node_modules', 'vendor', 'dist', 'build', '__pycache__'}
EXTENSIONS = {'.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs', '.cs', '.java',
              '.cpp', '.c', '.h', '.rb', '.php', '.swift', '.kt', '.sql', '.md', '.rst'}
STOP = set('the and for from with this that will would should have has are was '
           'were not into current project source code file files find search '
           'look inspect check read use using then first need investigate '
           'function class method return def self import python python3 json '
           'command args arguments tool call output reply session shell bash '
           'true false null none print echo'.split())


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def keywords(text):
    if not isinstance(text, str) or len(text.encode('utf-8')) > 65536:
        return []
    # Tool argument keys are protocol, not model-mentioned targets.
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        value = None
    if isinstance(value, dict):
        def strings(item):
            if isinstance(item, str):
                yield item
            elif isinstance(item, (dict, list)):
                for child in (item.values() if isinstance(item, dict) else item):
                    yield from strings(child)
        try:
            text = ' '.join(strings(value))
        except RecursionError:
            return []
    tokens = list(dict.fromkeys(re.findall(r'[A-Za-z_][A-Za-z_0-9]{2,63}', text)))
    tokens = [t for t in tokens if t.lower() not in STOP]
    return sorted(tokens, key=lambda t: (not ('_' in t or re.search(r'[a-z][A-Z]', t)),
                                          text.index(t)))[:12]


def snapshot(root, name):
    """Read only regular UTF-8 files, without following any symlink component."""
    if os.name == "nt":
        return windows_snapshot(root, name)
    if os.name != "posix":
        return None
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or any(p.startswith('.') for p in parts):
        return None
    with ExitStack() as stack:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, parent)
        for part in parts[:-1]:
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, parent)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        stack.callback(os.close, fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None
        raw = bytearray()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        if (b'\0' in raw or
            (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            return None
        return dict(path=name, sha256=hashlib.sha256(raw).hexdigest(),
                    text=raw.decode('utf-8'), size=len(raw))


from dataclasses import dataclass


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("expected a literal relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") or p.startswith(".") for p in value.split("/")):
        raise ValueError("unsafe source path")
    return value


@dataclass(frozen=True)
class SourcePolicy:
    """Host-owned exact readable-file inventory, with deny taking precedence.

    Exclusions can name files or directory prefixes. No filesystem enumeration
    or natural-language access inference is performed. Missing policy fails closed.
    """
    readable_paths: tuple
    forbidden_paths: tuple = ()

    def __post_init__(self):
        if not isinstance(self.readable_paths, (tuple, list)) or not isinstance(self.forbidden_paths, (tuple, list)):
            raise ValueError("source policy requires path lists")
        readable = tuple(sorted({relative_path(p) for p in self.readable_paths}))
        forbidden = tuple(sorted({relative_path(p) for p in self.forbidden_paths}))
        if len(readable) > MAX_FILES or len(forbidden) > MAX_ENTRIES:
            raise ValueError("source policy inventory exceeds bound")
        object.__setattr__(self, "readable_paths", readable)
        object.__setattr__(self, "forbidden_paths", forbidden)

    def permits(self, name):
        return name in self.readable_paths and not any(
            name == p or name.startswith(p + "/") for p in self.forbidden_paths)

IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z_0-9]*')
ENTITY = re.compile(r'\b([A-Za-z][A-Za-z_-]{2,40})\s+(?:id\s*[:#]?\s*)?#?\s*(\d+)\b', re.I)
NON_ENTITY = STOP | set('update change set verify preserve leave keep remain add remove '
    'delete days hours seconds minutes value values line lines step steps version versions '
    'to by at of in is it all only unchanged id ids identifier identifiers sha utf ipv'.split())
PATH = re.compile(r'(?<![\w/])(?:[\w-]+/)*[\w-]+\.(?:py|js|ts|tsx|jsx|go|rs|cs|java|cpp|c|h|rb|php|swift|kt|sql)(?!\w)')


def parts(value):
    value = re.sub(r'([a-z])([A-Z])', r'\1_\2', value)
    return re.findall(r'[a-z]+|\d+', value.casefold())


def singular(value):
    return value[:-1] if len(value) > 3 and value.endswith('s') else value


def number(value):
    return value.lstrip('0') or '0'


def text_values(text):
    import json
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return text
    def walk(item):
        if isinstance(item, str):
            yield item
        elif isinstance(item, (dict, list)):
            for child in (item.values() if isinstance(item, dict) else item):
                yield from walk(child)
    try:
        return ' '.join(walk(value)) if isinstance(value, (dict, list)) else text
    except RecursionError:
        return ''


def query(text, task_text=''):
    if not isinstance(text, str) or len(text.encode()) > 65536:
        return dict(terms=[], entities=[], paths=[], protected_paths=[], abstain='first-output-bound')
    visible = text_values(text)
    entities = []
    for label, digits in ENTITY.findall(visible):
        label = singular(label.casefold())
        if label not in NON_ENTITY:
            entities.append(dict(labels=[label], number=number(digits)))
    # Explicit compound identifiers also carry their numeric binding.
    for token in IDENTIFIER.findall(visible):
        components = parts(token)
        labels = [singular(p) for p in components if p.isalpha() and p not in NON_ENTITY]
        digits = [p for p in components if p.isdigit()]
        if labels and len(digits) == 1:
            entities.append(dict(labels=labels, number=number(digits[0])))
    entities = list({(tuple(e['labels']), e['number']):e for e in entities}.values())
    paths, protected = [], []
    # Only path intent supplements the first output. User prompt never triggers
    # collection and its numbers never substitute for model-mentioned entities.
    for clause in re.split(r'[;\n]|(?<=[.!?])\s+', visible+'\n'+task_text):
        found = PATH.findall(clause)
        if re.search(r'\b(unchanged|unmodified|preserve|do not (?:edit|change)|leave)\b', clause, re.I):
            protected.extend(found)
        else:
            paths.extend(found)
    return dict(terms=keywords(text), entities=entities[:4],
        paths=sorted(set(paths)-set(protected)), protected_paths=sorted(set(protected)),
        abstain='too-many-entities' if len(entities)>4 else None)


def bound(identifier, entities):
    components = parts(identifier)
    labels = {singular(p) for p in components if p.isalpha()}
    digits = {number(p) for p in components if p.isdigit()}
    return any(set(e['labels']) <= labels and e['number'] in digits for e in entities)


def search(root, target, policy):
    terms, entities = target['terms'], target['entities']
    patterns = [(t, re.compile(r'(?<!\w)'+re.escape(t)+r'(?!\w)', re.I)) for t in terms]
    rows = []; scanned = size = 0; bounded_scan = False
    if target.get('abstain'):
        return rows, dict(files_examined=0, bytes_read=0, scan_limit_reached=False,
                          coverage='abstained: '+target['abstain'])
    # Access decisions happen before opening, hashing, parsing or matching files.
    # The native host supplies its authorized readable inventory, never the model.
    for name in policy.readable_paths:
        if not policy.permits(name):
            continue
        scanned += 1
        try:
            row = snapshot(root,name)
        except (OSError, UnicodeError):
            continue
        if row is None:
            continue
        size += row['size']; lines = row['text'].splitlines(keepends=True)
        hits = []
        for n,line in enumerate(lines,1):
            matches = list(dict.fromkeys(t for t in IDENTIFIER.findall(line) if bound(t,entities))) if entities else [t for t,p in patterns if p.search(line)]
            if matches:
                hits.append((n,matches))
        if not hits:
            continue
        matched = {t.casefold() for _,ts in hits for t in ts}
        definitions = []
        if Path(name).suffix == '.py':
            try:
                for node in ast.walk(ast.parse(row['text'])):
                    names = []
                    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                        names = [node.name]
                    elif isinstance(node,(ast.Assign,ast.AnnAssign)):
                        targets=node.targets if isinstance(node,ast.Assign) else [node.target]
                        names=[n.id for t in targets for n in ast.walk(t) if isinstance(n,ast.Name)]
                    for symbol in names:
                        if symbol.casefold() in matched:
                            start=min([node.lineno]+[d.lineno for d in getattr(node,'decorator_list',[])])
                            definitions.append((start,node.end_lineno,symbol))
            except (SyntaxError,RecursionError,ValueError):
                pass
        explicit=any(row['path']==p or row['path'].endswith('/'+p) for p in target['paths'])
        protected=any(row['path']==p or row['path'].endswith('/'+p) for p in target['protected_paths'])
        row.update(lines=lines,hits=hits,definitions=sorted(definitions))
        row['rank']=(protected,not explicit,not bool(definitions),-min(len(matched),20),row['path'])
        rows.append(row)
    return sorted(rows,key=lambda r:r['rank']),dict(files_examined=scanned,bytes_read=size,
        scan_limit_reached=bounded_scan,coverage='host-authorized inventory only; unlimited source bytes; exact identifier-component entity binding; Python AST definitions/assignments, textual references')


def collect(root, target, policy):
    enabled = SOURCES
    if isinstance(target,list):
        target=query(' '.join(target))
    rows,coverage=search(root,target,policy)
    sections={s:[] for s in enabled};omitted={s:0 for s in enabled}
    def append(name,item):
        if name in sections:
            if len(encoded(sections[name]+[item]))<=SOURCE_QUOTA:sections[name].append(item)
            else:omitted[name]+=1
    for row in rows:
        origin=dict(path=row['path'],sha256=row['sha256'])
        append('reference_files',dict(**origin,matched_terms=sorted({t for _,ts in row['hits'] for t in ts}),
            # Locations are reference metadata, not excerpts. They make B useful
            # even after a directory listing, without repeating that listing.
            locations=[n for n,_ in row['hits']][:3],matching_lines=len(row['hits'])))
        for start,end,symbol in row['definitions']:
            stop=min(end,start+11)
            append('definitions',dict(**origin,symbol=symbol,start_line=start,end_line=stop,
                definition_end_line=end,truncated=stop<end,text=''.join(row['lines'][start-1:stop])))
    visibility=[dict(path=r['path'],sha256=r['sha256'],start_line=n,end_line=n,
        text=r['lines'][n-1]) for r in rows for n,_ in r['hits'][:3]]
    return dict(kind='first-output-source-evidence-ab-v7',target=target,sources=sections,
                visibility=visibility,omitted_items=omitted,**coverage)


def windows_snapshot(root, name):
    """Verify the final opened Windows handle before reading source bytes."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    relative_path(name)
    root = Path(root).absolute()
    # The handle's final path is normalized (long names, resolved ancestors), so
    # compare against the normalized root rather than its literal spelling; an
    # 8.3 alias such as C:\Users\RUNNER~1 would otherwise never match. Like the
    # POSIX path, ancestors above root may be links; root itself and every
    # component below it are still rejected when they are reparse points.
    expected = Path(os.path.realpath(root)).joinpath(*PurePosixPath(name).parts)
    path = root
    for part in ("", *PurePosixPath(name).parts):
        if part:
            path = path / part
        attributes = path.lstat()
        if getattr(attributes, "st_file_attributes", 0) & 0x400:
            return None  # Symlink or junction/reparse point.
    fd = os.open(path, os.O_RDONLY | os.O_BINARY)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        final = kernel.GetFinalPathNameByHandleW
        final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        size = final(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
        if not 0 < size < len(buffer):
            return None
        actual = buffer.value
        if actual.startswith("\\\\?\\UNC\\"):
            actual = "\\\\" + actual[8:]
        elif actual.startswith("\\\\?\\"):
            actual = actual[4:]
        if os.path.normcase(actual) != os.path.normcase(str(expected)):
            return None  # An ancestor changed or was followed during open.
        raw = bytearray()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        if b"\0" in raw or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return None
        return dict(path=name, sha256=hashlib.sha256(raw).hexdigest(),
                    text=raw.decode("utf-8"), size=len(raw))
    finally:
        os.close(fd)
