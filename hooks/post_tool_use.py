"""Compatibility entry point for the packaged automatic output handler."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ork_codex.auto_output import command_kind, handle, main, response_text

if __name__ == "__main__":
    main()
