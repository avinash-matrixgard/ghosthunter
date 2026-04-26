"""Layer 1: Fast-reject patterns.

These catch obvious shell-injection and dangerous binaries. They do NOT try
to catch destructive GCP verbs — that's the allowlist's job.
"""

import re

# Patterns that are unsafe regardless of where they appear in the string.
#
# Note: Command-substitution characters ``$(``, ``${``, and `` ` `` used
# to live here but now go through the quote-aware
# ``has_unquoted_command_substitution`` helper. They're still blocked
# when they appear outside single quotes — a necessary change because
# ``bq query`` SQL requires backticks around table refs like
# ``\`project.dataset.table\```, and Opus kept producing un-runnable
# SQL trying to avoid the false positive.
FAST_REJECT_PATTERNS: list[str] = [
    # Shell chaining
    r";",
    r"&&",
    r"\|\|",
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


def has_unquoted_command_substitution(command: str) -> str | None:
    """Return the offending pattern if `command` has shell command
    substitution outside of single quotes.

    Quote rules (matching bash semantics):
      - Inside single quotes: nothing expands; ``$(``, ``${``, and
        backtick are all literal. Safe.
      - Inside double quotes: bash still expands ``$(cmd)``, ``${var}``,
        and backtick-command — so they ARE dangerous here.
      - Unquoted: all three are shell expansion. Dangerous.

    We only give a safe pass inside *single* quotes, which is the
    canonical form BigQuery requires for fully-qualified table refs::

        bq query 'SELECT ... FROM ``proj.ds.tbl`` ...'

    (with real backticks around the table name inside the single-quoted
    SQL).

    Returns the pattern that triggered (``"`"``, ``"$("``, or
    ``"${"``), or None if the command is clean.
    """
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]

        if ch == "\\" and not in_single:
            # In unquoted and double-quoted contexts, backslash escapes
            # the next character. Skip the escaped char outright so an
            # escaped backtick / dollar can't trip us up.
            i += 2
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue

        # Inside single quotes → everything else is literal, skip.
        if in_single:
            i += 1
            continue

        # Outside single quotes — apply the substitution checks.
        if ch == "`":
            return "`"
        if ch == "$" and i + 1 < len(command):
            nxt = command[i + 1]
            if nxt == "(":
                return "$("
            if nxt == "{":
                return "${"

        i += 1

    return None
