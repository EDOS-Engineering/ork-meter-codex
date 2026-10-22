"""Ork Meter Plus gate for the savings levers.

Optimizations are an Ork Meter Plus feature (ork-meter #261, #265); free Ork
Meter is visibility only. Native tracking uses its existing OS-stored pairing
to ask the platform (``GET /v1/entitlement``) at most every 15 minutes.
Older helper-based connections remain supported. Both keep the answer in
``$CODEX_HOME/ork-meter-codex/entitlements/<reference>/entitlement.json``
bound to the saved public pairing reference, owner-only, with exactly the hosted contract fields::

    {"optimize": true, "state": "trial", "until": "2026-10-28T12:00:00Z", "checked_at": "2026-10-14T12:00:00Z"}

``state`` is one of ``STATES``. Reduction paths never wait on the network:
they read the file and apply the same rules as the helper and the Claude
plugin (ork-meter ``internal/pluginentitlement``):

* no saved pairing or no file means not connected: not entitled;
* anything but a small owner-only regular file holding exactly those fields
  is not entitled;
* entitled means ``optimize`` is true and now is at most ``OFFLINE_GRACE``
  past ``until``, or past ``checked_at`` when ``until`` is null.

When not entitled the levers switch to ``off`` (output passes through
unchanged). Telemetry capture, receipts, recovery of already-saved
artifacts and local reports are not gated.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener

FILE_NAME = "entitlement.json"
MAX_BYTES = 1024
OFFLINE_GRACE = 24 * 3600
REFRESH_INTERVAL = 15 * 60
STATES = ("promo", "trial", "active", "past_due", "free")
FIELDS = {"optimize", "state", "until", "checked_at"}

ENTITLED = "entitled"
NOT_ENTITLED = "not_entitled"
NOT_CONNECTED = "not_connected"

MESSAGE = ("Ork Meter savings are off: they are part of Ork Meter Plus. "
           "Upgrade at https://orkmeter.com/billing")
UNVERIFIED_MESSAGE = (
    "Ork Meter savings are off: could not verify current Plus access. "
    "Codex and usage capture continue. Check your connection and Ork Meter helper, "
    "then rerun setup after the 15-minute refresh interval. "
    "You do not need to purchase Plus again to retry verification.")
CONNECT_MESSAGE = "Ork Meter savings are off: run setup to connect your account."


def base(home=None):
    home = home or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(home).absolute() / "ork-meter-codex"


def path(home=None, ref=None):
    # An old helper may finish after reconnection. Its answer belongs only to
    # the reference captured when that helper was launched.
    ref = saved_reference(home) if ref is None else ref
    if ref is None:
        return base(home) / FILE_NAME  # legacy, unbound cache: cleanup only
    from .meter import reference
    return base(home) / "entitlements" / reference(ref) / FILE_NAME


def _timestamp(value):
    if not isinstance(value, str) or not value or len(value) > 40 or "T" not in value.upper():
        raise ValueError("invalid time")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value)
    if parsed.tzinfo is None:
        raise ValueError("time without zone")
    return parsed.timestamp()


def read(file_path):
    """The validated cached answer, or raise."""
    info = os.lstat(file_path)
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if not stat.S_ISREG(info.st_mode) or reparse or info.st_size > MAX_BYTES:
        raise ValueError("entitlement must be a small regular file")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise PermissionError("entitlement must be owner-only")
    with open(file_path, "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("entitlement exceeded its size allowance")

    def unique(pairs):
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate field")
        return dict(pairs)

    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("entitlement fields")
    if type(value["optimize"]) is not bool or value["state"] not in STATES:
        raise ValueError("entitlement values")
    if value["optimize"] and value["state"] == "free":
        raise ValueError("free does not optimize")
    return {"optimize": value["optimize"], "state": value["state"],
            "until": None if value["until"] is None else _timestamp(value["until"]),
            "checked_at": _timestamp(value["checked_at"])}


def _assessment(home=None, now=None):
    """Keep a failed verification distinct from a recent hosted denial."""
    try:
        ref = saved_reference(home)
        if ref is None:
            return NOT_CONNECTED, CONNECT_MESSAGE
        file_path = path(home, ref)
        if not os.path.lexists(file_path):
            return NOT_CONNECTED, UNVERIFIED_MESSAGE
        answer = read(file_path)
        if saved_reference(home) != ref:
            return NOT_CONNECTED, UNVERIFIED_MESSAGE
    except (OSError, ValueError, TypeError, OverflowError):
        return NOT_ENTITLED, UNVERIFIED_MESSAGE
    now = time.time() if now is None else now
    bound = answer["until"] if answer["until"] is not None else answer["checked_at"]
    if answer["optimize"] and now <= bound + OFFLINE_GRACE:
        return ENTITLED, None
    # Only a recent validated hosted answer can justify an upsell. An expired
    # positive answer or a stale denial could belong to a paying offline user.
    age = now - answer["checked_at"]
    message = MESSAGE if not answer["optimize"] and -300 <= age <= REFRESH_INTERVAL else UNVERIFIED_MESSAGE
    return NOT_ENTITLED, message


def status(home=None, now=None):
    """``ENTITLED``, ``NOT_ENTITLED`` or ``NOT_CONNECTED``. Never raises."""
    return _assessment(home, now)[0]


def message(home=None, now=None):
    """Recovery guidance when savings are off; None when Plus is available."""
    return _assessment(home, now)[1]


def entitled(home=None, now=None):
    return status(home, now) == ENTITLED


def gate(policy, home=None):
    """``policy`` unchanged with Ork Meter Plus; otherwise mode ``off``.
    Explicit opt-outs (``ORK_CODEX_MODE=off`` and the others) still apply
    on top while entitled."""
    return policy if entitled(home) else replace(policy, mode="off")


def saved_reference(home=None):
    native = base(home).parent / "ork-tracking" / "connection.json"
    if native.exists():
        return native_reference(home)
    try:
        record = json.loads((base(home) / "pairing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ref = record.get("reference") if isinstance(record, dict) else None
    from .meter import reference
    try:
        return reference(ref)
    except ValueError:
        return None


def native_reference(home=None):
    """The public reference installed by native tracking, never its credential."""
    from .tracking import decode, reference
    try:
        connection = base(home).parent / "ork-tracking" / "connection.json"
        if connection.stat().st_size > MAX_BYTES:
            return None
        value = decode(connection.read_text(encoding="utf-8"))
        return reference(value["reference"]) if isinstance(value, dict) and set(value) == {"reference"} else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def refresh_native(home, ref):
    """Refresh from this OS's existing native credential; keep offline grace."""
    from . import tracking
    from .storage import private_directory
    if saved_reference(home) != ref or native_reference(home) != ref:
        return False
    cache = path(home, ref)
    directory = private_directory(cache.parent)
    # The OS releases this lock even if the detached refresh is killed. An
    # O_EXCL marker survives termination and can strand Plus indefinitely.
    # This cache directory is per reference and separate from capture state.
    try:
        with tracking.delivery_lock(directory) as acquired:
            if not acquired:
                return False
            return _refresh_native_locked(home, ref, cache, directory)
    except (OSError, ValueError, TypeError):
        return False


