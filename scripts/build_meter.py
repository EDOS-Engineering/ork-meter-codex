"""Build a DEVELOPMENT helper from the sibling platform; no release publication."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

p = argparse.ArgumentParser()
p.add_argument("--platform-source", required=True, type=Path)
p.add_argument("--go", default="go")
args = p.parse_args()
source = args.platform_source.resolve()
if not (source / "cmd/ork-meter/main.go").is_file():
    p.error("source must contain the existing Ork Meter helper")
root = Path(__file__).resolve().parents[1] / ".local"
root.mkdir(exist_ok=True)
cache = root / "go-cache"
scratch = root / "go-tmp"
cache.mkdir(exist_ok=True)
scratch.mkdir(exist_ok=True)
name = "ork-meter.exe" if os.name == "nt" else "ork-meter"
dest = root / name
env = dict(os.environ, GOCACHE=str(cache), GOTMPDIR=str(scratch), GOTOOLCHAIN="local")
subprocess.run([args.go, "build", "-mod=readonly", "-trimpath", "-o", str(dest), "./cmd/ork-meter"], cwd=source, env=env, check=True)
revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=source))
evidence = {"source_repository": "EDOS-Engineering/ork-meter", "source_revision": revision,
            "source_dirty": dirty, "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
            "go_version": subprocess.check_output([args.go, "version"], text=True).strip(),
            "purpose": "development integration only"}
(root / "meter-build.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
print("Development helper built. Source and SHA-256 recorded in .local/meter-build.json.")
