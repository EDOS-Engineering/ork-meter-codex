"""Synthetic capture process: no account, network or real credential."""
from pathlib import Path
import signal
import sys
import time

fragment = Path(sys.argv[1])
marker = Path(sys.argv[2])


def stop(*_):
    fragment.unlink(missing_ok=True)
    marker.write_text('stopped', encoding='utf-8')
    raise SystemExit(0)


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
if hasattr(signal, 'SIGBREAK'):
    signal.signal(signal.SIGBREAK, stop)
fragment.write_text('synthetic only', encoding='utf-8')
print(f'Codex telemetry fragment: {fragment}', flush=True)
while True:
    time.sleep(.05)
