"""Freeze settings at process start; local counts are not provider billing."""
from dataclasses import dataclass
import os

@dataclass(frozen=True)
class Policy:
    mode: str = "compact"
    budget: int = 2048
    threshold: int = 4096
    test_filter: bool = True
    min_tokens_saved: int = 512
    keep: tuple[str, ...] = ()

    @classmethod
    def environment(cls):
        def integer(name, default, floor=1024):
            try:
                return min(1024 * 1024, max(floor, int(os.environ.get(name, default))))
            except ValueError:
                return default
        mode = os.environ.get("ORK_CODEX_MODE", "compact")
        if mode not in {"compact", "shadow", "off"}:
            mode = "compact"
        return cls(mode, integer("ORK_CODEX_BUDGET_BYTES", 2048),
                   integer("ORK_CODEX_MIN_BYTES", 4096),
                   os.environ.get("ORK_CODEX_TEST_FILTER", "on").lower() not in {"0", "off", "false", "no"},
                   integer("ORK_CODEX_MIN_TOKENS_SAVED", 512, 0))
