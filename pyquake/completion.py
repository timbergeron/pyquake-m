"""Context-aware console completion shared by both terminal editors."""

import os
from pathlib import Path
import re
import shlex
import time

if __package__:
    from .commands import quote_arg
    from .iplog import format_address
else:
    from commands import quote_arg
    from iplog import format_address


def token_start(text):
    """Locate the current token, respecting quoted spaces and separators."""
    start = 0
    quoted = escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char in "\r\n" or (not quoted and char in " \t;"):
            start = index + 1
    return start


def context_tokens(context):
    """Parse only the current command; comments suppress completion."""
    start = 0
    quoted = escaped = comment = False
    for index, char in enumerate(context):
        if char in "\r\n":
            start, quoted, escaped, comment = index + 1, False, False, False
        elif comment:
            continue
        elif escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and char == ";":
            start = index + 1
        elif (not quoted and context[index:index + 2] == "//"
              and (index == start or context[index - 1].isspace())):
            comment = True
    if comment:
        return None
    text = context[start:].lstrip()
    if text.startswith("]"):
        text = text[1:]
    if text.startswith("/") and not text.startswith("//"):
        text = text[1:]
    try:
        return shlex.split(text)
    except ValueError:
        return None


def matches(pool, partial):
    """QSS-M ranks exact matches, prefixes, then substrings in natural order."""
    needle = partial.casefold()
    unique = {}
    for name in pool:
        key = name.casefold()
        if needle in key:
            unique.setdefault(key, name)

    def order(name):
        key = name.casefold()
        rank = 0 if key == needle else 1 if key.startswith(needle) else 2
        natural = tuple((1, int(part)) if part.isdigit() else (0, part)
                        for part in re.split(r"(\d+)", key))
        return rank, natural

    return sorted(unique.values(), key=order)


def _files(partial, suffixes):
    # Scan only the requested directory, never the whole tree on a keystroke.
    directory, _ = os.path.split(partial)
    base = Path(directory or ".")
    try:
        return [os.path.join(directory, entry.name) + ("/" if entry.is_dir() else "")
                for entry in base.iterdir()
                if not entry.name.startswith(".")
                and (entry.is_dir() or entry.suffix.casefold() in suffixes)]
    except OSError:
        return []


def _cvar_values(session, variable, partial):
    if variable is None or variable.flags & session.api.CvarFlags.ROM:
        return []
    name = variable.name.casefold()
    if variable.completion is not None:
        return variable.completion(variable, partial)
    values = {
        "cl_chatmode": ("0", "1", "2", "3"),
        "cl_autodemo": ("0", "1", "2", "3", "4"),
        "cl_demo_format": ("dem", "dz"),
        "cl_demo_minframes": ("0", "72", "720", "2160", "4320", "-72", "-720", "-2160", "-4320"),
        # This client accepts the basic palette, so every suggestion is usable.
        "topcolor": tuple(map(str, range(14))),
        "bottomcolor": tuple(map(str, range(14))),
    }
    if name == "name":
        return [variable.string, *session.name_history]
    return values.get(name, ())


def complete(session, context, prefix):
    tokens = context_tokens(context)
    if tokens is None:
        return []
    quoted = prefix.startswith('"')
    try:
        partial = shlex.split(prefix)[0] if prefix else ""
    except ValueError:
        try:
            partial = shlex.split(prefix + '"')[0]
        except (ValueError, IndexError):
            return []
    q = session.api
    if not tokens:
        # Empty root completion has no useful domain and QSS-M leaves it alone.
        if not partial:
            return []
        lead = "/" if partial.startswith("/") else ""
        partial = partial.removeprefix("/")
        return [lead + name for name in matches(
            q._tabcomp__all_command_names() + q._tabcomp__all_cvar_names()
            + q._tabcomp__all_alias_names(), partial)]

    command, position = tokens[0].casefold(), len(tokens)
    variable = session.cvars.find(command)
    pool = []
    if variable is not None:
        if position == 1:
            pool = _cvar_values(session, variable, partial)
    elif command in session.cvars._command_completions:
        pool = session.cvars._command_completions[command](tokens[1:], partial)
    elif command == "connect" and position == 1:
        pool = [entry["address"] for entry in session.server_history]
    elif command in ("set", "seta", "toggle", "reset", "unset"):
        if position == 1:
            pool = q._tabcomp__all_cvar_names()
            if command in ("set", "seta"):
                pool = [name for name in pool
                        if not session.cvars.find(name).flags & q.CvarFlags.ARCHIVE]
        elif position == 2 and command in ("set", "seta", "toggle"):
            pool = _cvar_values(session, session.cvars.find(tokens[1]), partial)
    elif command in ("alias", "unalias", "aliasedit") and position == 1:
        pool = q._tabcomp__all_alias_names()
    elif command in ("cmd", "rcon"):
        # cmd itself accepts the same command-name domain as QSS-M.
        if position == 1:
            pool = q._tabcomp__all_command_names() + q._tabcomp__all_cvar_names() + q._tabcomp__all_alias_names()
            if command == "cmd":
                pool += ["protocols", "pext"]
        else:
            return complete(session, " ".join(quote_arg(token) for token in tokens[1:]) + " ", prefix)
    elif command in ("tell", "dm", "pm", "identify") and position == 1:
        active = session.client
        if active is not None and not active.disconnected:
            players = session._identity_players(active)
            players.update({slot: record.name for slot, record in active._players.items()
                            if slot not in players and slot not in active._score_state})
            pool = [session._iplog_printable(name) for name in players.values() if name]
            if partial.startswith("#"):
                pool = [f"#{slot + 1}" for slot in players]
        if command == "identify" and partial and (partial[0].isdigit() and "." in partial):
            pool = [format_address(address) for address, _ in session.iplog.entries()]
    elif command == "color" and position in (1, 2):
        pool = list(map(str, range(14)))
    elif command in ("ip", "extip") and position == 1:
        pool = ["ext", "local"]
    elif command in ("exec", "execconfig", "writeconfig") and position == 1:
        pool = _files(partial, {".cfg"})
        if command == "writeconfig" and not os.path.dirname(partial):
            pool += ["config.cfg", "qssm.cfg", time.strftime("config-%m-%d-%Y-%H%M%S.cfg")]
        elif command != "writeconfig" and not os.path.dirname(partial):
            pool += ["default.cfg"]
    elif command in ("demo", "record") and position == 1:
        active = session.client
        if active is not None and not active.disconnected:
            name = active.models[0] if active.models else "unknown"
            name = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(name).stem)
            pool = [name + time.strftime("_%m-%d-%Y-%H%M%S")]
    elif command == "recoverdemo" and position in (1, 2):
        pool = _files(partial, {".dem"})
    elif command in ("ipmerge", "ipdump", "condump") and position == 1:
        pool = _files(partial, {".dat"} if command == "ipmerge" else {".txt"})
    elif command == "download":
        if position == 2:
            pool = ["auto", "web", "server"]
        elif position == 1:
            active = session.client
            if active is not None and not active.disconnected:
                pool = [name for name in active.models or () if not name.startswith("*")]
                pool += ["sound/" + name for name in active.sounds or ()]
    elif command == "net_stats" and position == 1:
        pool = ["*"]
    elif re.fullmatch(r"say_rand(?:om)?[0-9]", command) and position == 1:
        pool = ["team", "-t"]

    candidates = matches(pool, partial)
    result = []
    for name in candidates:
        if quoted or any(char.isspace() or char in ';"\\' for char in name):
            name = quote_arg(name)
            if name.endswith('/"'):
                name = name[:-1]  # Keep a directory open for the next path segment.
        result.append(name)
    return result
