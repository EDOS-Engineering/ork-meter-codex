"""Plugin-owned, metadata-only usage delivery. No helper or capture daemon.

Read Codex's local token snapshots; never send transcript text or identities.
Credentials live exclusively in the OS credential store. Snapshots overlap and
must not be interpreted as additive billing events.
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import sys
import subprocess
from contextlib import contextmanager
import tempfile
from urllib.parse import quote_plus
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .storage import private_directory
from .terminal import masked_input

ORIGINS = {False: "https://orkmeter.com", True: "https://dev.ork-meter.edos.io"}
SERVICE = "edos-ork-meter-codex"
MARKER = "Ork usage tracking"
COUNTERS = {"input_tokens": "input_token_count", "output_tokens": "output_token_count",
            "cached_input_tokens": "cached_token_count", "reasoning_output_tokens": "reasoning_token_count",
            "total_tokens": "total_token_count", "cache_write_input_tokens": "cache_write_token_count"}


def home_path():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").absolute()


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError()
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() != timedelta(0):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ValueError("Usage timestamp must be UTC")


def encode(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate tracking field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def reference(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid tracking reference")
    return value


def vault():
    try:
        import keyring
        backend = keyring.get_keyring()
        if type(backend).__module__ not in {"keyring.backends.SecretService", "keyring.backends.Windows", "keyring.backends.macOS"}:
            raise ValueError()
        return backend
    except Exception:
        raise ValueError("OS credential store unavailable; unlock your WSL desktop keyring and retry") from None


def load(ref):
    try:
        raw = vault().get_password(SERVICE, reference(ref)) or "null"
        if len(raw.encode()) > 2048:
            raise ValueError()
        record = decode(raw)
        if (not isinstance(record, dict) or record.get("id") != ref or
                record.get("origin") not in ORIGINS.values() or record.get("source") != "openai" or
                record.get("client") != "codex"):
            raise ValueError()
        required = {"id", "origin", "source", "client", "created_at", "device_secret", "token"}
        if not required <= set(record) or set(record) - required - {"acknowledged_until"}:
            raise ValueError()
        for key, prefix in (("device_secret", "omp_"), ("token", "omc_")):
            if not re.fullmatch(prefix + r"[A-Za-z0-9_-]{43}", record[key]):
                raise ValueError()
        if timestamp(record["created_at"]) > now() + timedelta(minutes=5):
            raise ValueError()
        if record.get("acknowledged_until"):
            timestamp(record["acknowledged_until"])
        return record
    except Exception:
        raise ValueError("Tracking pairing unavailable in this OS credential store") from None


def save(record):
    try:
        backend = vault()
        raw = encode(record)
        backend.set_password(SERVICE, record["id"], raw)
        if backend.get_password(SERVICE, record["id"]) != raw:
            raise ValueError()
    except Exception:
        raise ValueError("Pairing could not be saved in the OS credential store") from None


def public(record):
    proposal = {"id": record["id"], "source": "openai", "client": "codex"}
    for key, prefix in (("device", "omp_"), ("token", "omc_")):
        secret = record["device_secret" if key == "device" else "token"]
        proposal[key + "_sha256"] = hashlib.sha256((prefix + "\0" + secret).encode()).hexdigest()
    raw = encode(proposal)
    fingerprint = hashlib.sha256(("ork-meter-pairing-v1\0" + record["origin"] + "\0" + raw).encode()).hexdigest()
    return {"reference": record["id"], "fingerprint": fingerprint,
            "approval_url": record["origin"] + "/pair#proposal=" + quote_plus(raw)}


def prepare(dev=False):
    secret = lambda prefix: prefix + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    record = {"id": secrets.token_hex(16), "source": "openai", "client": "codex",
              "origin": ORIGINS[dev], "created_at": now().isoformat(),
              "device_secret": secret("omp_"), "token": secret("omc_")}
    try:
        if vault().get_password(SERVICE, record["id"]) is not None:
            raise ValueError()
    except Exception:
        raise ValueError("OS credential store unavailable or pairing reference already exists") from None
    save(record)
    return public(record)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def exchange(record, path, body, device=False):
    if record["origin"] not in ORIGINS.values():
        raise ValueError("Unsupported tracking site")
    proof = ("Device " + record["device_secret"]) if device else ("Collector " + record["token"])
    request = Request(record["origin"] + path, data=encode(body).encode(), method="POST",
                      headers={"Authorization": proof, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(2049)
            if response.status != 200 or response.headers.get_all("Content-Type") != ["application/json"] or len(raw) > 2048:
                raise ValueError()
            result = decode(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
    except Exception:
        raise ValueError("Development/production site did not acknowledge tracking; check access and connectivity") from None


def validate_authorization(record, auth):
    expected = {"id": record["id"], "source": "openai", "client": "codex", "feed": record["id"],
                "audience": record["origin"], "scope": "usage:ingest"}
    if not isinstance(auth, dict) or set(auth) != set(expected) | {"expires_at"} or any(auth[k] != v for k, v in expected.items()):
        raise ValueError("Tracking authorization does not match this pairing")
    expires = timestamp(auth["expires_at"])
    if not now() < expires <= now() + timedelta(days=30, minutes=5):
        raise ValueError("Tracking authorization expired or invalid")
    if expires > timestamp(record["created_at"]) + timedelta(days=31, minutes=5):
        raise ValueError("Tracking authorization lifetime invalid")
    if record.get("acknowledged_until") and timestamp(record["acknowledged_until"]) != expires:
        raise ValueError("Tracking authorization changed")


def claim(ref):
    if not sys.stdin.isatty():
        raise ValueError("Claim the browser code in your own terminal; never paste it into chat")
    record = load(ref)
    if now() > timestamp(record["created_at"]) + timedelta(hours=24):
        raise ValueError("Pairing expired; prepare a new pairing")
    code = masked_input("Browser claim code: ").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", code):
        raise ValueError("Invalid browser claim code")
    auth = exchange(record, "/v1/collectors/claim", {"id": ref, "claim_code": code}, device=True)
    validate_authorization(record, auth)
    record["acknowledged_until"] = auth["expires_at"]
    save(record)
    return {"reference": ref, "authorized_until": auth["expires_at"]}


def status(ref):
    record = load(ref)
    result = exchange(record, "/v1/collectors/native-status", {})
    validate_authorization(record, result.get("authorization"))
    if set(result) - {"authorization", "latest_export"}:
        raise ValueError("Unexpected tracking receipt")
    latest = result.get("latest_export")
    if latest is not None:
        if (not isinstance(latest, dict) or latest.get("client") != "codex" or
                latest.get("additive") is not False or latest.get("included_in_spend") is not False):
            raise ValueError("Invalid tracking receipt")
        for field in ("captured_at", "received_at"):
            at = timestamp(latest[field])
            if not timestamp(record["created_at"]) - timedelta(minutes=5) <= at <= now() + timedelta(minutes=5):
                raise ValueError("Invalid tracking receipt time")
    return {"reference": ref, "site": record["origin"], "delivery_verified": latest is not None,
            "latest_export": latest}


def snapshot(path, ref):
    """Bound reads; select only token_count records and allowlisted integers."""
    latest = None
    with Path(path).open("rb") as stream:
        stream.seek(0, 2)
        offset = max(0, stream.tell() - (8 << 20))
        stream.seek(offset)
        if offset:
            stream.readline()
        for line in stream:
            if len(line) > (1 << 20):
                continue
            try:
                item = json.loads(line)
                payload = item.get("payload", {})
                if item.get("type") == "event_msg" and payload.get("type") == "token_count" and payload.get("info"):
                    latest = (item["timestamp"], payload["info"])
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
    if latest is None:
        raise ValueError("No native usage snapshot available for this session yet")
    at, info = latest
    if not now() - timedelta(days=1) <= timestamp(at) <= now() + timedelta(minutes=5):
        raise ValueError("Native usage snapshot is stale")
    samples = []
    for field, kind in (("total_token_usage", "thread_total_snapshot"), ("last_token_usage", "thread_last_snapshot")):
        values = info.get(field)
        if not isinstance(values, dict):
            raise ValueError("Incomplete native usage snapshot")
        counters = {}
        for key, target in COUNTERS.items():
            if key == "cache_write_input_tokens" and key not in values:
                continue
            value = values.get(key)
            if type(value) is not int or not 0 <= value <= 1_000_000_000_000:
                raise ValueError("Invalid native usage counter")
            counters[target] = str(value)
        samples.append({"kind": kind, "counters": counters})
    identity = hashlib.sha256((reference(ref) + "\0" + str(Path(path).resolve()) + "\0" + at + "\0" + encode(samples)).encode()).hexdigest()[:32]
    return {"id": identity, "client": "codex", "captured_at": at, "samples": samples}


def session_path(home, session=None, transcript=None):
    root = (Path(home) / "sessions").resolve()
    if transcript:
        path = Path(transcript).resolve()
        if path.is_relative_to(root) and path.is_file():
            return path
        raise ValueError("Usage transcript must be inside this Codex profile's sessions directory")
    session = session or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if not isinstance(session, str) or not re.fullmatch(r"[a-fA-F0-9-]{32,36}", session):
        raise ValueError("Provide the current Codex session ID")
    matches = list(root.rglob("*" + session + ".jsonl"))
    if len(matches) != 1:
        raise ValueError("Current session usage file unavailable in this Codex profile")
    return matches[0]


def write_json(path, value):
    if path.is_symlink():
        raise ValueError("Linked tracking configuration refused")
    descriptor, staging = tempfile.mkstemp(prefix=".ork-tracking-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(encode(value) + "\n")
        os.replace(staging, path)
    finally:
        Path(staging).unlink(missing_ok=True)


def sync(home, ref=None, session=None, transcript=None, *, refresh_entitlement=True):
    state = private_directory(Path(home) / "ork-tracking")
    if ref is None:
        ref = json.loads((state / "connection.json").read_text())["reference"]
    record = load(ref)
    if not record.get("acknowledged_until"):
        raise ValueError("Approve and claim this WSL pairing before sending usage")
    from . import entitlement
    if refresh_entitlement:
        entitlement.refresh(home)
    path = session_path(home, session, transcript)
    observation = snapshot(path, ref)
    identity = hashlib.sha256((reference(ref) + "\0" + str(path)).encode()).hexdigest()[:32]
    checkpoint = state / ("sent-" + identity + ".json")
    if checkpoint.exists() and decode(checkpoint.read_text()).get("id") == observation["id"]:
        return {"sent": False, "already_delivered": True}
    response = exchange(record, "/v1/collectors/observations", observation)
    if set(response) != {"created"} or type(response["created"]) is not bool:
        raise ValueError("Usage delivery not acknowledged")
    # Server handles duplicate IDs, including a retry after an interrupted write.
    write_json(checkpoint, {"id": observation["id"], "delivered": True})
    return {"sent": True, "created": response["created"], "site": record["origin"], "additive": False}


def install(home, ref):
    home = Path(home)
    private_directory(home / "ork-tracking")
    lock = home / "ork-tracking" / "install.lock"
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("Another tracking installation is active") from None
    os.close(descriptor)
    try:
        return _install(home, ref)
    finally:
        lock.unlink()


def _install(home, ref):
    """Snapshot plugin code, preserving unrelated hooks and their trust state."""
    load(ref)
    home = Path(home)
    state = private_directory(home / "ork-tracking")
    package = Path(__file__).resolve().parent
    sources = sorted(package.glob("*.py"))
    digest = hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in sources)).hexdigest()[:20]
    bundle = private_directory(state / digest)
    for source in sources:
        target = bundle / "ork_codex" / source.name
        if target.parent.is_symlink():
            raise ValueError("Linked tracking package refused")
        target.parent.mkdir(exist_ok=True)
        if target.is_symlink() or (target.exists() and target.read_bytes() != source.read_bytes()):
            raise ValueError("Installed tracking snapshot changed")
        if not target.exists():
            shutil.copyfile(source, target)
    launcher = bundle / "track.py"
    text = "import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parent))\nfrom ork_codex.tracking import hook\nhook()\n"
    if launcher.is_symlink() or (launcher.exists() and launcher.read_text() != text):
        raise ValueError("Installed tracking launcher changed")
    launcher.write_text(text)
    args = [sys.executable, "-I", "-B", str(launcher)]
    command = shlex.join(args) if os.name != "nt" else "& " + " ".join("'" + a.replace("'", "''") + "'" for a in args)
    config = home / "hooks.json"
    original = config.read_bytes() if config.exists() else None
    document = decode(original) if original else {}
    if not isinstance(document, dict) or not isinstance(document.get("hooks", {}), dict):
        raise ValueError("Invalid existing hooks configuration; left unchanged")
    hooks = document.setdefault("hooks", {})
    for event in ("PostToolUse", "Stop"):
        groups = hooks.get(event, [])
        if not isinstance(groups, list) or any(not isinstance(g, dict) or
                not isinstance(g.get("hooks"), list) or any(not isinstance(h, dict) for h in g["hooks"]) for g in groups):
            raise ValueError("Invalid existing hooks configuration; left unchanged")
    for event in ("PostToolUse", "Stop"):
        groups = hooks.setdefault(event, [])
        retained = []
        for group in groups:
            remaining = [h for h in group["hooks"] if h.get("statusMessage") != MARKER]
            if remaining or not group["hooks"]:
                retained.append({**group, "hooks": remaining})
        retained.append({"hooks": [{"type": "command", "command": command, "timeout": 5, "statusMessage": MARKER}]})
        hooks[event] = retained
    if (config.read_bytes() if config.exists() else None) != original:
        raise ValueError("Hooks changed during tracking installation; retry")
    if config.is_symlink():
        raise ValueError("Linked hooks configuration refused")
    write_json(config, document)
    write_json(state / "connection.json", {"reference": reference(ref)})
    from . import auto_hook, entitlement
    optimization = auto_hook.install(home)
    entitlement.refresh(home, background=False)
    return {"installed": True, "reference": ref, "optimization": optimization,
            "trust": "Review Ork usage tracking and automatic output filtering in Codex /hooks"}


# Automatic delivery may wait at most this long for its isolated process.
# Explicit terminal sync retains the normal transport timeout.
HOOK_DELIVERY_SECONDS = 2


@contextmanager
def delivery_lock(state):
    """Nonwaiting OS lock; released even if the hook process is terminated."""
    path = state / "delivery.lock"
    if path.is_symlink():
        raise ValueError("Linked delivery lock refused")
    with path.open("a+b") as stream:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, 2) == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            yield acquired
        finally:
            if acquired:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


def delivery_command(home, event):
    # No credentials or event/tool content in argv. Only local profile and
    # transcript selectors are passed to the same immutable package snapshot.
    source = ("import sys;sys.path.insert(0," + repr(str(Path(__file__).resolve().parent.parent)) + ");"
              "from ork_codex.tracking import sync;"
              "sync(sys.argv[1],session=sys.argv[2] or None,transcript=sys.argv[3] or None,"
              "refresh_entitlement=False)")
    selectors = [event.get("session_id") or "", event.get("transcript_path") or ""]
    if any(not isinstance(value, str) for value in selectors):
        raise ValueError("Invalid tracking selectors")
    return [sys.executable, "-I", "-B", "-c", source, str(home), *selectors]


def automatic_sync(home, event):
    state = private_directory(Path(home) / "ork-tracking")
    with delivery_lock(state) as acquired:
        if not acquired:
            return
        retry = state / "delivery-retry.json"
        previous = decode(retry.read_text()) if retry.exists() else {}
        if previous.get("after") and timestamp(previous["after"]) > now():
            return
        try:
            subprocess.run(delivery_command(home, event), timeout=HOOK_DELIVERY_SECONDS,
                           check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:
            failures = min(int(previous.get("failures", 0)) + 1, 5)
            write_json(retry, {"failures": failures,
                              "after": (now() + timedelta(seconds=min(60 * 2 ** (failures - 1), 900))).isoformat()})
            raise
        retry.unlink(missing_ok=True)
        (state / "last-error.json").unlink(missing_ok=True)


def hook():
    home = home_path()
    state = home / "ork-tracking"
    if not (state / "connection.json").is_file():
        return
    try:
        raw = sys.stdin.buffer.read((1 << 20) + 1)
        if len(raw) > (1 << 20):
            return
        event = json.loads(raw)
        if event.get("hook_event_name") not in {"PostToolUse", "Stop"}:
            return
        automatic_sync(home, event)
    except Exception:
        # Never block a tool, add model context, or leak private backend errors.
        try:
            write_json(state / "last-error.json", {"delivery_verified": False, "at": now().isoformat(),
                                                   "error": "Usage delivery failed; run tracking status or sync in your terminal"})
        except Exception:
            pass
