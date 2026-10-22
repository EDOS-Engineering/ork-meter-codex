"""Install automatic Codex output filtering in the selected profile."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ork_codex.auto_hook import main


if __name__ == "__main__":
    main()