def _refresh_native_locked(home, ref, cache, directory):
    from . import tracking
    staged = None
    try:
        if not due(home):
            return False
        record = tracking.load(ref)
        if not record.get("acknowledged_until"):
            return False
        tracking.write_json(cache.with_name("entitlement-attempt"), {})
        request = Request(record["origin"] + "/v1/entitlement", headers={
            "Authorization": "Collector " + record["token"],
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with build_opener(tracking.NoRedirect()).open(request, timeout=15) as response:
                if response.status != 200 or response.headers.get_all("Content-Type") != ["application/json"]:
                    return False
                raw = response.read(MAX_BYTES + 1)
        except HTTPError as error:
            if error.code in {401, 403} and saved_reference(home) == ref:
                cache.unlink(missing_ok=True)
            return False
        if len(raw) > MAX_BYTES:
            return False
        descriptor, staged = tempfile.mkstemp(prefix=".entitlement-", dir=directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        answer = read(staged)
        if answer["checked_at"] > time.time() + 300 or saved_reference(home) != ref:
            return False
        os.replace(staged, cache)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if staged is not None:
            Path(staged).unlink(missing_ok=True)


def due(home=None, now=None):
    if saved_reference(home) is None:
        return False
    file_path = path(home)
    try:
        try:
            info = os.lstat(file_path.with_name("entitlement-attempt"))
        except FileNotFoundError:
            info = os.lstat(file_path)
    except OSError:
        return True
    age = (time.time() if now is None else now) - info.st_mtime
    return not stat.S_ISREG(info.st_mode) or age < -300 or age >= REFRESH_INTERVAL


@contextmanager
def disconnect_guard(home=None):
    """Serialize forgetting a reference with setup's saved-pairing writes."""
    from .storage import private_directory
    directory = private_directory(base(home))
    lock = directory / "setup.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("Exit the active Ork Meter setup/Codex session before disconnecting") from None
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def forget(home=None, ref=None):
    """Forget only the selected pairing; callers serialize with setup.

    Without a reference, remove only the obsolete unbound cache. Late helper
    writes remain harmless because status requires a current saved reference.
    """
    if ref is not None:
        file_path = path(home, ref)
        if saved_reference(home) == ref:
            (base(home) / "pairing.json").unlink(missing_ok=True)
        file_path.unlink(missing_ok=True)
        file_path.with_name("entitlement-attempt").unlink(missing_ok=True)
    (base(home) / FILE_NAME).unlink(missing_ok=True)


def refresh(home=None, background=True):
    """Refresh using the installed connection when due. In the background it
    never blocks the caller; in the foreground it waits briefly. A missing
    saved pairing removes the cache. Never raises."""
    try:
        ref = saved_reference(home)
        if ref is None:
            forget(home)
            return False
        if not due(home):
            return False
        if native_reference(home) == ref:
            if not background:
                return refresh_native(str(base(home).parent), ref)
            # Spawn the installed package with no credential on argv or stdout.
            program = ("import sys;sys.path.insert(0,sys.argv[1]);"
                       "from ork_codex.entitlement import refresh_native;"
                       "refresh_native(sys.argv[2],sys.argv[3])")
            argv = [sys.executable, "-I", "-B", "-c", program,
                    str(Path(__file__).resolve().parent.parent), str(base(home).parent), ref]
        else:
            from .meter import binary, reference
            argv = [binary(), "pair", "entitlement", "--reference", reference(ref), "--cache", str(path(home, ref))]
        quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if not background:
            subprocess.run(argv, timeout=25, check=False, **quiet)
            return True
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            subprocess.Popen(argv, creationflags=flags, close_fds=True, **quiet)
        else:
            subprocess.Popen(argv, start_new_session=True, close_fds=True, **quiet)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
