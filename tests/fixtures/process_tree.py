"""Synthetic owned process tree for cancellation; no external operations."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

role, directory = sys.argv[1:]
root = Path(directory)
def interrupted(signum, frame):
    (root / (role + ".ended")).write_text("signal received")
    print(role + " interrupted", flush=True)
    raise SystemExit(0)

signal.signal(signal.SIGBREAK if os.name == "nt" else signal.SIGINT, interrupted)
(root / (role + ".started")).write_text(str(os.getpid()))
print(role + " before interruption", flush=True)
if role == "parent":
    child = subprocess.Popen([sys.executable, __file__, "descendant", directory])
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
else:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not (root / "fixture.stop").exists():
        time.sleep(0.02)
