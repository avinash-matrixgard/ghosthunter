"""Layer 1: Fast-reject patterns.

These catch obvious shell-injection and dangerous binaries. They do NOT try
to catch destructive GCP verbs — that's the allowlist's job.
"""
import re

# Patterns that are unsafe regardless of where they appear in the string.
FAST_REJECT_PATTERNS: list[str] = [
    # Shell chaining
    r";",
    r"&&",
    r"\|\|",
    # Command substitution
    r"\$\(",
    r"`",
    r"\$\{",
    # Encoding tricks
    r"\\x[0-9a-fA-F]{2}",
    r"\\[0-7]{3}",
    r"%[0-9a-fA-F]{2}",
    # Dangerous binaries (word-boundary)
    r"\bcurl\b",
    r"\bwget\b",
    r"\bnc\b",
    r"\bnetcat\b",
    r"\bssh\b",
    r"\bscp\b",
    r"\brsync\b",
    r"\brm\b",
    r"\brmdir\b",
    r"\bmkdir\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bdd\b",
    r"\bpython\b",
    r"\bpython3\b",
    r"\bnode\b",
    r"\bbash\b",
    r"\bsh\b",
    r"\beval\b",
    r"\bbase64\b",
]

_COMPILED = [(re.compile(p), p) for p in FAST_REJECT_PATTERNS]


def find_fast_reject(command: str) -> str | None:
    """Return the offending pattern if `command` matches any fast-reject rule."""
    for regex, pattern in _COMPILED:
        if regex.search(command):
            return pattern
    return None


def has_unquoted_redirect(command: str) -> bool:
    """True if `command` contains a redirect operator (< > >>) outside of quotes.

    `>` and `<` are legal inside gcloud --filter expressions as comparison
    operators, so we only flag them when they appear in unquoted shell context.
    """
    in_single = False
    in_double = False
    for ch in command:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch in "<>" and not in_single and not in_double:
            return True
    return False
