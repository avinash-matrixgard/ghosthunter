"""MemPalace integration for Ghosthunter.

Architecture
------------
Ghosthunter is an MCP CLIENT; MemPalace runs as an MCP SERVER in a child
process (``python -m mempalace.mcp_server``). All calls go over stdio
using the official ``mcp`` Python SDK.

Both dependencies are OPTIONAL. If either is missing at import time,
``is_available()`` returns False and every operation becomes a no-op.
The chat REPL uses this as a feature flag — if the palace isn't
available, Ghosthunter works exactly as before with a soft hint in
the mode picker.

Storage lives under ``~/.ghosthunter/palace/`` (not MemPalace's default
``~/.mempalace/palace/``) so Ghosthunter remains self-contained.

Wing / room / hall mapping
--------------------------
MemPalace organizes memories spatially. We map Ghosthunter's concepts
onto the palace structure like this:

- **Wing**: the *billing account* the investigation was run against
  (parsed from filenames like ``Billing Account for [COMPANY]_Reports``).
  Falls back to ``default``.
- **Room**: the *service* or *project* name of the spike (e.g.
  ``Cloud DNS``, ``[PROJECT-DNS]``).
- **Hall**: the kind of memory:
    - ``facts``         — user-provided ground truth (`/remember`)
    - ``corrections``   — "X was wrong, Y is right"
    - ``conclusions``   — auto-saved root causes from concluded investigations
    - ``user_notes``    — notes dropped mid-investigation via `/note`

Concurrency model
-----------------
Each public method opens a fresh MCP stdio session, performs its call,
and closes the session. This trades performance (~500 ms per call) for
simplicity: no background threads, no shared state between the sync chat
REPL and the async MCP client.

If this becomes a bottleneck, later optimization will keep the session
open on a daemon thread with a command queue — but for the v1 use cases
(recall at the start of an investigation, save at the end, occasional
slash commands) the simple model is fine.

Tool name discovery
-------------------
MemPalace exposes 19 MCP tools. The exact tool names for search / save
need to be confirmed by running ``ghosthunter palace tools`` once
MemPalace is installed. ``_RESOLVED_TOOL_NAMES`` is populated at runtime
by caching the first successful ``list_tools`` call.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# --- Optional deps: guarded imports ----------------------------------------
try:
    from mcp import ClientSession, StdioServerParameters  # type: ignore
    from mcp.client.stdio import stdio_client  # type: ignore
    _HAS_MCP = True
except ImportError:
    _HAS_MCP = False


PALACE_ROOT = Path.home() / ".ghosthunter" / "palace"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class MemoryHit:
    """One result from a palace search."""
    content: str
    wing: str | None = None
    room: str | None = None
    hall: str | None = None
    score: float | None = None
    source: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PalaceStatus:
    available: bool
    reason: str = ""
    storage_path: Path = PALACE_ROOT
    server_pid: int | None = None
    tool_count: int = 0


# ---------------------------------------------------------------------------
# Tool name resolution (cached)
# ---------------------------------------------------------------------------
# Candidate tool names per logical operation. The first that exists in the
# connected server wins. Populated by `_discover_tool_names` on first use.
_TOOL_NAME_CANDIDATES: dict[str, tuple[str, ...]] = {
    # Search for relevant memories
    "search": (
        "mempalace_search",
        "search_memories",
        "palace_search",
        "search",
    ),
    # Add a new memory
    "remember": (
        "mempalace_remember",
        "add_memory",
        "palace_remember",
        "remember",
        "store_memory",
    ),
    # Server-level status / health
    "status": (
        "mempalace_status",
        "palace_status",
        "status",
    ),
}
_RESOLVED_TOOL_NAMES: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def is_available() -> bool:
    """Return True if both ``mcp`` and ``mempalace`` are importable.

    Doesn't actually start the server — for that use ``PalaceClient.status()``.
    """
    if not _HAS_MCP:
        return False
    # Cheap check: can we import the mempalace package?
    try:
        import importlib.util
        return importlib.util.find_spec("mempalace") is not None
    except Exception:
        return False


def parse_wing_from_filename(path: Path | str) -> str:
    """Parse a billing-account wing name from a Console export filename.

    Examples
    --------
    >>> parse_wing_from_filename("Billing Account for example.com_Reports, 2026-01-01.csv")
    'example.com'
    >>> parse_wing_from_filename("random.csv")
    'default'
    """
    name = Path(path).name
    m = re.match(r"Billing Account for\s+([^_]+)_Reports", name)
    if m:
        return m.group(1).strip()
    return "default"


def default_wing_for_files(files: Iterable[Path | str]) -> str:
    """Pick a single wing name for a bundle of billing files.

    If all files agree, use that. Otherwise fall back to ``default``.
    """
    wings = {parse_wing_from_filename(f) for f in files}
    wings.discard("default")
    if len(wings) == 1:
        return wings.pop()
    return "default"


# ---------------------------------------------------------------------------
# PalaceClient — the public API
# ---------------------------------------------------------------------------
class PalaceClient:
    """Thin MCP client wrapper around a spawned MemPalace server.

    All methods are SAFE to call even when MemPalace isn't installed —
    they become no-ops and return empty results.

    Usage
    -----
    >>> palace = PalaceClient()
    >>> if palace.status().available:
    ...     palace.remember("Cloud Run lives in [PROJECT]",
    ...                     wing="[COMPANY]", room="Cloud Run", hall="facts")
    ...     hits = palace.recall("where does Cloud Run live?", wing="[COMPANY]")
    ...     for h in hits:
    ...         print(h.content)
    """

    def __init__(self, storage_path: Path = PALACE_ROOT) -> None:
        self.storage_path = storage_path
        self.storage_path.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------
    # Status
    # --------------------------------------------------------------
    def status(self) -> PalaceStatus:
        """Probe whether the palace can actually start and list tools."""
        if not is_available():
            return PalaceStatus(
                available=False,
                reason=(
                    "mempalace and/or mcp not installed. "
                    "Run: .venv/bin/pip install mempalace mcp"
                ),
                storage_path=self.storage_path,
            )
        try:
            tools = asyncio.run(self._list_tools_once())
        except Exception as exc:  # noqa: BLE001
            return PalaceStatus(
                available=False,
                reason=f"failed to start mempalace server: {exc}",
                storage_path=self.storage_path,
            )
        return PalaceStatus(
            available=True,
            storage_path=self.storage_path,
            tool_count=len(tools),
        )

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the full tool catalog from the server."""
        if not is_available():
            return []
        try:
            return asyncio.run(self._list_tools_once())
        except Exception:
            return []

    # --------------------------------------------------------------
    # Remember / recall — the two operations chat.py actually uses
    # --------------------------------------------------------------
    def remember(
        self,
        content: str,
        wing: str,
        room: str | None = None,
        hall: str = "facts",
        source: str | None = None,
    ) -> bool:
        """Store a memory. Returns True on success, False if no-op/failure."""
        if not is_available() or not content.strip():
            return False
        try:
            return asyncio.run(
                self._remember_once(content, wing, room, hall, source)
            )
        except Exception:
            return False

    def recall(
        self,
        query: str,
        wing: str | None = None,
        room: str | None = None,
        n: int = 5,
    ) -> list[MemoryHit]:
        """Search memories. Returns at most ``n`` hits, ordered by relevance.

        Wing/room are advisory — some servers may not honor them as filters,
        in which case they're encoded into the query.
        """
        if not is_available() or not query.strip():
            return []
        try:
            return asyncio.run(self._recall_once(query, wing, room, n))
        except Exception:
            return []

    # --------------------------------------------------------------
    # Async internals — one session per call
    # --------------------------------------------------------------
    @staticmethod
    def _server_params():
        """Build the StdioServerParameters for spawning mempalace.

        Pin the palace storage path via env var so the server writes under
        ``~/.ghosthunter/palace/`` rather than its default.
        """
        env = os.environ.copy()
        env["MEMPALACE_PATH"] = str(PALACE_ROOT)
        return StdioServerParameters(
            command="python",
            args=["-m", "mempalace.mcp_server"],
            env=env,
        )

    async def _list_tools_once(self) -> list[dict[str, Any]]:
        async with stdio_client(self._server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                tools = []
                for t in getattr(result, "tools", []) or []:
                    tools.append(
                        {
                            "name": getattr(t, "name", ""),
                            "description": getattr(t, "description", ""),
                        }
                    )
                _cache_tool_names(tools)
                return tools

    async def _remember_once(
        self,
        content: str,
        wing: str,
        room: str | None,
        hall: str,
        source: str | None,
    ) -> bool:
        async with stdio_client(self._server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if not _RESOLVED_TOOL_NAMES:
                    _cache_tool_names(await _list_tools_via(session))
                tool = _RESOLVED_TOOL_NAMES.get("remember")
                if not tool:
                    return False
                arguments: dict[str, Any] = {
                    "content": content,
                    "wing": wing,
                    "hall": hall,
                }
                if room:
                    arguments["room"] = room
                if source:
                    arguments["source"] = source
                result = await session.call_tool(tool, arguments=arguments)
                return not getattr(result, "isError", False)

    async def _recall_once(
        self,
        query: str,
        wing: str | None,
        room: str | None,
        n: int,
    ) -> list[MemoryHit]:
        async with stdio_client(self._server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if not _RESOLVED_TOOL_NAMES:
                    _cache_tool_names(await _list_tools_via(session))
                tool = _RESOLVED_TOOL_NAMES.get("search")
                if not tool:
                    return []
                arguments: dict[str, Any] = {"query": query, "limit": n}
                if wing:
                    arguments["wing"] = wing
                if room:
                    arguments["room"] = room
                result = await session.call_tool(tool, arguments=arguments)
                return _parse_hits(result, n)


# ---------------------------------------------------------------------------
# Helpers used by async internals
# ---------------------------------------------------------------------------
async def _list_tools_via(session) -> list[dict[str, Any]]:
    result = await session.list_tools()
    return [
        {"name": getattr(t, "name", ""), "description": getattr(t, "description", "")}
        for t in getattr(result, "tools", []) or []
    ]


def _cache_tool_names(tools: list[dict[str, Any]]) -> None:
    """Look up each logical operation against the server's real tool list."""
    names = {t["name"] for t in tools}
    for op, candidates in _TOOL_NAME_CANDIDATES.items():
        if op in _RESOLVED_TOOL_NAMES:
            continue
        for candidate in candidates:
            if candidate in names:
                _RESOLVED_TOOL_NAMES[op] = candidate
                break


def _parse_hits(call_result: Any, n: int) -> list[MemoryHit]:
    """Extract MemoryHit objects from an MCP tool result.

    MCP tool results are a list of Content objects — usually text. MemPalace
    may return either structured JSON-in-text or typed content. We try both.
    """
    import json

    content_blocks = getattr(call_result, "content", []) or []
    hits: list[MemoryHit] = []
    for block in content_blocks:
        text = getattr(block, "text", None)
        if text is None:
            continue
        # Try to parse as JSON first — MemPalace often returns a list of dicts
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None

        if isinstance(parsed, list):
            for item in parsed[:n]:
                if isinstance(item, dict):
                    hits.append(
                        MemoryHit(
                            content=item.get("content") or item.get("text") or "",
                            wing=item.get("wing"),
                            room=item.get("room"),
                            hall=item.get("hall"),
                            score=item.get("score"),
                            source=item.get("source"),
                            raw=item,
                        )
                    )
        elif isinstance(parsed, dict) and "results" in parsed:
            for item in parsed["results"][:n]:
                if isinstance(item, dict):
                    hits.append(
                        MemoryHit(
                            content=item.get("content") or item.get("text") or "",
                            wing=item.get("wing"),
                            room=item.get("room"),
                            hall=item.get("hall"),
                            score=item.get("score"),
                            source=item.get("source"),
                            raw=item,
                        )
                    )
        else:
            # Plain text response: one big hit
            hits.append(MemoryHit(content=text))

    return hits[:n]
