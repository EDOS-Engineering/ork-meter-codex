"""Test fixture: an Ork Meter Plus entitlement, so reduction tests run entitled.

Savings levers are off without Ork Meter Plus (ork_codex/entitlement.py).
``install()`` points ``CODEX_HOME`` at a private temporary profile holding a
synthetic saved pairing reference and a fresh, valid ``entitlement.json``
(fresh, so no helper refresh is attempted), and returns the profile.
tests/test_entitlement.py covers the gate itself.
"""
import atexit
import json
import os
from pathlib import Path
import shutil
import tempfile
from ork_codex.storage import private_directory

ANSWER = {"optimize": True, "state": "active", "until": "2099-01-01T00:00:00Z", "checked_at": "2026-10-14T12:00:00Z"}


def write(home, answer=ANSWER, reference="synthetic-reference"):
    # Match setup's private ACL; plain mkdir inherits a shared Windows ACL
    # that disconnect correctly refuses to reuse.
    base = Path(home) / "ork-meter-codex"
    private_directory(base)
    if reference:
        (base / "pairing.json").write_text(json.dumps({"reference": reference, "dev": True}), encoding="utf-8")
    path = base / "entitlements" / reference / "entitlement.json" if reference else base / "entitlement.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if answer is not None:
        path.write_text(answer if isinstance(answer, str) else json.dumps(answer), encoding="utf-8")
        os.chmod(path, 0o600)
    return path


def install():
    home = tempfile.mkdtemp(prefix="ork-codex-plus-")
    atexit.register(shutil.rmtree, home, True)
    write(home)
    os.environ["CODEX_HOME"] = home
    return home
