"""Shared prompt_toolkit session for the chat REPL.

All user input across the project (`chat.py` REPL + `AdvisorProvider`
output prompt + ask-user flow) goes through the same `PromptSession`
so users get a consistent experience:

- **Enter**             — submit
- **Esc then Enter**    — insert a newline (Alt+Enter also works)
- **Ctrl+J**            — insert a newline (alternative)
- **Paste**             — bracketed paste captures multi-line content
                          without submitting mid-paste; press Enter when done
- **Up / Down**         — navigate persistent history
                          (stored at ``~/.ghosthunter/chat_history``)
- **Ctrl+R**            — reverse history search
- **Ctrl+C**            — cancel current input
- **Ctrl+D**            — EOF (exit chat session)

Why not Shift+Enter?
  Most terminals don't distinguish Shift+Enter from plain Enter — there's
  no standard escape sequence for it. Esc+Enter is universally supported.

The ``###`` paste-terminator is gone — bracketed paste mode handles
multi-line paste natively in any modern terminal (iTerm2, Terminal.app,
Alacritty, kitty, most Linux terminals).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

HISTORY_PATH = Path.home() / ".ghosthunter" / "chat_history"

_session: Optional[PromptSession] = None


def _build_key_bindings() -> KeyBindings:
    """Enter submits; Esc+Enter / Ctrl+J inserts a newline.

    Shift+Enter isn't universally supported in terminals (there's no
    standard escape sequence for it), so we don't bind it. Esc+Enter
    is the portable way to insert a newline in multiline mode.
    """
    kb = KeyBindings()

    @kb.add("enter", eager=True)
    def _submit(event) -> None:
        """Enter → submit the current buffer."""
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline_meta(event) -> None:
        """Esc+Enter (Alt+Enter, Meta+Enter) → insert newline."""
        event.current_buffer.insert_text("\n")

    @kb.add("c-j")
    def _newline_ctrl_j(event) -> None:
        """Ctrl+J → insert newline (alternative)."""
        event.current_buffer.insert_text("\n")

    return kb


def get_session() -> PromptSession:
    """Return the process-wide PromptSession, creating it on first use."""
    global _session
    if _session is not None:
        return _session

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)

    _session = PromptSession(
        history=FileHistory(str(HISTORY_PATH)),
        multiline=True,
        enable_history_search=True,
        key_bindings=_build_key_bindings(),
        mouse_support=False,
    )
    return _session


def read_line(prompt: str = "> ") -> str:
    """Prompt the user and return their input.

    Raises EOFError on Ctrl+D, KeyboardInterrupt on Ctrl+C.
    Bracketed-paste content is captured as a single multi-line string.
    """
    return get_session().prompt(prompt)
