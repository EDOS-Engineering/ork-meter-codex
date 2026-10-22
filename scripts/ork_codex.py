"""Isolated entry point: import only this package, never the caller's cwd."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ork_codex.cli import main

if __name__ == "__main__":
    main()
