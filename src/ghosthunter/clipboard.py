"""Clipboard helpers — OSC 52 auto-copy + OS-native fallback.

Ghosthunter proposes commands the user has to run in their own
terminal. Terminals have no concept of a "copy button" the way a
markdown-rendered IDE does, and triple-click behaviour on soft-wrapped
lines is terminal-dependent. Users reported that selecting multi-line
wrapped commands required awkward click-drag selection — this module
is the fix.

Two independent paths:

1. ``write_osc52`` — writes the given text into the user's system
   clipboard via the OSC 52 terminal escape sequence. Supported on
   iTerm2 (default-on), Kitty, WezTerm, Alacritty (with opt-in),
   Ghostty, and tmux (with ``set-clipboard on``). Silently no-ops on
   older macOS Terminal.app and any terminal that hasn't enabled
   OSC 52 — that's the design: we don't want to spam unrecognised
   escape codes to the user's shell history.

2. ``copy_to_clipboard`` — explicit, user-triggered copy (``/copy``).
   Tries OS-native tools first (pbcopy on macOS, xclip/wl-copy on
   Linux, clip.exe on Windows) then falls back to OSC 52. Returns
   True only if something plausibly succeeded.

Opt-out: set ``GHOSTHUNTER_NO_CLIPBOARD=1`` in the env and both
paths turn into no-ops. Useful in CI, in terminals where OSC 52
output is visible garbage instead of silent, or for security-paranoid
users who don't want their clipboard mutated by a tool.
"""

from __future__ import annotations

import base64
import os
import platform
import subprocess
import sys
from typing import IO

# Conservative cap — some terminals reject larger OSC 52 payloads. Our
# proposed commands are typically < 1 KB, so this just guards against
# an accidental huge string arriving.
_OSC52_MAX_BYTES = 8192

_ENV_OPT_OUT = "GHOSTHUNTER_NO_CLIPBOARD"


def _opted_out() -> bool:
    """True if the user has disabled clipboard integration via env."""
    value = os.environ.get(_ENV_OPT_OUT, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def write_osc52(text: str, *, stream: IO[str] | None = None) -> bool:
    """Write `text` to the terminal's system clipboard via OSC 52.

    Returns True if we emitted the escape sequence (which doesn't
    guarantee the terminal honoured it — that's the user's terminal
    configuration), False if we refused (opt-out, too large, no tty).

    Notes:
      - Must target the terminal (fd 1/2), not an arbitrary stream.
        We default to stdout; callers that only have access to a
        Rich Console must pass its underlying file.
      - ``c`` = the canonical clipboard target. Some terminals also
        accept ``p`` (primary) or ``s`` (selection); we stick with
        the universal one.
    """
    if _opted_out() or not text:
        return False
    try:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    except Exception:
        return False
    if len(encoded) > _OSC52_MAX_BYTES:
        return False

    out = stream if stream is not None else sys.stdout
    # Guard: only emit if the stream looks like a terminal. OSC 52
    # arriving on a redirected stdout would end up in a log file as
    # garbage, which is exactly the kind of debugging surprise we want
    # to avoid.
    try:
        if not out.isatty():
            return False
    except Exception:
        return False

    # ``\x1b]52;c;PAYLOAD\x07`` — the BEL variant. The ``\x1b\\``
    # variant (ST) also works but BEL is more widely supported.
    try:
        out.write(f"\x1b]52;c;{encoded}\x07")
        out.flush()
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# OS-native clipboard commands. Ordered by preference per platform.
# ---------------------------------------------------------------------------
def _native_clipboard_cmd() -> list[str] | None:
    """Return the argv of a system clipboard tool, or None if none found.

    Each tool reads the text to copy from its stdin — consistent across
    all three platforms.
    """
    system = platform.system()
    if system == "Darwin":
        return ["pbcopy"]
    if system == "Windows":
        return ["clip"]
    # Linux / BSD: prefer Wayland (wl-copy) then X11 (xclip, xsel).
    for candidate in (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ):
        # We don't actually run shutil.which here — subprocess will
        # raise FileNotFoundError which the caller catches — cheaper
        # to fail fast and try the next one. But we DO want to check
        # the first tool exists to report cleanly.
        return candidate  # first one wins; subprocess handles missing
    return None


def copy_to_clipboard(text: str, *, stream: IO[str] | None = None) -> tuple[bool, str]:
    """User-initiated copy (``/copy``). Returns ``(ok, mechanism)``.

    Tries OS-native tools first — these survive SSH without terminal
    clipboard-sync tricks and work regardless of the user's terminal —
    then falls back to OSC 52 for remote / unusual environments.

    ``mechanism`` is one of:
      - ``"pbcopy"`` / ``"wl-copy"`` / ``"xclip"`` / ``"xsel"`` / ``"clip"``
      - ``"osc52"`` — terminal escape fallback
      - ``"skipped"`` — opted out via env
      - ``"unavailable"`` — no mechanism worked
    """
    if _opted_out():
        return False, "skipped"
    if not text:
        return False, "unavailable"

    native = _native_clipboard_cmd()
    if native is not None:
        try:
            proc = subprocess.run(
                native,
                input=text,
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
            if proc.returncode == 0:
                return True, native[0]
        except FileNotFoundError:
            # Tool not installed. Fall through to OSC 52.
            pass
        except Exception:
            pass

    if write_osc52(text, stream=stream):
        return True, "osc52"

    return False, "unavailable"
