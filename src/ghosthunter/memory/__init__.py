"""Memory integration for Ghosthunter.

Optional. If `mcp` and `mempalace` aren't installed, `get_palace()`
still works — it just returns a client whose methods are no-ops.
"""
from ghosthunter.memory.palace import (
    MemoryHit,
    PalaceClient,
    PalaceStatus,
    PALACE_ROOT,
    default_wing_for_files,
    is_available,
    parse_wing_from_filename,
)

__all__ = [
    "MemoryHit",
    "PalaceClient",
    "PalaceStatus",
    "PALACE_ROOT",
    "default_wing_for_files",
    "is_available",
    "parse_wing_from_filename",
    "get_palace",
]


_singleton: PalaceClient | None = None


def get_palace() -> PalaceClient:
    """Process-wide PalaceClient. Safe to call even when MemPalace is missing."""
    global _singleton
    if _singleton is None:
        _singleton = PalaceClient()
    return _singleton
