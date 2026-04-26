"""Layer 3: Pipe validation.

After the leading command is matched against the allowlist, any pipe
segments must target a known-safe utility (head, wc, jq, grep, ...).
"""

import re

SAFE_PIPE_TARGETS: list[str] = [
    r"^wc(\s+-[lwcm]+)?$",
    r"^sort(\s+-[rnukbf]+)?$",
    r"^uniq(\s+-c)?$",
    r"^head(\s+-?\d+)?$",
    r"^tail(\s+-?\d+)?$",
    r"^grep(\s+-[ivEclnH]+)?(\s+.+)?$",
    r"^cut(\s+.+)?$",
    r"^awk(\s+.+)?$",
    r"^tr(\s+.+)?$",
    r"^jq(\s+-[rsc]+)?(\s+.+)?$",
]

_COMPILED = [re.compile(p) for p in SAFE_PIPE_TARGETS]

# Explicitly forbidden pipe targets — listed for clarity even though
# anything not in SAFE_PIPE_TARGETS is rejected by default.
BLOCKED_PIPE_TARGETS = {
    "curl",
    "wget",
    "nc",
    "netcat",
    "bash",
    "sh",
    "zsh",
    "python",
    "python3",
    "node",
    "xargs",
    "tee",
    "dd",
    "mail",
    "sendmail",
    "ssh",
    "scp",
    "eval",
}


def validate_pipes(segments: list[str]) -> tuple[bool, str]:
    """Each pipe segment (after the head command) must be a safe target.

    `segments` is the list of pipe segments AFTER the head command.
    Empty list means no pipes — always valid.
    """
    for seg in segments:
        seg = seg.strip()
        if not seg:
            return False, "empty pipe segment"
        first_word = seg.split()[0]
        if first_word in BLOCKED_PIPE_TARGETS:
            return False, f"blocked pipe target: {first_word}"
        if not any(rx.match(seg) for rx in _COMPILED):
            return False, f"pipe target not in safe list: {first_word}"
    return True, ""


def split_pipes(command: str) -> list[str]:
    """Split a command on `|`, ignoring `|` inside quotes and `||` operators.

    Returns a list of segments. The first segment is the head command;
    subsequent segments are pipe targets.
    """
    segments: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
        elif ch == "|" and not in_single and not in_double:
            # `||` should have been caught by Layer 1; treat defensively here.
            if i + 1 < len(command) and command[i + 1] == "|":
                buf.append("||")
                i += 2
                continue
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return segments
