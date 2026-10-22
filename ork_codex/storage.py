"""Immutable full results and concurrent SQLite statistics, local only."""
import hashlib
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

HANDLE = re.compile(r"^[a-f0-9]{64}$")


def private_directory(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise OSError("state directory must not be a symlink")
    if os.name == "nt":
        from .windows_private import private_mkdir
        path.parent.mkdir(parents=True, exist_ok=True)
        private_mkdir(path)
    else:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    return path.resolve()


def default_state():
    base = os.environ.get("ORK_CODEX_STATE")
    if base:
        return Path(base) / "ork-codex"
    return workspace_state(os.environ.get("ORK_CODEX_WORKSPACE") or Path.cwd())


def workspace_state(workspace, anchor=False):
    base = os.environ.get("ORK_CODEX_STATE")
    if base:
        return Path(base) / "ork-codex"
    workspace = Path(workspace).resolve()
    # Both MCP and shell wrappers use project-local state: plugin runtime
    # environment variables are not automatically shared with shell tools.
    # Workspace-write shells cannot rely on writable user-profile state.
    if not anchor:
        for candidate in [workspace, *workspace.parents]:
            if (candidate / ".git").exists():
                workspace = candidate
                break
    return workspace / ".local" / "ork-codex"


class Store:
    def __init__(self, root=None, run_id=None):
        self.run_id = run_id or os.environ.get("ORK_CODEX_RUN") or uuid.uuid4().hex
        self.root = private_directory(root or default_state())
        self.artifacts = private_directory(self.root / "artifacts")
        self.db = self.root / "stats.sqlite3"
        self.accounting_gap = False
        try:
            self.initialize_stats()
        except (sqlite3.Error, OSError):
            self.mark_accounting_gap()

    def initialize_stats(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL, kind TEXT, before_n INTEGER, after_n INTEGER, recovered_n INTEGER, mode TEXT)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
            for name in ("run_id", "artifact", "intervention_id", "origin_id", "policy_id", "delivery_state"):
                if name not in columns:
                    db.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT")
            db.execute("CREATE TABLE IF NOT EXISTS failures (key TEXT PRIMARY KEY, count INTEGER)")
        if os.name != "nt":
            self.db.chmod(0o600)

    @contextmanager
    def connection(self):
        if self.db.is_symlink():
            raise sqlite3.OperationalError("statistics file unavailable")
        db = sqlite3.connect(self.db, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def path(self, handle):
        if not isinstance(handle, str) or not HANDLE.fullmatch(handle):
            raise ValueError("invalid artifact handle")
        return self.artifacts / handle

    def put(self, data):
        handle = hashlib.sha256(data).hexdigest()
        path = self.path(handle)
        staging = self.artifacts / (".pending-" + uuid.uuid4().hex)
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            # A fully synced inode becomes visible under its immutable digest
            # atomically. A competing winner is complete before we inspect it.
            try:
                os.link(staging, path)
            except FileExistsError:
                if self.get(handle) != data:
                    raise OSError("artifact collision")
        finally:
            staging.unlink(missing_ok=True)
        return handle

    def get(self, handle):
        path = self.path(handle)
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise OSError("artifact unavailable")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != handle:
            raise OSError("artifact checksum mismatch; original unavailable")
        return data

    def event(self, kind, before=0, after=0, recovered=0, mode="unknown", artifact=None,
              intervention=None, origin=None, run_id=None, policy_id=None, delivery_state="emitted"):
        with self.connection() as db:
            db.execute("INSERT INTO events(at,kind,before_n,after_n,recovered_n,mode,run_id,artifact,intervention_id,origin_id,policy_id,delivery_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (time.time(), kind, before, after, recovered, mode, run_id or self.run_id,
                        artifact, intervention, origin, policy_id, delivery_state))

    def finish_delivery(self, intervention, state, byte_count=None):
        if state not in {"emitted", "cancelled", "abandoned", "write_failed", "flush_failed"}:
            raise ValueError("invalid delivery finalization")
        with self.connection() as db:
            if byte_count is None:
                db.execute("UPDATE events SET delivery_state=? WHERE intervention_id=? AND delivery_state='prepared'", (state, intervention))
            else:
                db.execute("UPDATE events SET delivery_state=?,after_n=? WHERE intervention_id=? AND delivery_state='prepared'", (state, byte_count, intervention))

    def recovery_context(self, handle, intervention=None):
        try:
            with self.connection() as db:
                rows = db.execute("SELECT run_id,mode,policy_id,intervention_id FROM events WHERE artifact=? AND intervention_id IS NOT NULL", (handle,)).fetchall()
        except (sqlite3.Error, OSError):
            self.mark_accounting_gap()
            return {"run_id": "unknown", "mode": "unknown", "policy_id": None,
                    "origin_intervention": None, "basis": "statistics unavailable"}
        if intervention:
            rows = [row for row in rows if row[3] == intervention]
            if len(rows) != 1:
                raise ValueError("intervention does not identify a delivery of this artifact")
        identities = {(row[0], row[1], row[2]) for row in rows}
        if len(identities) != 1:
            return {"run_id": "unknown", "mode": "unknown", "policy_id": None,
                    "origin_intervention": None, "basis": "missing or ambiguous delivery provenance"}
        run, mode, policy = next(iter(identities))
        return {"run_id": run, "mode": mode, "policy_id": policy,
                "origin_intervention": rows[0][3] if len(rows) == 1 else None,
                "basis": "explicit intervention" if intervention else "unique originating run/policy"}

    def charge_recovery(self, handle, count, context):
        try:
            self.event("recovery", recovered=count, mode=context["mode"], run_id=context["run_id"],
                       policy_id=context["policy_id"], artifact=handle, origin=context["origin_intervention"])
            return True
        except (sqlite3.Error, OSError):
            self.mark_accounting_gap()
            return False

    def mark_accounting_gap(self):
        self.accounting_gap = True
        # Preserve an honest boolean gap across processes independently of the
        # optional DB. No content, identity or invented missing-byte total.
        try:
            fd = os.open(self.root / "accounting-gap", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as marker:
                marker.write(b"Optional accounting is incomplete.\n")
        except OSError:
            pass

    def failure(self, identity, failed):
        key = hashlib.sha256(identity.encode()).hexdigest()
        with self.connection() as db:
            if not failed:
                db.execute("DELETE FROM failures WHERE key=?", (key,))
                return 0
            db.execute("INSERT INTO failures VALUES(?,1) ON CONFLICT(key) DO UPDATE SET count=count+1", (key,))
            return db.execute("SELECT count FROM failures WHERE key=?", (key,)).fetchone()[0]

    def report(self):
        try:
            with self.connection() as db:
                rows = db.execute("SELECT mode,COUNT(*),COALESCE(SUM(before_n),0),COALESCE(SUM(after_n),0),COALESCE(SUM(recovered_n),0) FROM events WHERE delivery_state='emitted' GROUP BY mode").fetchall()
                runs = db.execute("SELECT COALESCE(run_id,'unknown'),mode,COUNT(*),SUM(before_n-after_n-recovered_n) FROM events WHERE delivery_state='emitted' GROUP BY run_id,mode").fetchall()
                omitted = dict(db.execute("SELECT COALESCE(delivery_state,'legacy_unverified'),COUNT(*) FROM events WHERE delivery_state IS NULL OR delivery_state!='emitted' GROUP BY delivery_state").fetchall())
        except (sqlite3.Error, OSError):
            self.mark_accounting_gap()
            rows, runs, omitted = [], [], {}
        gap = self.accounting_gap or (self.root / "accounting-gap").exists()
        modes = {mode: {"events": n, "bytes_before": before, "bytes_delivered": after,
                        "bytes_recovered": recovery, "net_bytes_avoided": before - after - recovery,
                        "estimated_net_tokens_avoided": (before - after - recovery) / 4}
                 for mode, n, before, after, recovery in rows}
        return {"modes": modes, "runs": [{"run_id": run, "mode": mode, "events": count,
                                          "net_bytes_avoided": net} for run, mode, count, net in runs],
                "estimate_method": "UTF-8 bytes / 4; recovery charged against avoided bytes",
                "excluded_delivery_states": omitted,
                "measured_usage": None, "actual_cost_savings": None,
                "accounting_complete": False if gap else None,
                "accounting_status": "known gaps; totals are partial" if gap else "recorded events only; external coverage unverified",
                "note": "Local delivery estimates only. Shadow mode did not reduce context. Use Ork Meter receipts for native usage evidence."}
