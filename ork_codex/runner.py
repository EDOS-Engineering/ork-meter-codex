"""Wrap one explicitly selected noninteractive command in its original sandbox."""
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import time
from .policy import Policy
from .reduce import deliver
from .delivery import OutputDeliveryError

MAX_REDUCE = 64 * 1024 * 1024


def run(argv, kind="unknown", store=None, policy=None, full=False, stdout=None, stderr=None):
    import sys
    stdout = stdout or sys.stdout.buffer
    stderr = stderr or sys.stderr.buffer
    policy = policy or Policy.environment()
    if not argv:
        raise ValueError("command argv required after --")
    if full or policy.mode == "off":
        return subprocess.call(argv)
    # Files bound memory during execution. No shell parsing, reordering,
    # retries, environment changes, approval changes, or global config writes.
    with tempfile.TemporaryFile(dir=store.root if store else None) as out, tempfile.TemporaryFile(dir=store.root if store else None) as err:
        child = subprocess.Popen(argv, stdout=out, stderr=err,
                                 start_new_session=os.name != "nt",
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        interrupted = None
        interrupted_at = None
        previous = {}
        def forward(signum, frame):
            nonlocal interrupted, interrupted_at
            interrupted = signum
            interrupted_at = time.monotonic()
            if child.poll() is not None:
                return
            try:
                if os.name != "nt":
                    os.killpg(child.pid, signum)
                elif signum in (signal.SIGINT, signal.SIGBREAK):
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    child.terminate()
            except OSError:
                if child.poll() is None:
                    child.terminate()
        signals = [signal.SIGINT, signal.SIGTERM]
        if os.name == "nt":
            signals.append(signal.SIGBREAK)
        for sig in signals:
            previous[sig] = signal.signal(sig, forward)
        try:
            while True:
                try:
                    code = child.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if interrupted_at is not None and time.monotonic() - interrupted_at > 5:
                        if os.name != "nt":
                            os.killpg(child.pid, signal.SIGKILL)
                        else:
                            child.kill()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        reduced = False
        def write_result(target, result, receipt=None):
            phase = "write_failed"
            try:
                accepted = target.write(result)
                if accepted != len(result):
                    raise OSError("short command output write")
                phase = "flush_failed"
                target.flush()
            except (OSError, ValueError):
                if receipt:
                    receipt.abandon(phase)
                if store:
                    store.mark_accounting_gap()
                raise OutputDeliveryError(code, receipt.get("artifact") if receipt else None)
            if receipt:
                receipt.confirm(len(result))
        for stream, target, stream_kind in ((out, stdout, kind), (err, stderr, "log")):
            size = stream.tell()
            stream.seek(0)
            if size > MAX_REDUCE:
                while chunk := stream.read(1024 * 1024):
                    write_result(target, chunk)
            else:
                data = stream.read()
                result, info = deliver(data, stream_kind, store, policy)
                reduced = reduced or (info.get("reduced", False) and not info.get("shadow", False))
                write_result(target, result, info if hasattr(info, "confirm") else None)
        if reduced and code != 0:
            # PowerShell's -Command tool boundary can normalize native nonzero
            # exits to 1. Retain the child's actual status in shortened output.
            note = f"\n[Ork Meter command exit: {code}; interrupted: {interrupted is not None}]\n".encode()
            write_result(stderr, note)
            try:
                store.event("status_note", after=len(note), mode=policy.mode)
            except (OSError, sqlite3.Error):
                store.mark_accounting_gap()
        if store is not None and os.environ.get("ORK_CODEX_SESSION"):
            # Scope loop warnings to an explicit context identity. Never stop
            # or rerun the command automatically, and reset after success.
            try:
                identity = json.dumps([os.environ["ORK_CODEX_SESSION"], os.getcwd(), argv])
                count = store.failure(identity, code != 0)
                if count == 3 and policy.mode == "compact":
                    note = b"\n[Ork Meter: this command failed three times in this context; inspect its evidence before retrying.]\n"
                    write_result(stderr, note)
                    store.event("loop_warning", after=len(note), mode=policy.mode)
            except (OSError, RuntimeError, sqlite3.Error):
                pass
        if interrupted is not None:
            if os.name == "nt" and interrupted in (signal.SIGINT, signal.SIGBREAK):
                return 130
            return 128 + interrupted
        return 128 - code if code < 0 else code
