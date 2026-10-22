"""Explicit, reversible installation of a user-level automatic output hook."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

from .storage import private_directory

MARKER = "Ork automatic output filtering"


def hook_command(argv, platform=None):
    if (platform or os.name) == "nt":
        # Windows hooks run in PowerShell; quoted paths need its call operator.
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


def configure(home, action):
    home = Path(home).expanduser().absolute()
    config = home / "hooks.json"
    if config.is_symlink():
        raise ValueError("Linked hooks configuration requires editing its target explicitly")
    if action == "status":
        document = json.loads(config.read_text()) if config.exists() else {}
        groups = document.get("hooks", {}).get("PostToolUse", [])
        return {"installed": any(h.get("statusMessage") == MARKER
                                 for g in groups for h in g.get("hooks", [])),
                "config": str(config), "trust": "Review and enable the hook in Codex /hooks"}
    if action not in {"install", "uninstall"}:
        raise ValueError("unsupported hook action")
    home.mkdir(parents=True, exist_ok=True)
    lock = home / "ork-auto-install.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("Another hook installation owns this profile") from None
    os.close(descriptor)
    try:
        original = config.read_bytes() if config.exists() else None
        document = json.loads(original) if original is not None else {}
        if not isinstance(document, dict) or not isinstance(document.get("hooks", {}), dict):
            raise ValueError("Invalid hooks configuration; left unchanged")
        groups = document.get("hooks", {}).get("PostToolUse", [])
        if not isinstance(groups, list) or any(not isinstance(g, dict) or
                not isinstance(g.get("hooks"), list) or
                any(not isinstance(h, dict) for h in g["hooks"]) for g in groups):
            raise ValueError("Invalid PostToolUse configuration; left unchanged")
        retained = []
        for group in groups:
            hooks = [h for h in group["hooks"] if h.get("statusMessage") != MARKER]
            if hooks or not group["hooks"]:
                retained.append({**group, "hooks": hooks})
        if action == "install":
            # Copy only package code and public vocabulary, never customer state.
            package = Path(__file__).resolve().parent
            files = sorted(p for p in package.rglob("*") if p.is_file() and
                           "__pycache__" not in p.parts and
                           (p.suffix == ".py" or p.parent.name == "data"))
            digest = hashlib.sha256()
            for source in files:
                digest.update(str(source.relative_to(package)).encode())
                digest.update(source.read_bytes())
            bundle = private_directory(home / "ork-auto" / digest.hexdigest()[:20])
            for source in files:
                target = bundle / "ork_codex" / source.relative_to(package)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or (target.exists() and target.read_bytes() != source.read_bytes()):
                    raise ValueError("Installed hook snapshot changed; configuration was left unchanged")
                if not target.exists():
                    shutil.copyfile(source, target)
            launcher = bundle / "run.py"
            launch_text = "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent))\nfrom ork_codex.auto_output import main\nmain()\n"
            if launcher.is_symlink() or (launcher.exists() and launcher.read_text() != launch_text):
                raise ValueError("Installed hook launcher changed; configuration was left unchanged")
            if not launcher.exists():
                launcher.write_text(launch_text)
            argv = [sys.executable, "-I", "-B", str(launcher)]
            command = hook_command(argv)
            retained.append({"matcher": "Bash", "hooks": [{"type": "command", "command": command,
                             "timeout": 15, "statusMessage": MARKER}]})
        elif retained == groups:
            return {"status": "already-uninstalled", "config": str(config)}
        document.setdefault("hooks", {})["PostToolUse"] = retained
        rendered = (json.dumps(document, indent=2) + "\n").encode()
        if original == rendered:
            return {"status": "already-installed", "config": str(config), "hook": str(launcher)}
        descriptor, name = tempfile.mkstemp(prefix=".ork-hooks-", dir=home)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            if (config.read_bytes() if config.exists() else None) != original:
                raise ValueError("Hooks changed during installation; retry with the current configuration")
            os.replace(name, config)
        finally:
            Path(name).unlink(missing_ok=True)
        return {"status": "installed" if action == "install" else "uninstalled", "config": str(config),
                "hook": str(launcher) if action == "install" else None,
                "trust": "Review and enable the hook in Codex /hooks" if action == "install" else None}
    finally:
        lock.unlink()


def install(home):
    return configure(home, "install")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    args = parser.parse_args()
    print(json.dumps(install(args.codex_home), indent=2))
