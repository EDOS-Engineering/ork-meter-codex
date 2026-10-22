"""Thin bridge to the reviewed Ork Meter helper. It owns all telemetry/auth."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
READ_ACTIONS = {"status": ["collect", "status"], "diagnose": ["collect", "diagnose", "--client", "codex"]}


def binary():
    """The helper to run: an explicit path, a development build, the verified
    release helper setup installed for this plugin version, then PATH."""
    explicit = os.environ.get("ORK_METER_BINARY")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("ORK_METER_BINARY must be an absolute installed helper path")
        return str(path)
    name = "ork-meter.exe" if os.name == "nt" else "ork-meter"
    local = ROOT / ".local" / name
    if local.is_file():
        return str(local)
    from .helper import installed
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    released = installed(home)
    found = released or shutil.which(name)
    if not found:
        raise ValueError("Ork Meter helper unavailable; run setup to install the reviewed release helper, or build a development helper with scripts/build_meter.py --platform-source ../ork-meter")
    return str(found)


def reference(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("provide the public pairing reference from Ork Meter")
    return value


def status(ref, diagnose=False):
    action = "diagnose" if diagnose else "status"
    result = subprocess.run([binary(), *READ_ACTIONS[action], "--reference", reference(ref)],
                            capture_output=True, timeout=20)
    if result.returncode:
        raise ValueError("Ork Meter authorization or connectivity unverified; check the helper terminal")
    return json.loads(result.stdout)


def command(action, ref=None, settings=None, config=None, output=None, confirm=False, dev=False):
    args = []
    if action == "prepare":
        args = ["pair", "prepare", "--source", "openai", "--client", "codex"]
        if dev:
            args += ["--origin", "https://dev.ork-meter.edos.io"]
    elif action in {"claim", "verify", "show"}:
        args = ["pair", action, "--reference", reference(ref)]
    elif action in {"capture", "status", "diagnose"}:
        args = ["collect", action]
        if action != "status":
            args += ["--client", "codex"]
        args += ["--reference", reference(ref)]
        if settings and action == "diagnose":
            args += ["--settings", settings]
    elif action in {"check-local", "progress", "retry"}:
        if not settings:
            raise ValueError("private settings path required; keep its content out of chat")
        args = ["collect", action, "--client", "codex", "--settings", settings]
    elif action in {"prepare-config", "prepare-removal"}:
        if not config or not output or (action == "prepare-config" and not settings):
            raise ValueError("config/output paths and capture settings required")
        args = ["collect", action, "--client", "codex", "--config", config, "--output", output]
        if action == "prepare-config":
            args += ["--settings", settings]
    elif action == "forget":
        if not confirm:
            raise ValueError("stop capture and revoke in the browser, then use --confirm-local-removal")
        args = ["pair", "forget", "--reference", reference(ref), "--confirm-local-removal"]
    else:
        raise ValueError("unsupported meter action")
    # Terminal streams pass through. A claim code is never captured or echoed
    # by this plugin and browser approval stays with the account holder.
    if action == "forget":
        from .entitlement import disconnect_guard, forget
        with disconnect_guard():
            code = subprocess.call([binary(), *args])
            if code == 0:
                forget(ref=ref)
            return code
    return subprocess.call([binary(), *args])
