"""Fixtures inherit the workspace sandbox ACEs, unlike Windows mkdir(0700)."""
from pathlib import Path
import shutil
import uuid

ROOT = Path(__file__).resolve().parents[1] / ".local" / "tests"


class WorkspaceTemp:
    def __init__(self):
        self.path = ROOT / uuid.uuid4().hex
        self.path.mkdir(parents=True)
        self.name = str(self.path)

    def cleanup(self):
        target = self.path.resolve()
        if not target.is_relative_to(ROOT.resolve()) or target == ROOT.resolve():
            raise ValueError("invalid fixture cleanup path")
        if target.exists():
            shutil.rmtree(target)

    def __enter__(self):
        return self.name

    def __exit__(self, *args):
        self.cleanup()
