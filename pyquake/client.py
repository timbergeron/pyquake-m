# Copyright (c) 2024 Matthew Earl
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
#     The above copyright notice and this permission notice shall be included
#     in all copies or substantial portions of the Software.
#
#     THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
#     OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
#     MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
#     NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
#     DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
#     OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
#     USE OR OTHER DEALINGS IN THE SOFTWARE.

import argparse
import asyncio
import dataclasses
import logging
import platform
import math
import re
import struct
import time
import random
import inspect
import threading

import shlex
import subprocess
import os
import ctypes
import socket
import sys
import urllib.request
import urllib.error
import ipaddress
# Optional line-editing/clipboard helpers for aliasedit
try:
    import readline as _readline
    import functools as _functools
except Exception:
    _readline = None
import json
import plistlib
import pathlib
import locale
import shutil
from typing import Optional, Callable, Any, Tuple
from enum import Flag, Enum, auto
from threading import Event

if __package__:
    from . import aiodgram, proto
    from .iplog import IpLog, StatusParser
    from .commands import define_alias, split_commands, quote_arg
    from .console import ConsoleSession, SERVER_COMMANDS
else:
    import aiodgram
    import proto
    from iplog import IpLog, StatusParser
    from commands import define_alias, split_commands, quote_arg
    from console import ConsoleSession, SERVER_COMMANDS
from typing import Union

# --- Windows UDP reliability: use SelectorEventLoop instead of Proactor ---
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# --- Inline InfoString helpers (no separate module needed) ---
def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    return s

class InfoString(dict):
    """Quake \\key\\value string with dict-like access."""
    @classmethod
    def parse(cls, s: Union[str, bytes]) -> "InfoString":
        if isinstance(s, bytes):
            s = s.decode("latin-1", "ignore")
        s = _strip_quotes(s)
        if s.startswith("\\"):
            s = s[1:]
        parts = s.split("\\") if s else []
        it = iter(parts)
        d: dict[str, str] = {}
        for k, v in zip(it, it):
            if k:
                d[k] = v
        return cls(d)

    def to_string(self) -> str:
        flat = []
        for k, v in self.items():
            flat.extend(("\\", k, "\\", v))
        return "".join(flat)

    def get_key(self, key: str, default: str = "") -> str:
        return str(self.get(key, default))

def info_get_key(src: Union["InfoString", str, bytes], key: str, default: str = "") -> str:
    if not isinstance(src, InfoString):
        src = InfoString.parse(src)
    return src.get_key(key, default)

def info_set_key(dst: Union["InfoString", dict], key: str, value: str) -> None:
    """Helper to mirror Info_SetKey semantics (no size limit; backslash-safe via InfoString)."""
    if not isinstance(dst, InfoString):
        # Promote plain dict to InfoString so .to_string() etc. work uniformly
        d = InfoString()
        d.update(dst or {})
        dst.clear()
        dst.update(d)
    dst[key] = value


# ---- NetQuake control (RCON) ----
NETFLAG_LENGTH_MASK = 0x0000_FFFF
NETFLAG_CTL         = 0x8000_0000
CCREQ_RCON          = 0x05  # per your net_defs.h

def _nq_pack_control(body: bytes) -> bytes:
    """Big-endian (NETFLAG_CTL | len) + body; len includes 4-byte header."""
    total_len = 4 + len(body)
    hdr = struct.pack(">I", NETFLAG_CTL | (total_len & NETFLAG_LENGTH_MASK))
    return hdr + body

def _nq_rcon_body(password: str, args: str) -> bytes:
    # MSG_WriteByte(CCREQ_RCON); MSG_WriteString(pw); MSG_WriteString(args)
    def z(s: str) -> bytes:
        return s.encode("latin-1", "ignore") + b"\x00"
    return bytes([CCREQ_RCON]) + z(password) + z(args)


# =============================== CVAR SYSTEM ===============================
class CvarFlags(Flag):
    NONE = 0
    ARCHIVE = 1           # Save to config file
    NOTIFY = 2            # Broadcast changes to listeners
    SERVERINFO = 4        # (server use; no-op client-side)
    USERINFO = 8          # Include in user info -> setinfo
    ROM = 16              # Read-only
    LOCKED = 32           # Temporarily locked
    REGISTERED = 64       # Has been registered
    CALLBACK = 128        # Has callback function
    USERDEFINED = 256     # Created by user/mod
    SETA = 512            # Save with 'seta' command
    CHANGED = 1024        # Value was changed
    AUTOCVAR = 2048       # Auto-sync with QC globals (reserved)
    TEMP = 4096           # Do not persist to config (engine-only)
    USERDEFINED_SETA = USERDEFINED | SETA

class CvarType(Enum):
    STRING = auto()
    FLOAT  = auto()
    INT    = auto()
    BOOL   = auto()

class Cvar:
    def __init__(self, name: str, string_value: str,
                 flags: CvarFlags = CvarFlags.NONE,
                 callback: Optional[Callable[['Cvar', str, str], None]] = None,
                 cvar_type: CvarType = CvarType.FLOAT,
                 minimum: float | None = None,
                 maximum: float | None = None,
                 description: str = ""):
        self.name = name
        self._canon = name.lower()
        self.string = str(string_value)
        self.flags = flags
        # Preserve the exact default (even an empty string) and never mutate it later.
        self.default_string = str(string_value)
        self.callback = callback
        self.type = cvar_type
        self.minimum = minimum
        self.maximum = maximum
        self.description = description
        self._modified = False
        self.value = 0.0
        self._update_value()

    def _update_value(self):
        s = self.string
        if self.type is CvarType.BOOL:
            v = str(s).strip().lower()
            self.value = 1.0 if v in ("1","true","on","yes","y") else 0.0
        elif self.type is CvarType.INT:
            try:
                self.value = float(int(float(s)))
            except Exception:
                self.value = 0.0
        elif self.type is CvarType.STRING:
            # Keep numeric .value stable/harmless for strings.
            self.value = 0.0
        elif self.type is CvarType.FLOAT:
            try:
                self.value = float(s)
            except Exception:
                self.value = 0.0
        else:
            try:
                self.value = float(s)
            except Exception:
                self.value = 0.0
        if not math.isfinite(self.value):
            self.value = 0.0

    def _format_float(self, value: float) -> str:
        if not math.isfinite(value):
            return "0"
        if value == int(value):
            return str(int(value))
        s = f"{value:.6f}".rstrip('0').rstrip('.')
        return s if s else '0'

    def _coerce_and_clamp(self, new_str: str) -> str:
        if self.type is CvarType.STRING:
            return str(new_str)
        if self.type is CvarType.BOOL:
            v = 1.0 if str(new_str).strip().lower() in ("1","true","on","yes","y") else 0.0
        elif self.type is CvarType.INT:
            try:
                v = float(int(float(new_str)))
            except Exception:
                v = 0.0
        else:
            try:
                v = float(new_str)
            except Exception:
                v = 0.0
        if not math.isfinite(v):
            v = 0.0
        if self.minimum is not None and v < self.minimum:
            v = self.minimum
        if self.maximum is not None and v > self.maximum:
            v = self.maximum
        if self.type is CvarType.BOOL:
            return "1" if v != 0.0 else "0"
        if self.type is CvarType.INT:
            return str(int(v))
        return self._format_float(v)

    def set_quick(self, value: str) -> bool:
        if self.flags & (CvarFlags.ROM | CvarFlags.LOCKED):
            return False
        if not (self.flags & CvarFlags.REGISTERED):
            return False
        new_s = self._coerce_and_clamp(value)
        if self.string == new_s:
            return True
        old_string = self.string
        self.string = new_s
        self._update_value()
        self.flags |= CvarFlags.CHANGED
        self._modified = True
        if self.callback:
            self.callback(self, old_string, self.string)
        return True

    def set_value_quick(self, value: float) -> bool:
        return self.set_quick(self._format_float(value))

    def reset(self) -> bool:
        return self.set_quick(self.default_string)

    def as_float(self) -> float: return float(self.value)
    def as_int(self) -> int: return int(self.value)
    def as_bool(self) -> bool: return bool(int(self.value))

class CvarSystem:
    def __init__(self):
        self._cvars: dict[str, Cvar] = {}
        self._aliases: dict[str, str] = {}  # alias -> canonical name
        self._commands: dict[str, Callable] = {}
        self.command_aliases: dict[str, str] = {}
        self._config_file = "config.cfg"
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Cvar, str, str], None]] = []
        self._userinfo_dirty = False
        self._serverinfo_dirty = False
        self._last_write_snapshot: dict[str, str] = {}
        # CVARs we've retired and never want to (re)surface
        self._retired: set[str] = {"sayrand_dir", "cl_chat_notify", "cl_chatinfo_mode"}

    @staticmethod
    def _canon(name: str) -> str: return name.lower()

    def subscribe(self, fn: Callable[[Cvar, str, str], None]) -> None:
        self._listeners.append(fn)

    def unsubscribe(self, fn: Callable[[Cvar, str, str], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def register(self, cvar: Cvar) -> bool:
        with self._lock:
            k = self._canon(cvar.name)
            # Don't allow retired names to register
            if k in self._retired:
                return False
            if k in self._cvars:
                print(f"Can't register variable {cvar.name}, already defined")
                return False
            if k in self._commands:
                print(f"Cvar_RegisterVariable: {cvar.name} is a command")
                return False
            self._cvars[k] = cvar
            cvar.flags |= CvarFlags.REGISTERED
            env_key = f"CVAR_{cvar.name}".upper()
            if env_key in os.environ:
                cvar.set_quick(os.environ[env_key])
            if cvar.flags & CvarFlags.USERINFO: self._userinfo_dirty = True
            if cvar.flags & CvarFlags.SERVERINFO: self._serverinfo_dirty = True
            return True

    def create(self, name: str, default_value: str, flags: CvarFlags = CvarFlags.NONE,
               cvar_type: CvarType = CvarType.FLOAT) -> Optional[Cvar]:
        with self._lock:
            # Block creation of retired CVARs
            if self._canon(name) in self._retired:
                return None
            existing = self.find(name)
            if existing:
                return existing
            if self._canon(name) in self._commands:
                return None
            cvar = Cvar(name, default_value, flags | CvarFlags.USERDEFINED, cvar_type=cvar_type)
            if self.register(cvar):
                return cvar
            return None

    def register_alias(self, cvar: Cvar, alias_name: str) -> bool:
        with self._lock:
            a = self._canon(alias_name)
            if a in self._cvars or a in self._aliases:
                print(f"Can't register alias {alias_name}, already defined")
                return False
            if self.find(cvar.name) != cvar:
                print(f"Can't register alias {alias_name}, cvar {cvar.name} not properly registered")
                return False
            if a in self._commands:
                print(f"Cvar_RegisterAlias: {alias_name} is a command")
                return False
            self._aliases[a] = self._canon(cvar.name)
            return True

    def find(self, name: str) -> Optional[Cvar]:
        with self._lock:
            k = self._canon(name)
            if k in self._cvars:
                return self._cvars[k]
            if k in self._aliases:
                return self._cvars.get(self._aliases[k])
            return None

    def set(self, name: str, value: str) -> bool:
        with self._lock:
            cvar = self.find(name)
            if not cvar:
                print(f"Cvar_Set: variable {name} not found")
                return False
            old = cvar.string
            ok = self.set_quick(cvar, value)
            if ok:
                if cvar.flags & CvarFlags.USERINFO: self._userinfo_dirty = True
                if cvar.flags & CvarFlags.SERVERINFO: self._serverinfo_dirty = True
                if old != cvar.string and cvar.flags & (CvarFlags.NOTIFY | CvarFlags.USERINFO):
                    for fn in tuple(self._listeners):
                        try: fn(cvar, old, cvar.string)
                        except Exception: pass
            return ok

    def set_quick(self, cvar: Cvar, value: str) -> bool:
        ok = cvar.set_quick(value)
        if ok:
            if cvar.flags & CvarFlags.USERINFO: self._userinfo_dirty = True
            if cvar.flags & CvarFlags.SERVERINFO: self._serverinfo_dirty = True
        return ok

    def set_value(self, name: str, value: float) -> bool:
        return self.set(name, self._format_float(value))

    def set_value_quick(self, cvar: Cvar, value: float) -> bool:
        return self.set_quick(cvar, self._format_float(value))

    def set_rom(self, name: str, value: str) -> bool:
        with self._lock:
            cvar = self.find(name)
            if not cvar:
                return False
            cvar.flags &= ~CvarFlags.ROM
            result = self.set_quick(cvar, value)
            cvar.flags |= CvarFlags.ROM
            return result

    def set_value_rom(self, name: str, value: float) -> bool:
        with self._lock:
            cvar = self.find(name)
            if not cvar:
                return False
            cvar.flags &= ~CvarFlags.ROM
            result = self.set_value_quick(cvar, value)
            cvar.flags |= CvarFlags.ROM
            return result

    def get_value(self, name: str, default: float = 0.0) -> float:
        cvar = self.find(name)
        return cvar.value if cvar else default

    def get_bool(self, name: str, default: bool = False) -> bool:
        cvar = self.find(name)
        return cvar.as_bool() if cvar else default

    def get_string(self, name: str, default: str = "") -> str:
        cvar = self.find(name)
        return cvar.string if cvar else default

    def lock_var(self, name: str) -> bool:
        with self._lock:
            cvar = self.find(name)
            if cvar:
                cvar.flags |= CvarFlags.LOCKED
                return True
            return False

    def unlock_var(self, name: str) -> bool:
        with self._lock:
            cvar = self.find(name)
            if cvar:
                cvar.flags &= ~CvarFlags.LOCKED
                return True
            return False

    def unlock_all(self):
        with self._lock:
            for cvar in self._cvars.values():
                cvar.flags &= ~CvarFlags.LOCKED

    def reset(self, name: str) -> bool:
        with self._lock:
            cvar = self.find(name)
            if not cvar:
                print(f"variable '{name}' not found")
                return False
            return self.set(cvar.name, cvar.default_string)

    def reset_all(self):
        with self._lock:
            for cvar in self._cvars.values():
                self.reset(cvar.name)

    def complete_variable(self, partial: str, limit: int = 16) -> list[str]:
        if not partial:
            return []
        p = self._canon(partial)
        # Include aliases in results
        names = {c.name: k for k, c in self._cvars.items()}
        for a, k in self._aliases.items():
            names.setdefault(self._cvars[k].name, k)
        exact = [n for n, k in names.items() if k == p]
        pref  = [n for n, k in names.items() if k.startswith(p) and n not in exact]
        out = exact + sorted(pref, key=str.lower)
        return out[:limit]

    def command(self, command_args: list[str]) -> bool:
        if not command_args:
            return False
        var_name = command_args[0]
        cvar = self.find(var_name)
        if not cvar:
            return False
        if len(command_args) == 1:
            if cvar.string == cvar.default_string:
                print(f'"{cvar.name}" is "{cvar.string}" (default)')
            else:
                # Show the default even when it's the empty string
                print(f'"{cvar.name}" is "{cvar.string}" (default: "{cvar.default_string}")')
            return True
        new_value = ' '.join(command_args[1:])
        self.set(cvar.name, new_value)
        return True

    def list_cvars(self, pattern: str = "", with_flags: CvarFlags = CvarFlags.NONE) -> list[tuple]:
        with self._lock:
            result = []
            # Iterate cvar VALUES and sort by name
            for cvar in sorted(self._cvars.values(), key=lambda c: c.name.lower()):
                # Hide retired CVARs from the list
                if self._canon(cvar.name) in self._retired:
                    continue
                name = cvar.name
                if pattern and pattern not in name:
                    continue
                if with_flags and not (cvar.flags & with_flags):
                    continue
                archive_mark = "*" if cvar.flags & CvarFlags.ARCHIVE else " "
                notify_mark = "s" if cvar.flags & CvarFlags.NOTIFY else " "
                result.append((f"{archive_mark}{notify_mark}", name, cvar.string))
            return result

    def write_variables(self, filename: Optional[str] = None) -> bool:
        """Save archived cvars and, when enabled, command aliases."""
        if filename is None:
            filename = self._config_file
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                # Alphabetical output -> bottomcolor, name, topcolor, etc.
                for cvar in sorted(self._cvars.values(), key=lambda c: c.name.lower()):
                    if cvar.flags & CvarFlags.ARCHIVE:
                        command = "seta " if cvar.flags & CvarFlags.USERDEFINED else ""
                        f.write(f'{command}{cvar.name} {quote_arg(cvar.string)}\n')
                if self.get_bool("cfg_save_aliases", True):
                    for name, body in sorted(self.command_aliases.items(), key=lambda pair: pair[0].casefold()):
                        body = "; ".join(split_commands(body))
                        f.write(f"alias {quote_arg(name)} {quote_arg(body)}\n")
            self._last_write_snapshot = {c.name: c.string for c in self._cvars.values()}
            return True
        except Exception as e:
            print(f"Error writing config: {e}")
            return False

    async def load_config(self, filename: Optional[str] = None) -> bool:
        """Execute a config through the same dispatcher as console commands."""
        filename = self._config_file if filename is None else filename
        dispatcher = _get_console_session().dispatcher
        ok = dispatcher.exec_file(filename)
        await dispatcher.flush()
        return ok

    def _format_float(self, value: float) -> str:
        if not math.isfinite(value):
            return "0"
        if value == int(value):
            return str(int(value))
        return f"{value:.6f}".rstrip('0').rstrip('.')

    def register_command(self, name: str, func: Callable):
        self._commands[self._canon(name)] = func

    def unregister_command(self, name: str):
        self._commands.pop(self._canon(name), None)

    # --- NEW: support removing a CVAR cleanly ---
    def unregister_variable(self, name: str) -> bool:
        """Remove a CVAR (and any alias pointing to it) if present.
        Accepts either the canonical name or an alias."""
        with self._lock:
            k = self._canon(name)
            # If caller passed an alias, resolve it to the canonical key.
            if k in self._aliases:
                k = self._aliases[k]
            # Remove any aliases that point at this canonical name.
            for a, target in list(self._aliases.items()):
                if target == k:
                    self._aliases.pop(a, None)
            if k in self._cvars:
                del self._cvars[k]
                return True
            return False

    # Info helpers
    def userinfo(self) -> dict[str, str]:
        return {c.name: c.string for c in self._cvars.values() if c.flags & CvarFlags.USERINFO}
    def serverinfo(self) -> dict[str, str]:
        return {c.name: c.string for c in self._cvars.values() if c.flags & CvarFlags.SERVERINFO}
    def build_info_string(self, which: str = "user") -> str:
        d = self.userinfo() if which == "user" else self.serverinfo()
        parts: list[str] = []
        for k in sorted(d.keys(), key=str.lower):
            parts.extend([f"\\{k}", f"\\{d[k]}"])
        return "".join(parts)
    def clear_changed(self):
        for c in self._cvars.values():
            c.flags &= ~CvarFlags.CHANGED
            c._modified = False
        self._userinfo_dirty = self._serverinfo_dirty = False

# CVAR system will be initialized in _aioclient() to prevent
# "variable not found" messages during config loading

# ============================ END CVAR SYSTEM ==============================


# ---------------- Console buffer (Con_Clear_f-style) ----------------
class _ConsoleBuffer:
    """
    Minimal in-memory console buffer to mirror Con_Clear_f semantics:
      - keep a list of lines printed through this console
      - maintain a backscroll pointer (not used yet, but reset on clear)
      - clear() empties the buffer and clears the terminal silently
    """
    def __init__(self):
        self.lines: list[str] = []
        self.backscroll: int = 0
        # --- line-stitching state ---
        self._line_open: bool = False         # currently appending a line to the terminal
        self._pending_line: str = ""          # accumulated text for current line

    def _finalize_line(self, *, force: bool = False):
        """Emit a newline if a stitched line is open, store it in history, and reset."""
        if not self._line_open and not force:
            return
        # Finish the terminal line
        sys.stdout.write("\n")
        sys.stdout.flush()
        # Remember it as a discrete console line
        self.lines.append(self._pending_line)
        # Reset state
        self._pending_line = ""
        self._line_open = False

    def newline(self):
        """Emit one explicit newline, including an empty line when none is open."""
        self._finalize_line(force=True)

    def append(self, s: str, prefix: str = ""):
        """
        Append a chunk to the current console line without forcing a newline.
        If no line is open, start one (optionally with a prefix, printed once).
        """
        if s is None:
            return
        s = str(s)
        if not self._line_open:
            # Start a new visible line (emit prefix once)
            if prefix:
                sys.stdout.write(prefix)
                self._pending_line = prefix
            self._line_open = True
        # Append the chunk verbatim (no extra spaces; we preserved them upstream)
        sys.stdout.write(s)
        sys.stdout.flush()
        self._pending_line += s

    def write(self, s: str):
        # Suppress certain noisy diagnostics unless developer is enabled.
        try:
            if not _dev_enabled() and _is_dev_only_logline(s):
                # Don't print or record to history when developer=0
                return
        except Exception:
            pass
        # If we're in the middle of a stitched line, close it before writing a full line
        self._finalize_line()
        self.lines.append(s)
        print(s, flush=True)

    def clear(self):
        # Equivalent spirit of: Q_memset(con_text,' ', con_buffersize); con_backscroll=0;
        self.lines.clear()
        self.backscroll = 0
        # Reset any open stitched line
        self._pending_line = ""
        self._line_open = False
        try:
            if os.name == "nt":
                os.system("cls")
            else:
                print("\033[2J\033[H", end="", flush=True)
        except Exception:
            print("\n" * 100)

    def dump(self, path: str = "condump.txt") -> bool:
        """
        Dump the console buffer to a text file (Quake's Con_Dump_f flavor):
          - skips initial blank/whitespace-only lines
          - trims trailing spaces per line
          - applies dequake_text mapping
        Usage: condump [filename]
        """
        try:
            path = os.path.expanduser(path or "condump.txt")
            # Ensure directory exists (similar spirit to COM_CreatePath)
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            # Find first non-empty line
            start = 0
            for i, line in enumerate(self.lines):
                if line and line.strip():
                    start = i
                    break
            with open(path, "w", encoding="utf-8") as f:
                for s in self.lines[start:]:
                    # Strip any ANSI color we added for the terminal first, then dequake.
                    plain = strip_ansi((s or "")).rstrip(" ")
                    t = dequake_text(plain)
                    # Write one line per entry; add newline if missing
                    if t.endswith("\n"):
                        f.write(t)
                    else:
                        f.write(t + "\n")
            print(f"Dumped console text to {path}")
            return True
        except Exception as e:
            print(f"ERROR: couldn't open file {path}. ({e})")
            return False
CON = _ConsoleBuffer()

# --- TEMP DEBUG REMOVED: no-op stubs so call sites don't need edits ---
def DBG(_msg: str) -> None:
    return

def DBG_ONCE(_key: str, _msg: str, _limit: int = 3) -> None:
    return
# --- END TEMP DEBUG REMOVED ---

# ---------------- Developer logging toggle (cvar: developer) ----------------
def _ensure_developer_cvar() -> None:
    """
    Register a boolean 'developer' cvar if missing.
      0 = suppress developer/info chatter (default)
      1 = enable developer/info chatter (e.g., save/auto-save notes)
    """
    try:
        if cvar_system.find("developer") is None:
            cvar_system.register(Cvar(
                "developer", "0",
                flags=CvarFlags.ARCHIVE,
                cvar_type=CvarType.BOOL,
                description="Show developer/info messages in console (e.g., config save notes)."
            ))
    except Exception:
        # cvar_system may not be initialized yet; that's ok.
        pass

def _ensure_centerprint_cvar() -> None:
    """Register centerprint logging and its compatibility alias before config loading."""
    try:
        if cvar_system.find("con_logcenterprint") is None:
            cvar_system.register(Cvar(
                "con_logcenterprint", "0",
                flags=CvarFlags.ARCHIVE,
                cvar_type=CvarType.BOOL,
                description="Log centerprints to console (deduped). 0=off, 1=on"
            ))
        cv = cvar_system.find("con_logcenterprint")
        if cv and cvar_system.find("con_logcenterpint") is None:
            cvar_system.register_alias(cv, "con_logcenterpint")
    except Exception:
        # Standalone clients may be created without a global cvar system.
        pass

def _dev_enabled() -> bool:
    try:
        return bool(cvar_system.get_bool("developer", False))
    except Exception:
        return False

def _is_dev_only_logline(s: str) -> bool:
    """
    Returns True for noisy diagnostics we only want in developer mode.
    Extend this with more substrings/patterns as needed.
    """
    # Normalize input and make checks case-insensitive
    if isinstance(s, (bytes, bytearray)):
        try:
            s = s.decode("latin-1", "ignore")
        except Exception:
            s = str(s)
    elif not isinstance(s, str):
        s = str(s)
    low = s.lower()

    # Classic spammy/diagnostic lines
    # Gate *any* variation (punctuation/case) of this string to developer=1
    if "duplicate reliable message received" in low:
        return True
    # Treat config/apply chatter as developer-only
    #   cfg: timelimit -> 10
    #   after map cfg: deathmatch 1, timelimit 10, forceTL 0
    #   config start: ...
    #   config end: ...
    # Allow an optional leading emoji/space (some paths prefix 💬).
    if re.match(r'^\s*(?:💬\s*)?(?:cfg:|after(?:\s+map)?\s+cfg:|config(?:\s+(?:start|end))?:)', s, flags=re.IGNORECASE):
        return True
    # Exec noise while processing configs (only show to devs)
    if IN_CFG_EXEC and re.match(r'^\s*(?:execing|couldn.?t\s+exec)\b', s, flags=re.IGNORECASE):
        return True
    return False

# --- Global stdout filter so bare print() calls are also gated by developer ---
class _DevFilterStdout:
    def __init__(self, real):
        self._real = real
    def write(self, s):
        try:
            if (not _dev_enabled()) and _is_dev_only_logline(s):
                return 0
        except Exception:
            # If anything goes wrong, fail open
            pass
        return self._real.write(s)
    def writelines(self, lines):
        for ln in lines:
            self.write(ln)
    def flush(self):
        return self._real.flush()
    # Pass through everything else (encoding, fileno, isatty, etc.)
    def __getattr__(self, name):
        return getattr(self._real, name)

# Install once
try:
    if not isinstance(sys.stdout, _DevFilterStdout):
        sys.stdout = _DevFilterStdout(sys.stdout)
    # Also filter stderr so prints/logging routed there are gated too
    if not isinstance(sys.stderr, _DevFilterStdout):
        sys.stderr = _DevFilterStdout(sys.stderr)
except Exception:
    pass

logger = logging.getLogger()

# --- Logging filter so library logs obey developer gating too ---
class _DevLoggingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            if (not _dev_enabled()) and _is_dev_only_logline(msg):
                return False
        except Exception:
            # Fail open if anything goes wrong
            return True
        return True

# Attach once to the root logger (covers all handlers by default)
try:
    root = logging.getLogger()
    # Avoid adding duplicates if module is reloaded
    if not any(isinstance(f, _DevLoggingFilter) for f in getattr(root, "filters", [])):
        root.addFilter(_DevLoggingFilter())
except Exception:
    pass

_COLOR_DEBUG = False  # disable all color-debug prints
_UCLR_RAW_DUMPS = 0  # limit raw dumps from UPDATECOLORS to avoid spam
_DEBUG_THROTTLE = {}  # throttle debug prints by type

# --- Config exec state + default.cfg fallback (optional) ---
IN_CFG_EXEC = False         # guard to suppress noisy hints during config processing
DEFAULT_CFG: Optional[str] = None  # set to a string to embed a fallback default.cfg

# --- External IP cache (avoid hammering services) ---
_EXTIP_CACHE: dict[str, Any] = {"ip": None, "at": 0.0}  # {"ip": "1.2.3.4", "at": monotonic}

# --- say_random state ---
_sayrand_loaded: list[bool] = [False]*10
_sayrand_msgs: list[list[str]] = [[] for _ in range(10)]
_sayrand_order: list[list[int]] = [[] for _ in range(10)]
_sayrand_next: list[int] = [0]*10

# All console and script entry points share the active session's dispatcher.
_CONSOLE_SESSION = None
_EDIT_PREFILL = False


def _get_console_session():
    global _CONSOLE_SESSION
    if _CONSOLE_SESSION is None or _CONSOLE_SESSION.cvars is not cvar_system:
        _CONSOLE_SESSION = ConsoleSession(sys.modules[__name__])
    return _CONSOLE_SESSION


def _queue_commands(text, *, insert=False):
    dispatcher = _get_console_session().dispatcher
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(dispatcher.execute(text))
    else:
        context = dispatcher.current if insert else None
        dispatcher.add_text(text, insert=insert, context=context,
                            source=context.source if context else "command")


def Cbuf_AddText(text: str) -> None:
    _queue_commands(text)


def Cbuf_InsertText(text: str) -> None:
    _queue_commands(text, insert=True)


async def _wait_next_frame(n: int = 1) -> None:
    for _ in range(max(1, int(n))):
        active = CURRENT_CLIENT
        if active and active._alive and not active.disconnected:
            try:
                await active.wait_for_update()
            except asyncio.CancelledError:
                if active.disconnected:
                    break
                raise
        else:
            await asyncio.sleep(0)

# Show up on servers + *ver as PyQuake-M
CLIENT_VER = f"pyquake-m/{platform.system().lower()}-{platform.machine().lower()} 0.1"
# Minimal userinfo-safe version tag; mirrors QSS-M doing Info_SetKey(*ver,...)

ALIAS_MAX_DEPTH = 5  # prevent recursive alias loops

# ---------- connect/disconnect helpers ----------
_HOSTPORT_RE = re.compile(r"^\s*(?P<host>[A-Za-z0-9\.\-\_]+|local|localhost)\s*(?::\s*(?P<port>\d{1,5}))?\s*$")


class ClientError(Exception):
    pass


def _make_cmd_body(cmd):
    return b'\x04' + cmd.encode('ascii') + b'\0'


def _encode_angle(angle):
    return int(round((angle * 128 / math.pi))) % 256


def _encode_angle_16(angle):
    return int(round((angle * 32768 / math.pi))) % 65536


# --- Quake text helpers ------------------------------------------------------
# DeQuake mapping (ported from QSS-M Host_InitDeQuake for #iplog / console-safe text)
# C reference:
#   for (i = 1; i < 12; i++) dequake[i] = '#';
#   dequake[9]=9; dequake[10]=10; dequake[13]=13; dequake[12]=' ';
#   dequake[1]=dequake[5]=dequake[14]=dequake[15]=dequake[28]='.';
#   dequake[16]='['; dequake[17]=']';
#   for (i=0;i<10;i++) dequake[18+i]='0'+i;
#   dequake[29]='<'; dequake[30]='-'; dequake[31]='>';
#   for (i=32;i<128;i++) dequake[i]=i;
#   for (i=0;i<128;i++) dequake[i+128]=dequake[i];
#   dequake[128]='(' ; dequake[129]='='; dequake[130]=')'; dequake[131]='*'; dequake[141]='>';
def _build_dequake_table() -> bytes:
    tbl = [ord('#')] * 256  # default '#'

    # 1..11 -> '#'
    for i in range(1, 12):
        tbl[i] = ord('#')
    # Preserve TAB/LF/CR; map FF (12) to space
    tbl[9]  = 9
    tbl[10] = 10
    tbl[13] = 13
    tbl[12] = ord(' ')
    # Dots
    for i in (1, 5, 14, 15, 28):
        tbl[i] = ord('.')
    # Brackets
    tbl[16] = ord('[')
    tbl[17] = ord(']')
    # 0..9 digits at codes 18..27
    for i in range(10):
        tbl[18 + i] = ord('0') + i
    # Arrows-ish
    tbl[29] = ord('<')
    tbl[30] = ord('-')
    tbl[31] = ord('>')
    # Printable ASCII identity
    for i in range(32, 128):
        tbl[i] = i
    # Mirror 0..127 to 128..255
    for i in range(128):
        tbl[i + 128] = tbl[i]
    # Overrides in high range
    tbl[128] = ord('(')
    tbl[129] = ord('=')
    tbl[130] = ord(')')
    tbl[131] = ord('*')
    tbl[141] = ord('>')
    return bytes(tbl)

_DEQUAKE_TABLE = _build_dequake_table()

def dequake_text(s) -> str:
    """
    Convert Quake 8-bit console bytes/latin-1 string to ASCII-safe text,
    mirroring QSS-M's Host_InitDeQuake mapping.
    Accepts bytes or str; returns str (latin-1 decode).
    """
    if s is None:
        return ""
    if isinstance(s, str):
        b = s.encode("latin-1", "replace")
    else:
        b = bytes(s)
    return b.translate(_DEQUAKE_TABLE).decode("latin-1", "ignore")

# ---- ANSI helpers for console coloring ----
# Matches ESC[ ... letter final (typical CSI SGR sequences)
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')

def strip_ansi(s: str) -> str:
    if not isinstance(s, str):
        try:
            s = s.decode("latin-1", "ignore")
        except Exception:
            s = str(s)
    return _ANSI_RE.sub("", s)

ANSI_RED    = "\x1b[31m"
ANSI_YELLOW = "\x1b[33m"
ANSI_RESET  = "\x1b[0m"

def _dequake_with_red(src: "str|bytes") -> str:
    """
    Render Quake text while coloring:
      • high-bit masked bytes (>=128) as red
      • 'gold number' bytes (base 18..27) as yellow
    All bytes are still normalized through the DeQuake table.
    """
    if src is None:
        return ""
    b = src.encode("latin-1", "replace") if isinstance(src, str) else bytes(src)
    out_parts = []
    cur = None  # None | 'red' | 'yellow'
    for ch in b:
        base = ch % 128
        is_gold   = 18 <= base <= 27              # QuakeC strings_ftos_gold
        is_masked = (ch >= 128)
        # Priority: gold (yellow) over masked (red)
        want = 'yellow' if is_gold else ('red' if is_masked else None)
        if want != cur:
            if cur is not None:
                out_parts.append(ANSI_RESET)
            if want == 'red':
                out_parts.append(ANSI_RED)
            elif want == 'yellow':
                out_parts.append(ANSI_YELLOW)
            cur = want
        mapped = _DEQUAKE_TABLE[ch]
        out_parts.append(chr(mapped))
    if cur is not None:
        out_parts.append(ANSI_RESET)
    return "".join(out_parts)

# NetQuake uses '\b' (backspace) to flip color/bright in prints. Some servers
# or proxies may also inject ^1/^2 style color codes. We just strip all that.
# Treat lines as chat only if they look like classic "(TEAM) nick: msg"
# Allow leading spaces and dequaked '.'/'#' junk (some servers colorize with backspaces).
_CHAT_RE = re.compile(r"^\s*[.#]*(?:\((?P<t>[^)]+)\)\s+)?(?P<nick>[^:]+)\s*:\s*(?P<msg>.+)$")

# Also accept team chat printed as ".(nick): msg"
_CHAT_TEAM_ALT_RE = re.compile(r"^\s*[.#]*\((?P<nick>[^)]+)\)\s*:\s*(?P<msg>.+)$")

def _strip_quake_colors(
    s: str,
    *,
    colorize_masked_red: bool = True,
    strip_ansi_codes: bool = False,
) -> str:
    """
    Normalize Quake text for console display.
      - When colorize_masked_red=True, bytes with high-bit set (>=128) are
        shown as red ANSI while still dequaked to readable ASCII.
      - Caret codes (^1,^m, etc) are stripped (add handling later if desired).
      - Placeholder '#' introduced by dequaking control chars are removed.
    """
    if not s:
        return s
    if strip_ansi_codes:
        # Strip existing terminal escapes before dequaking maps ESC to a digit.
        s = strip_ansi(s)
    # Choose rendering path: color masked-red glyphs or plain dequake.
    s = _dequake_with_red(s) if colorize_masked_red else dequake_text(s)
    # Strip Quake-style caret color codes (digits or letters like ^m)
    s = re.sub(r"\^[0-9a-z]", "", s, flags=re.IGNORECASE)
    # Remove placeholder '#' produced by dequaking control bytes (e.g., backspace tricks)
    s = re.sub(r"#+", "", s)
    # Optionally remove any ANSI escape sequences completely.
    if strip_ansi_codes:
        s = strip_ansi(s)
    # Preserve spaces exactly; just strip carriage returns.
    return s.replace("\r", "")

def _generate_random_bundle() -> str:
    """Generate a bundle of random results for f_random replies."""
    import random

    # Coin flip
    coin = "heads" if random.randint(0, 1) == 1 else "tails"

    # Rock/Paper/Scissors
    rps_options = ["rock", "paper", "scissors"]
    rps = random.choice(rps_options)

    # Team color
    color = "red" if random.randint(0, 1) == 1 else "blue"

    # Weighted blackjack hand
    r = random.randint(0, 99)  # 0-99 for weighting

    if r < 8:  # 8% chance for exactly 21
        blackjack_value = 21
    elif r < 23:  # 15% chance for bust (22, 23, or 24)
        bust_weights = [60, 25, 15]  # 60% 22, 25% 23, 15% 24
        bust_r = random.randint(0, 99)
        if bust_r < 60:
            blackjack_value = 22
        elif bust_r < 85:
            blackjack_value = 23
        else:
            blackjack_value = 24
    else:  # 77% chance for 15-20
        blackjack_value = 15 + random.randint(0, 5)  # 15-20

    if blackjack_value > 21:
        blackjack = f"{blackjack_value} (bust)"
    else:
        blackjack = str(blackjack_value)

    # 1-100 number
    number = random.randint(1, 100)

    return f"{coin}, {rps}, {color}, blackjack: {blackjack}, 1-100: {number}"

def _display_server_print(raw: str) -> None:
    # Render text; by default masked-high-bit glyphs become red in the terminal.
    txt = _strip_quake_colors(raw, colorize_masked_red=True)
    if not txt:
        return
    # During an internal ping sweep for 'showscores', swallow prints into a buffer
    # so the console doesn't show the temporary 'ping' output.
    if CURRENT_CLIENT and getattr(CURRENT_CLIENT, "_suppress_prints", False):
        try:
            CURRENT_CLIENT._captured_ping_lines.append(txt)
        except Exception:
            pass
        return
    # svc_print is a text stream: only server newlines terminate its lines.
    # A print may contain several lines, one blank line, or part of a table row.
    parts = txt.split("\n")
    for i, seg in enumerate(parts):
        if seg:
            if not _dev_enabled() and _is_dev_only_logline(seg):
                continue
            prefix = "  "
            if not CON._line_open:
                for match, alternate in ((_CHAT_TEAM_ALT_RE.match(seg), True),
                                         (_CHAT_RE.match(seg), False)):
                    if match is None:
                        continue
                    nick = re.sub(r'^[.#]+', '', match.group("nick"))
                    if not _is_known_player_name(nick):
                        continue
                    message = match.group("msg")
                    if alternate:
                        seg = f"({nick}): {message}"
                    elif match.group("t"):
                        seg = f"({match.group('t')}) {nick}: {message}"
                    else:
                        seg = f"{nick}: {message}"
                    prefix = ""
                    break
            CON.append(seg, prefix=prefix)
        if i < len(parts) - 1:
            CON.newline()

def _strip_slash_command_name(cmd: str) -> str:
    """Servers may send commands prefixed with '//' (FTE compatibility). Normalize."""
    if cmd.startswith("//"):
        return cmd[2:]
    return cmd


def _sanitize_player_name_full(s: str) -> str:
    """
    Normalize a *full* player name as sent by the server (up to 31 chars):
      - strip Quake color/control codes
      - remove ASCII controls
      - trim and clamp to 31 visible chars
    """
    # For names/status we want *plain* text: no ANSI, no masked-red re-coloring.
    s = _strip_quake_colors(s or "", colorize_masked_red=False, strip_ansi_codes=True)
    s = re.sub(r"[\x00-\x1F]", "", s).strip()
    if len(s) > 31:
        s = s[:31]
    return s

def _status_from_names(_short_name: str | None, full_name: str | None) -> str:
    """
    NetQuake full name layout (31 chars):
      0..14: 15-char display name
      15   : single space sentinel
      16..30: status text (0..15 chars)
    Only extract status if the sentinel space at index 15 is present.
    """
    full = (full_name or "")
    if len(full) >= 16 and full[15] == " ":
        return full[16:31].strip()
    return ""

def _maybe_upgrade_name_full(row: dict, short_name: str, full_candidate: str | None) -> None:
    """
    Persist the best-known full (31-char) name for a player without downgrading.
    We only replace if:
      - candidate starts with the 15-char short_name AND
      - candidate is strictly longer than short_name AND
      - candidate is at least as long as any existing name_full.
    """
    if not isinstance(full_candidate, str) or not full_candidate:
        return
    if not full_candidate.startswith(short_name):
        return
    if len(full_candidate) <= len(short_name):
        return
    prev = row.get("name_full", "")
    if len(full_candidate) >= len(prev):
        row["name_full"] = full_candidate[:31]

def _rule_with_title(title: str, width: int, ch: str = "=") -> str:
    """
    Build a rule line of length `width` with ` title ` centered.
    Ensures the rule spans exactly the columns we print below.
    """
    t = f" {title} "
    if width < len(t):
        width = len(t)
    left = (width - len(t)) // 2
    right = width - len(t) - left
    return (ch * left) + t + (ch * right)

def _merge_score_row(rows_by_key: dict[str, tuple[int,int,str,str,str]],
                     fr: int, pg: int, st: str, nm_display: str, nm_key: str) -> None:
    """
    Keep exactly one row per player (by canonical name key):
      1) prefer a row that HAS status over one that doesn't
      2) tiebreak by higher frags
      3) then by lower ping
    Row tuple layout: (frags, ping, status, name_display, name_key)
    """
    cand = (int(fr), int(pg), st or "", nm_display, nm_key)
    old = rows_by_key.get(nm_key)
    if old is None:
        rows_by_key[nm_key] = cand
        return
    old_has = 1 if old[2] else 0
    new_has = 1 if cand[2] else 0
    if (new_has, cand[0], -cand[1]) > (old_has, old[0], -old[1]):
        rows_by_key[nm_key] = cand

# (removed: _extract_fullname_from_line – we trust UPDATENAME for 31-char names)

def _parse_hostport_spec(spec: str, default_port: int | None = None):
    """Parse 'host[:port]' (accepts local/localhost); return (host, port) or None."""
    m = _HOSTPORT_RE.match(spec or "")
    if not m:
        return None
    host = m.group("host")
    if host in ("local", "localhost"):
        host = "localhost"
    # Default to cvar net_hostport if caller doesn't provide one
    if default_port is None:
        try:
            default_port = int(cvar_system.get_value("net_hostport", 26000))
        except Exception:
            default_port = 26000
    port = int(m.group("port")) if m.group("port") else int(default_port)
    return (host, port) if 1 <= port <= 65535 else None

def _split_queued_commands(s: str) -> list[str]:
    return split_commands(s)

# Built-in client commands that shouldn't be shadowed; used for conflict rename.
_RESERVED_CMDS = frozenset({
    "help","cmdlist","aliaslist","clear","quit","exit","q","scoreboard","extip","disconnect","connect","reconnect","demo","record","stop","kill","status",
    "players","showscores","ping","impulse","name","color","say","say_team","tell","dm","pm","like",
    "condump","echo","exec","ip","extip","port","random","cmd","rcon",
    "reset","resetall","resetcfg","s","st","net_stats","unalias","unaliasall","wait",
    # say_random / say_rand slots (reserve so servers can't shadow)
    "version",
    "say_rand0","say_rand1","say_rand2","say_rand3","say_rand4",
    "say_rand5","say_rand6","say_rand7","say_rand8","say_rand9",
    "say_random0","say_random1","say_random2","say_random3","say_random4",
    "say_random5","say_random6","say_random7","say_random8","say_random9",
    # new local commands (reserve so server can't shadow them)
    "find","apropos","aliasedit",
    "identify","ipdump","ipmerge","ipnames",
    # temp local debug command so servers can't shadow it
    "infotest",
    # cvar-related builtins
    "set","seta","unset","toggle","cvarlist","writeconfig","execconfig"
})

# Only treat these server STUFFCMD impulses as pings we should echo.
# (ClanRing uses 39. Add others if you really need them.)
ALLOWED_STUFFCMD_IMPULSES: set[int] = {39}

# --- ChatInfo flags (match engine) ---
CIF_CHAT = 1 << 0
CIF_AFK  = 1 << 1

# ============================== TAB COMPLETION ==============================
_TABCOMP_CACHE: dict[str, list[str]] = {}   # simple per-buffer cache for current request

def _rl_is_libedit() -> bool:
    try:
        return "libedit" in (_readline.__doc__ or "").lower()
    except Exception:
        return False

def _tabcomp__all_command_names() -> list[str]:
    """
    Union of built-in/reserved command names and anything registered via cvar_system.register_command().
    """
    names = set()
    names.update(SERVER_COMMANDS)
    try:
        names.update(_RESERVED_CMDS)
    except Exception:
        pass
    try:
        # cvar_system keeps commands under canonical (lowercased) keys
        for k in getattr(cvar_system, "_commands", {}).keys():
            names.add(k)
    except Exception:
        pass
    return sorted(names)

def _tabcomp__all_cvar_names() -> list[str]:
    """
    All CVAR names plus their CVAR-alias names (cvar_system.register_alias).
    """
    out = set()
    try:
        for cv in getattr(cvar_system, "_cvars", {}).values():
            out.add(cv.name)
        for a in getattr(cvar_system, "_aliases", {}).keys():
            out.add(a)
    except Exception:
        pass
    return sorted(out, key=str.lower)

def _tabcomp__all_alias_names() -> list[str]:
    try:
        return sorted(cvar_system.command_aliases, key=str.lower)
    except NameError:
        return []

def _startswith_ci(hay: str, needle: str) -> bool:
    return hay.casefold().startswith((needle or "").casefold())

def _tabcomp_candidates(prefix: str, *, mode: str, first_token: str|None) -> list[str]:
    """
    mode:
      - "root": first token completion -> commands ∪ cvars ∪ aliases
      - "cvar": second token completion for set/seta/toggle/reset/unset -> cvars
      - "alias": second token completion for alias/unalias/aliasedit -> aliases
    """
    if mode == "cvar":
        pool = _tabcomp__all_cvar_names()
    elif mode == "alias":
        pool = _tabcomp__all_alias_names()
    else:
        # root
        pool = []
        pool.extend(_tabcomp__all_command_names())
        pool.extend(_tabcomp__all_cvar_names())
        # prefer alias names last so commands/cvars appear first in duplicates
        alias_names = _tabcomp__all_alias_names()
        # De-dup while preserving earlier items' order
        seen = set()
        merged = []
        for name in pool + alias_names:
            k = name.casefold()
            if k not in seen:
                seen.add(k)
                merged.append(name)
        pool = merged
    # Filter by prefix (case-insensitive)
    return [n for n in pool if _startswith_ci(n, prefix)]

def _tabcomp_mode_for_line(tokens: list[str]) -> str:
    """
    Decide which domain to complete based on the first token.
    """
    if not tokens:
        return "root"
    t0 = tokens[0].casefold()
    if t0 in ("set", "seta", "toggle", "reset", "unset"):
        return "cvar"
    if t0 in ("alias", "unalias", "aliasedit"):
        return "alias"
    return "root"

def _clean_prompt_lead(buf: str) -> str:
    # Our UI sometimes prints ']' before the cursor; ignore it for parsing.
    # Only strip a single leading ']' so legit ']' in the command stays.
    if buf.startswith("]"):
        return buf[1:]
    return buf

def _tab_completer(text: str, state: int):
    """
    GNU readline-compatible completer:
      - complete only "words" separated by spaces/semicolons
      - first token: commands ∪ cvars ∪ aliases
      - 2nd token on set/seta/toggle/reset/unset: cvars
      - 2nd token on alias/unalias/aliasedit: alias names
    """
    try:
        buf = _readline.get_line_buffer()
    except Exception:
        buf = ""
    buf = _clean_prompt_lead(buf)

    # Compute the current tokens *up to* the cursor position.
    try:
        beg = _readline.get_begidx()
    except Exception:
        beg = len(buf) - len(text)
    line_up_to_cursor = buf[:beg]
    # Split naively on whitespace (aliases are single bare names, fine)
    tokens = [t for t in re.split(r"\s+", line_up_to_cursor.strip()) if t]
    mode = _tabcomp_mode_for_line(tokens)

    # The prefix being completed is 'text'. Build cache key from context.
    key = f"{mode}|{text}|{buf}|{beg}"
    if state == 0 or key not in _TABCOMP_CACHE:
        # When completing the second argument after e.g. 'set ', drop the first token from tokens.
        prefix = text or ""
        cand = _tabcomp_candidates(prefix, mode=mode, first_token=(tokens[0] if tokens else None))
        # Sort with a mild heuristic: shorter first, then lexicographic (case-insensitive)
        cand.sort(key=lambda s: (len(s), s.lower()))
        _TABCOMP_CACHE.clear()
        _TABCOMP_CACHE[key] = cand
    lst = _TABCOMP_CACHE.get(key, [])
    return lst[state] if 0 <= state < len(lst) else None

def _install_tab_completion() -> None:
    """
    Best-effort install of readline completion:
      - binds Tab for GNU readline
      - for libedit (macOS default), uses 'bind ^I rl_complete'
      - keeps delimiter set minimal so we complete tokens split by spaces/semicolons
    """
    if not _readline:
        return
    try:
        # Make ';' a delimiter so "cmd1; c<TAB>" completes the next token properly.
        _readline.set_completer_delims(" \t\n;")
    except Exception:
        pass
    try:
        if _rl_is_libedit():
            _readline.parse_and_bind("bind ^I rl_complete")
        else:
            _readline.parse_and_bind("tab: complete")
    except Exception:
        # Non-fatal
        pass
    try:
        _readline.set_completer(_tab_completer)
    except Exception:
        pass

# Install tab completion early (safe to run multiple times)
try:
    _install_tab_completion()
except Exception:
    pass
# ============================ END TAB COMPLETION ============================

# --- Console typing & idle tracking (no-OS, flicker-free) ---
_INPUT_ACTIVE = Event()           # True while input() is waiting
_LAST_ACTIVITY = time.monotonic() # last user interaction (typing/command)

def _mark_activity():
    global _LAST_ACTIVITY
    _LAST_ACTIVITY = time.monotonic()

# -------------------- Userinfo CVAR helpers: name / color --------------------
# We maintain a pointer to the current AsyncClient so CVAR callbacks
# can forward updates to the server without plumbing it everywhere.
CURRENT_CLIENT = None  # type: Optional["AsyncClient"]

# ======================= Connection timeouts (CVARs) =======================
def _ensure_timeout_cvars() -> None:
    """
    Register connection timeout CVARs if missing.
      net_connecttimeout   : seconds to wait for initial connect/handshake (default 10)
      net_messagetimeout   : seconds without any server message before timing out (default 300)
    """
    try:
        if cvar_system.find("net_connecttimeout") is None:
            cvar_system.register(Cvar(
                "net_connecttimeout", "10",
                flags=CvarFlags.ARCHIVE,
                cvar_type=CvarType.FLOAT,
                minimum=1.0, maximum=120.0,
                description="Seconds to wait for initial connect/handshake before failing."
            ))
        if cvar_system.find("net_messagetimeout") is None:
            cvar_system.register(Cvar(
                "net_messagetimeout", "300",
                flags=CvarFlags.ARCHIVE,
                cvar_type=CvarType.FLOAT,
                minimum=10.0, maximum=1800.0,
                description="Seconds of silence from the server before closing the connection."
            ))
    except Exception:
        pass

# Ensure timeout CVARs exist at import time too
try:
    _ensure_timeout_cvars()
except Exception:
    pass

# ================= Immediate UDP error surfacing helpers ==================
def _enable_windows_udp_connreset(sock) -> None:
    """
    On Windows, make UDP report ICMP 'port unreachable' as WSAECONNRESET (10054).
    That gives us the 'instant' disconnect behavior like classic Quake.
    (If you set this to 0 you *suppress* the error; we explicitly enable it.)
    """
    try:
        if os.name != "nt":
            return
        # SIO_UDP_CONNRESET = _WSAIOW(IOC_VENDOR,12) = 0x9800000C
        SIO_UDP_CONNRESET = 0x9800000C
        # 1 → enable error reporting; 0 would suppress it
        sock.ioctl(SIO_UDP_CONNRESET, struct.pack("I", 1))
    except Exception:
        # If this fails, watchdog still covers us; don't crash
        pass

def _now_monotonic() -> float:
    try:
        return time.monotonic()
    except Exception:
        return time.time()

# -------------------- Config auto-save (no CVARs) --------------------
def _autosave_config(silent: bool = False):
    """
    Unconditionally write the config using the CVAR system's default path.
    Safe to call even if the CVAR system isn't present.

    Args:
        silent: If True, don't print success/failure messages
    """
    # Ensure the toggle exists before checking it
    _ensure_developer_cvar()
    try:
        # Prefer the system's configured default; fall back to 'config.cfg'
        path = getattr(cvar_system, "_config_file", "config.cfg")
        ok = cvar_system.write_variables(path)  # same as write_variables() with default

        # Respect both the explicit 'silent' arg and the 'developer' toggle.
        quiet = bool(silent) or (not _dev_enabled())

        if not quiet:
            if ok:
                print(f"💾 Config saved to {path}")
            else:
                print(f"⚠️  Failed writing {path}")
    except NameError:
        # cvar_system not available
        pass
    except Exception as e:
        # Only emit the error if developer chatter is enabled and not explicitly silenced
        if not silent and _dev_enabled():
            print(f"⚠️  Autosave error: {e}")

def _set_current_client(c: "AsyncClient|None", *, reset_idle: bool = True) -> None:
    global CURRENT_CLIENT
    CURRENT_CLIENT = c
    try:
        _ensure_cl_idle_cvar()
        _ensure_timeout_cvars()
        if reset_idle:
            _IDLE_RECONNECT.on_client_changed()
        _ensure_idle_reconnect_task()
    except Exception:
        pass

def _sanitize_player_name(s: str) -> str:
    """
    Normalize a player name for NetQuake servers:
      - strip Quake color/control codes
      - remove ASCII controls
      - trim and clamp to 15 visible chars (classic limit)
      - provide a fallback if empty
    """
    return _sanitize_player_name_full(s)[:15] or "pyquake-m"

def _name_key(s: str) -> str:
    """Canonical key for merging/lookup: sanitized 15-char name, case-insensitive."""
    return _sanitize_player_name(s).casefold()

def _is_known_player_name(nick: str) -> bool:
    """Check if a nickname is a known player (or the local player)."""
    if not nick:
        return False

    # Check against local player name
    cvars = globals().get("cvar_system")
    me_name = cvars.get_string("name", "") if cvars is not None else ""
    if me_name and _name_key(nick) == _name_key(me_name):
        return True

    # Check against server-known players (if we have a client)
    if CURRENT_CLIENT:
        nick_key = _name_key(nick)
        for row in getattr(CURRENT_CLIENT, "_score_state", {}).values():
            if row.get("active") and _name_key(row.get("name", "")) == nick_key:
                return True
    if CURRENT_CLIENT and hasattr(CURRENT_CLIENT, '_players'):
        nick_key = _name_key(nick)
        for player in CURRENT_CLIENT._players.values():
            player_name = getattr(player, 'name', '')
            if _name_key(player_name) == nick_key:
                return True

    return False

def _clamp_color(n: int) -> int:
    # Classic engines typically accept 0..13; some allow up to 15.
    # We clamp to 0..13 to be conservative.
    return max(0, min(13, int(n)))

def _debug_print_color_event(src: str,
                             idx: int | None,
                             name: str | None,
                             bottom: int | None,
                             packed: int | None,
                             baseline_cm: int | None,
                             resolved: int | None,
                             emoji: str | None = None) -> None:
    """Emit a concise single-line debug record (guarded by _COLOR_DEBUG)."""
    if not _COLOR_DEBUG:
        return
    # Don't log from showscores rows
    if isinstance(src, str) and src.startswith("showscores"):
        return

    global _DEBUG_THROTTLE
    import time

    # Throttle frequent debug types
    now = time.monotonic()
    throttle_key = f"{src}_{idx}"

    # Different throttle rates for different event types
    if "baseline" in src:
        throttle_secs = 5.0  # baseline events every 5 seconds
    elif "showscores" in src:
        throttle_secs = 2.0  # showscores every 2 seconds
    else:
        throttle_secs = 1.0  # other events every 1 second

    last_time = _DEBUG_THROTTLE.get(throttle_key, 0)
    if now - last_time < throttle_secs:
        return

    _DEBUG_THROTTLE[throttle_key] = now

    # Periodic cleanup of old entries (every 100 calls)
    if len(_DEBUG_THROTTLE) > 50:  # If we have more than 50 entries
        cutoff = now - 60  # Remove entries older than 1 minute
        _DEBUG_THROTTLE = {k: v for k, v in _DEBUG_THROTTLE.items() if v > cutoff}

    try:
        # Show just initials/name abbreviation, source type, and color
        name_initials = ""
        if name:
            name_parts = name.strip().split()
            if len(name_parts) >= 2:
                name_initials = f"{name_parts[0][0]}{name_parts[1][0]}".upper()
            elif len(name_parts) == 1 and len(name_parts[0]) > 0:
                name_initials = name_parts[0][:3].upper()
            else:
                name_initials = "???"

        src_abbrev = src.replace("updatecolors", "uclr").replace("update-colormap", "ucm").replace("baseline", "base")
        CON.write(f"[c] {src_abbrev} p{idx or '?'}:{name_initials} c{resolved or '?'} {emoji or ''}")
    except Exception:
        pass

def _parse_info_string(info: str) -> dict[str, str]:
    """Parse Quake \\key\\value userinfo into a dict (tolerant)."""
    out: dict[str, str] = {}
    if not isinstance(info, str) or not info:
        return out
    i, n = 0, len(info)
    while i < n and info[i] == '\\':
        i += 1
        k0 = i
        while i < n and info[i] != '\\': i += 1
        if i >= n: break
        key = info[k0:i]
        i += 1  # skip '\'
        v0 = i
        while i < n and info[i] != '\\': i += 1
        val = info[v0:i]
        out[key] = val
        # next loop continues if another '\' follows
    return out

def _info_get_int(info: str | None, key: str) -> int | None:
    """Return int value of \\key\\ from a userinfo string, masked to 0..15."""
    if not info:
        return None
    d = _parse_info_string(info)
    try:
        return int(d.get(key, "")) & 15
    except Exception:
        return None

def _safe_int(v):
    try:
        return int(v)
    except Exception:
        return None

def _extract_basic(v):
    """If v is a plcolour-like object, prefer v.basic; else int(v) if possible."""
    if v is None:
        return None
    try:
        b = getattr(v, "basic", None)
        if b is not None:
            return _safe_int(b)
    except Exception:
        pass
    return _safe_int(v)

def _extract_bottom_from_updatecolors(parsed) -> int | None:
    """
    Be liberal in what we accept:
      - explicit fields: bottomcolor/bottom/bottom_colour/bottomcolour
      - engine fields: pants (plcolour_t or int)
      - packed fields: color/colors/colours (low nibble = bottom)
    Returns 0..15 or None.
    """
    # singular packed 'color' (seen on some FTE/666 builds)
    vi = _safe_int(getattr(parsed, "color", None))
    if vi is not None:
        return vi & 15
    # direct-bottom style
    for key in ("bottomcolor", "bottom", "bottom_colour", "bottomcolour"):
        v = getattr(parsed, key, None)
        b = _extract_basic(v)
        if b is not None:
            return b & 15
    # pants style
    v = getattr(parsed, "pants", None)
    b = _extract_basic(v)
    if b is not None:
        return b & 15
    # packed plural style
    for key in ("colors", "colours"):
        v = getattr(parsed, key, None)
        vi = _safe_int(v)
        if vi is not None:
            return vi & 15
    return None

def _pants_color_emoji(n: int | None) -> str:
    """
    Emoji palette that matches your engine's color names by index:
      0 white, 1 brown, 2 light blue, 3 green, 4 red, 5 orange, 6 gold,
      7 peach, 8 purple, 9 magenta, 10 tan, 11 green alt, 12 yellow, 13 blue
    """
    if n is None:
        return "·"  # visible placeholder when unknown
    try:
        i = _clamp_color(int(n))
    except Exception:
        return "·"
    palette = [
        "⚪",  # 0 white
        "🟫",  # 1 brown
        "🟦",  # 2 light blue
        "🟩",  # 3 green
        "🟥",  # 4 red
        "🟧",  # 5 orange
        "🟨",  # 6 gold
        "🟧",  # 7 peach
        "🟪",  # 8 purple
        "🟪",  # 9 magenta
        "🟫",  # 10 tan
        "🟩",  # 11 green alt
        "🟨",  # 12 yellow
        "🟦",  # 13 blue
    ]
    return palette[i]

# Reentrancy guards so our callbacks can normalize values without
# echo loops, and avoid double-sending to the server.
_in_name_cb = False
_in_color_cb = False

def _on_name_changed(cvar, _old, _new):
    """CVAR callback for 'name' -> sanitize + send to server."""
    global _in_name_cb
    if _in_name_cb:
        return
    try:
        # Normalize value; if it changed, update CVAR once more (quietly-ish)
        new = _sanitize_player_name(cvar.string)
        if new != cvar.string:
            _in_name_cb = True
            # Setting the CVAR again will invoke this callback; the guard avoids loops.
            cvar_system.set_quick(cvar, new)
        # Tell server (fire-and-forget)
        if CURRENT_CLIENT and not CURRENT_CLIENT.disconnected and CURRENT_CLIENT._alive:
            try:
                asyncio.create_task(CURRENT_CLIENT.send_command(f'name "{new}"'))
            except Exception:
                pass
    finally:
        _in_name_cb = False

def _push_color_to_server():
    if CURRENT_CLIENT and not CURRENT_CLIENT.disconnected and CURRENT_CLIENT._alive:
        try:
            top = _clamp_color(int(cvar_system.get_value("topcolor", 0)))
            bot = _clamp_color(int(cvar_system.get_value("bottomcolor", 0)))
            return asyncio.create_task(CURRENT_CLIENT.send_command(f"color {top} {bot}"))
        except Exception:
            return None
    return None

def _on_color_changed(_cvar, _old, _new):
    """CVAR callback for topcolor/bottomcolor -> clamp + send to server."""
    global _in_color_cb
    if _in_color_cb:
        return
    try:
        _in_color_cb = True
        # Clamp and rewrite if necessary, then push combined to server once.
        top = _clamp_color(int(cvar_system.get_value("topcolor", 0)))
        bot = _clamp_color(int(cvar_system.get_value("bottomcolor", 0)))
        if str(top) != cvar_system.get_string("topcolor"):
            cv = cvar_system.find("topcolor")
            if cv: cvar_system.set_quick(cv, str(top))
        if str(bot) != cvar_system.get_string("bottomcolor"):
            cv = cvar_system.find("bottomcolor")
            if cv: cvar_system.set_quick(cv, str(bot))
        _push_color_to_server()
    finally:
        _in_color_cb = False


def _make_move_body(pitch, yaw, roll, forward, side, up, buttons, impulse,
                    high_res_inputs=False, cmd_time=None, *, input_sequence=None,
                    protocol_flags=0):
    # Use server time if available, else a monotonic fallback
    if cmd_time is None:
        cmd_time = 0.0
    if protocol_flags & proto.ProtocolFlags.FLOATANGLE:
        fmt = '<Bf' + 'fff' + 'hhhBB'
        encode_func = lambda angle: math.degrees(angle)
    elif not high_res_inputs and not protocol_flags & proto.ProtocolFlags.SHORTANGLE:
        fmt = "<BfBBBhhhBB"
        encode_func = _encode_angle
    else:
        fmt = "<BfHHHhhhBB"
        encode_func = _encode_angle_16

    body = struct.pack(fmt,
                       3, float(cmd_time),
                       encode_func(pitch), encode_func(yaw), encode_func(roll),
                       int(forward), int(side), int(up), int(buttons), int(impulse))
    if input_sequence is not None:
        body = body[:1] + struct.pack('<H', input_sequence & 0xffff) + body[1:]
    return body


def _patch(old_val, new_val):
    return old_val if new_val is None else new_val


def _patch_vec(old_vec, update):
    return tuple(v if u is None else u for v, u in zip(old_vec, update))


# --------- Platform / System helpers (for q_version / q_sysinfo) ----------
def _bitness():
    import struct as _struct
    return _struct.calcsize("P") * 8


def _sysctl_str(name: str):
    try:
        out = subprocess.check_output(["/usr/sbin/sysctl", "-n", name], text=True).strip()
        return out if out else None
    except Exception:
        return None

def _sysctl_int(name: str):
    s = _sysctl_str(name)
    try:
        return int(s) if s is not None else None
    except Exception:
        return None

def _mac_ram_gb():
    # Prefer hw.memsize (bytes) over page math for accuracy on macOS
    try:
        mem_bytes = _sysctl_int("hw.memsize")
        if mem_bytes:
            return int(round(mem_bytes / (1024**3)))
    except Exception:
        pass
    return None

def _linux_mem_gb():
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        psize = os.sysconf("SC_PAGE_SIZE")
        if pages and psize:
            return int(round((pages * psize) / (1024**3)))
    except Exception:
        pass
    return None


def _windows_mem_gb():
    try:
        # GetPhysicallyInstalledSystemMemory returns KB
        val_kb = ctypes.c_ulonglong(0)
        if ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(val_kb)):
            return int(round(val_kb.value / (1024**2)))
    except Exception:
        pass
    return None


def _total_ram_gb():
    sysname = platform.system()
    if sysname == "Windows":
        v = _windows_mem_gb()
        if v is not None:
            return v
    v = _linux_mem_gb()
    if v is not None:
        return v
    if sysname == "Darwin":
        v = _mac_ram_gb()
        if v is not None:
            return v
    # Last resort
    return 0


_MAC_CODENAMES = [
    ("15", "macOS Sequoia (2024)"),
    ("14", "macOS Sonoma (2023)"),
    ("13", "macOS Ventura (2022)"),
    ("12", "macOS Monterey (2021)"),
    ("11", "macOS Big Sur (2020)"),
    ("10.15", "macOS Catalina (2019)"),
    ("10.14", "macOS Mojave (2018)"),
    ("10.13", "macOS High Sierra (2017)"),
    ("10.12", "macOS Sierra (2016)"),
    ("10.11", "OS X El Capitan (2015)"),
    ("10.10", "OS X Yosemite (2014)"),
    ("10.9", "OS X Mavericks (2013)"),
    ("10.8", "OS X Mountain Lion (2012)"),
    ("10.7", "OS X Lion (2011)"),
    ("10.6", "OS X Snow Leopard (2009)"),
    ("10.5", "OS X Leopard (2007)"),
]


def _mac_label():
    ver = platform.mac_ver()[0] or ""
    for prefix, name in _MAC_CODENAMES:
        if ver.startswith(prefix):
            return name
    return f"macOS {ver}" if ver else "macOS"


def _platform_label():
    sysname = platform.system()
    if sysname == "Darwin":
        return _mac_label()
    if sysname == "Windows":
        return f"Windows {platform.release()}"
    if sysname == "Linux":
        try:
            info = platform.freedesktop_os_release()
            name, ver = info.get("NAME"), info.get("VERSION_ID")
            if name and ver:
                return f"{name} {ver}"
            if name:
                return name
        except Exception:
            pass
        return f"Linux {platform.release()}"
    return f"{sysname} {platform.release()}"


def _python_label():
    return f"Python {platform.python_version()}"
# -------------------------------------------------------------------------

# ---------------- MAC RICH SYSINFO (model/OS/GPU/display/audio) -------------
_MAC_MODEL_PPI = {
    # Common Apple Silicon laptops
    "MacBookPro18,3": 255,  # 14" 2021
    "MacBookPro18,4": 255,
    "MacBookPro18,1": 254,  # 16" 2021
    "MacBookPro18,2": 254,
}

def _sp_json(datatype: str) -> dict | None:
    try:
        out = subprocess.check_output(["system_profiler", datatype, "-json"], text=True)
        return json.loads(out)
    except Exception:
        return None

def _sp_hw_first() -> dict:
    d = _sp_json("SPHardwareDataType") or {}
    lst = d.get("SPHardwareDataType") or []
    return lst[0] if lst else {}

def _mac_marketing_model_name() -> str | None:
    """
    Best-effort, no hard-coded table:
      1) ~/Library/Preferences/com.apple.SystemProfiler.plist → 'CPU Names' key
         that STARTS WITH the model code (last 4 of serial), ignoring locale suffix.
      2) /System/Library/PrivateFrameworks/AppleSystemInfo.framework/.../AppleSystemInfo.strings
         (convert to XML via plutil) → lookup by model code.
      3) system_profiler 'model_name' (generic), then hw.model.
    """
    try:
        hw = _sp_hw_first()
        serial = (hw.get("serial_number") or "").strip()
        code = (serial[-4:].upper() if len(serial) >= 4 else "")
        plist_path = pathlib.Path.home() / "Library/Preferences/com.apple.SystemProfiler.plist"
        if code and plist_path.exists():
            with plist_path.open("rb") as f:
                pl = plistlib.load(f)
            cpu_names = pl.get("CPU Names") or {}
            # Match any key that begins with the model code, regardless of locale suffix.
            for k, v in cpu_names.items():
                if isinstance(k, str) and k.upper().startswith(code) and isinstance(v, str) and "Mac" in v:
                    return v.strip()
    except Exception:
        pass

    # System fallback: read Apple's mapping from AppleSystemInfo.strings (no GUI needed).
    try:
        if 'code' not in locals():
            hw = _sp_hw_first()
            serial = (hw.get("serial_number") or "").strip()
            code = (serial[-4:].upper() if len(serial) >= 4 else "")
        if code:
            # Try a few language dirs; Apple has used both English.lproj and en.lproj across releases.
            base = pathlib.Path("/System/Library/PrivateFrameworks/AppleSystemInfo.framework/Versions/A/Resources")
            cand = [
                base / "English.lproj" / "AppleSystemInfo.strings",
                base / "en.lproj" / "AppleSystemInfo.strings",
            ]
            # Prefer current locale if a dir exists (e.g., en_GB.lproj)
            loc = (locale.getlocale()[0] or "").split("_")[0] or "en"
            loc_path = base / f"{loc}.lproj" / "AppleSystemInfo.strings"
            if loc_path.exists():
                cand.insert(0, loc_path)
            for p in cand:
                if not p.exists():
                    continue
                # plutil → XML on stdout, then plistlib
                if shutil.which("plutil"):
                    xml = subprocess.check_output(["plutil", "-convert", "xml1", "-o", "-", str(p)])
                    d = plistlib.loads(xml)
                else:
                    # .strings is a plist; many macOS versions can be read directly
                    with p.open("rb") as f:
                        d = plistlib.load(f)
                # Keys are model codes like "J314", values are "MacBook Pro (14-inch, 2021)"
                name = d.get(code)
                if isinstance(name, str) and "Mac" in name:
                    return name.strip()
    except Exception:
        pass

    # Fallback: machine_name from SPHardwareDataType (e.g. "MacBook Pro")
    try:
        hw = _sp_hw_first()
        if hw.get("machine_name"):
            return str(hw["machine_name"]).strip()
    except Exception:
        pass
    # Last resort: hw.model identifier
    return _mac_model_id()

def _mac_cpu_brand() -> str | None:
    s = _sysctl_str("machdep.cpu.brand_string")
    return s or (platform.processor() or None)

def _mac_model_id() -> str | None:
    return _sysctl_str("hw.model")

def _primary_display_info_mac() -> tuple[str|None, tuple[int,int]|None, int|None]:
    """Return (gpu_name, (w,h), refresh_hz) for main display via system_profiler."""
    d = _sp_json("SPDisplaysDataType")
    if not d:
        return (None, None, None)
    gpu_name = None
    res = None
    hz = None
    for gpu in d.get("SPDisplaysDataType", []):
        if not gpu_name:
            gpu_name = gpu.get("_name")
        for disp in gpu.get("spdisplays_displays", []) or []:
            is_main = disp.get("spdisplays_main") == "spdisplays_yes"
            m = re.search(r"(\d+)\s*x\s*(\d+)", disp.get("spdisplays_pixelresolution") or disp.get("spdisplays_resolution") or "")
            if m:
                res = (int(m.group(1)), int(m.group(2)))
            hz_raw = disp.get("spdisplays_refreshrate") or disp.get("spdisplays_refresh_rate")
            try:
                hz = int(float(str(hz_raw).split()[0])) if hz_raw else hz
            except Exception:
                pass
            if is_main and res:
                return (gpu_name, res, hz)
    return (gpu_name, res, hz)

def _default_audio_output_mac() -> str | None:
    d = _sp_json("SPAudioDataType")
    if not d:
        return None
    for dev in d.get("SPAudioDataType", []):
        for item in dev.get("_items", []) or []:
            if item.get("coreaudio_default_output_device"):
                return item.get("_name")
    for dev in d.get("SPAudioDataType", []):
        for item in dev.get("_items", []) or []:
            if item.get("coreaudio_device_output"):
                return item.get("_name")
    return None

def _compose_multi_sysinfo_lines_mac() -> list[str]:
    model_id = _mac_model_id()
    model_pretty = _mac_marketing_model_name() or _mac_model_id()
    cpu = _mac_cpu_brand() or "CPU"
    oslabel = _platform_label()
    cores = os.cpu_count() or 0
    ram_gb = _total_ram_gb() or 0
    # Removed audio/video collection to reduce sysinfo verbosity
    ppi = _MAC_MODEL_PPI.get(model_id or "", None)

    lines: list[str] = []
    lines.append(model_pretty)                          # e.g., MacBook Pro (14-inch, 2021) or fallback
    lines.append(cpu)                                   # Apple M1 Pro ...
    lines.append(f"{oslabel}, {cores} l-cores, {ram_gb}gb ram")
    # Removed video/audio display to reduce sysinfo verbosity
    lines.append(_python_label())
    # ASCII-safe, trimmed a bit per line
    safe = [re.sub(r"[^\x20-\x7E]", "", s).strip()[:200] for s in lines if s and s.strip()]
    return safe

def _compose_sysinfo_line():
    """
    Build a single compact line for q_sysinfo/f_system to avoid chat flood filters.
    """
    sysname = platform.system()
    py = _python_label()
    cores = os.cpu_count() or 0

    if sysname == "Darwin":
        # macOS: model + CPU brand + RAM + OS codename
        model = _sysctl_str("hw.model") or "Mac"
        cpu = (_sysctl_str("machdep.cpu.brand_string")
               or platform.processor() or "CPU")
        ram = _mac_ram_gb()
        oslabel = _mac_label()
        parts = [model, cpu]
        if ram:
            parts.append(f"{ram}GB RAM")
        parts.append(oslabel)
        if cores:
            parts.append(f"{cores} cores")
        parts.append(py)
        return " • ".join(parts)

    # Other OS: keep it succinct but informative
    oslabel = _platform_label()
    ram = _total_ram_gb()
    base = f"{oslabel}, {cores} l-cores, {ram}GB RAM"
    return f"{base} • {py}"

def _print_cmdlist():
    """Print unified command list for the interactive console."""
    print("\nCommands:")
    print("  (input)             - Commands by default; use say/s for chat")
    print("  cl_chatmode 0..3     - 0=commands, 1=auto chat, 2=chat-first, 3=space chats")
    print("  /command            - Force a command in any chat mode")
    print("  help                - Show this command list")
    print("  version             - Show client version tag")
    print("  echo <text>         - Print text locally")
    print("  ip [ext|local]      - Show external or local IP")
    print("  port [n]            - Show/set default server port (for connect)")
    print("  random <a> <b> ...  - Pick a random option; says result if connected")
    print("  net_stats [*]        - Show basic network stats (client-side)")
    print("  wait [n]             - Defer the rest of the buffer until next frame (optionally N frames)")
    print("  say_rand0..9 [team] - Say a randomized line from msgrand#.txt")
    print("  unalias <name>      - Delete an alias")
    print("  unaliasall          - Delete all aliases")
    print("  s  <msg>            - Shortcut for say")
    print("  st <msg>            - Shortcut for say_team")
    print("  reset <cvar>        - Reset a cvar to its default")
    print("  resetall            - Reset all cvars to defaults")
    print("  resetcfg            - Reset archived cvars to defaults")
    print("  exec <file>         - Execute a script file (like Quake)")
    print("  find <substr>       - Search commands, cvars, and aliases (apropos)")
    print("  cmd <args>          - Forward command to server (special-cases: protocols, pext)")
    print("  cmdlist             - Show this command list")
    print("  aliaslist           - Show current aliases")
    print("  alias NAME \"BODY\"   - Define a persistent command alias")
    print("  cfg_save_aliases 0/1 - Save aliases with archived settings (default 1)")
    print("  set/seta VAR VALUE  - Set a variable; seta also saves it")
    print("  writeconfig [file]  - Save settings and aliases")
    print("  execconfig [file]   - Execute config commands (default config.cfg)")
    print("  connect H[:P]|last  - Connect to host[:port] (default port 26000) or last")
    print("  disconnect          - Disconnect but keep client running")
    print("  reconnect           - Disconnect, then reconnect to the last server")
    print("  cl_idle 0..60       - Auto-reconnect interval in minutes; 0 disables")
    print("  like                - Say \"likes <last chat>\" (mirrors team/public)")
    print("  tell WHO MSG        - Private msg; WHO may be name, wildcard, or #N")
    print("  clear               - Clear the client console")
    print("  condump [file]      - Dump console text to file (default condump.txt)")
    print("  quit                - Disconnect from server")
    print("  demo/record [file]  - Start a demo (default pyquake.dem)")
    print("  stop                - Stop and save the demo")
    print("  kill                - Suicide")
    print("  status              - Show server status")
    print("  identify [WHO|IP]   - Show locally known player aliases (slots start at 1)")
    print("  ipdump [file]       - Export identity history (default iplog.txt)")
    print("  ipmerge <file>      - Import a ProQuake/QSS-M iplog.dat file")
    print("  ipnames             - List unique names in local identity history")
    print("  players             - Show player list")
    print("  showscores          - Show live scoreboard with pings/frags")
    print("  ping                - Send ping to server")
    print("  impulse <num>       - Use item/powerup")
    print("  name <newname>      - Change player name")
    print("  color <top> <bot>   - Set player colors")
    print("  say <message>       - Send public chat message")
    print("  say_team <msg>      - Send team chat message")
    print("  rcon <args>         - Send remote console command (uses rcon_password CVAR)")
    print("")
    print("  infotest            - TEMP: dump a few info keys from server/my/user caches")

# ====================== cl_idle (auto reconnect) =======================
def _on_cl_idle_changed(cvar: Cvar, old: str, new: str) -> None:
    _IDLE_RECONNECT.reset_timer()
    _ensure_idle_reconnect_task()


def _ensure_cl_idle_cvar() -> None:
    """
    Register cl_idle if missing.
      0 = disabled (default)
      N = minutes between reconnect attempts while disconnected (clamped 1–60)
    """
    try:
        cv = cvar_system.find("cl_idle")
        if cv is None:
            cvar_system.register(Cvar(
                "cl_idle", "0",
                flags=CvarFlags.ARCHIVE,
                callback=_on_cl_idle_changed,
                cvar_type=CvarType.INT,
                minimum=0, maximum=60,
                description="Reconnect immediately when disconnected, then every N minutes (1–60); 0 disables."
            ))
    except Exception:
        pass

class _IdleReconnectManager:
    """
    Watches cl_idle and connection state; when disconnected and cl_idle>0,
    attempts an immediate 'reconnect' and then retries every N minutes
    (N clamped to 1..60). Queued retries recheck state before connecting.
    """
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._last_attempt: float = 0.0
        self._saw_dc: bool = False
        self._pending: bool = False

    def ensure_task(self):
        # start the loop once per process on the active asyncio loop
        if self._task and not self._task.done():
            return
        loop = asyncio.get_running_loop()
        self._pending = False
        self._task = loop.create_task(self._loop(), name="idle-reconnect")

    def on_client_changed(self):
        # reset state so a fresh immediate attempt will happen after a dc
        self._last_attempt = 0.0
        self._saw_dc = False

    def reset_timer(self):
        # called when cl_idle changes to re-arm promptly
        self._last_attempt = 0.0

    async def reconnect(self, session):
        self._pending = False
        minutes = int(cvar_system.get_value("cl_idle", 0))
        active = CURRENT_CLIENT
        if (minutes <= 0 or session.connecting or not session.last_connect_spec
                or (active is not None and not active.disconnected)):
            return
        clamped = max(1, min(60, minutes))
        unit = "minute" if clamped == 1 else "minutes"
        CON.write(f"Attempting idle reconnect; will retry every {clamped} {unit}.")
        self._last_attempt = time.monotonic()
        await session.connect("last")

    async def _loop(self):
        while True:
            if _CONSOLE_SESSION is not None and _CONSOLE_SESSION.connecting:
                await asyncio.sleep(1.0)
                continue
            try:
                _ensure_cl_idle_cvar()
                minutes = int(cvar_system.get_value("cl_idle", 0))
            except Exception:
                minutes = 0

            if minutes <= 0:
                # disabled; idle a bit
                self._saw_dc = False
                self._last_attempt = 0.0
                await asyncio.sleep(1.0)
                continue

            # clamp 1..60 to avoid spam
            clamped = max(1, min(60, int(minutes)))
            interval = clamped * 60.0
            now = time.monotonic()

            dc = (CURRENT_CLIENT is None) or bool(getattr(CURRENT_CLIENT, "disconnected", True))
            if dc:
                # first notice of being disconnected → attempt immediately
                if not self._saw_dc:
                    self._saw_dc = True
                    self._last_attempt = 0.0
                session = _CONSOLE_SESSION
                if (not self._pending and session is not None and session.last_connect_spec
                        and (self._last_attempt == 0.0 or now - self._last_attempt >= interval)):
                    self._pending = True
                    session.dispatcher.add_text("reconnect", source="idle")
            else:
                # connected → clear flags so next dc gets an immediate attempt
                self._saw_dc = False
                self._last_attempt = 0.0

            await asyncio.sleep(1.0)

_IDLE_RECONNECT = _IdleReconnectManager()

def _ensure_idle_reconnect_task():
    try:
        _IDLE_RECONNECT.ensure_task()
    except RuntimeError:
        # no running loop yet; it will be started the next time a loop exists
        pass

async def _handle_local_command(line: str) -> bool:
    """Return True if we handled it locally (and nothing should be sent to server)."""
    raw = (line or "").strip()
    if not raw:
        DBG("input: <empty>")
        return True
    DBG(f"input: {raw!r}")

    low = raw.lower()
    # --- info extensions from server (and for manual testing) ---
    # Accept both forms: fullserverinfo/svi/fui/ui and //fullserverinfo//svi//fui//ui
    if re.match(r'^\s*(//)?(fullserverinfo|svi|fui|ui)\b', raw, flags=re.IGNORECASE):
        if CURRENT_CLIENT and CURRENT_CLIENT._handle_info_stufftext(raw):
            return True

    # --- net_stats [*] : print client-side network counters ---
    m_nstats = re.match(r'(?i)^\s*net_stats(?:\s+(\S+))?\s*$', raw)
    if m_nstats:
        # In this client, we only have one active connection; '*' is accepted for parity.
        stats = None
        if CURRENT_CLIENT:
            stats = getattr(CURRENT_CLIENT, "_net_stats", None)
        if not stats:
            # Print zeros if no active connection
            stats = {
                "unreliable_messages_sent": 0,
                "unreliable_messages_received": 0,
                "reliable_messages_sent": 0,
                "reliable_messages_received": 0,
                "packets_sent": 0,
                "packets_resent": 0,
                "packets_received": 0,
                "received_duplicate_count": 0,
                "short_packet_count": 0,
                "dropped_datagrams": 0,
                "bytes_sent": 0,
                "bytes_received": 0,
            }
        # Match your C output labels/ordering
        CON.write(f"unreliable messages sent   = {stats['unreliable_messages_sent']}")
        CON.write(f"unreliable messages recv   = {stats['unreliable_messages_received']}")
        CON.write(f"reliable messages sent     = {stats['reliable_messages_sent']}")
        CON.write(f"reliable messages received = {stats['reliable_messages_received']}")
        CON.write(f"packetsSent                = {stats['packets_sent']}")
        CON.write(f"packetsReSent              = {stats['packets_resent']}")
        CON.write(f"packetsReceived            = {stats['packets_received']}")
        CON.write(f"receivedDuplicateCount     = {stats['received_duplicate_count']}")
        CON.write(f"shortPacketCount           = {stats['short_packet_count']}")
        CON.write(f"droppedDatagrams           = {stats['dropped_datagrams']}")
        # Extra: bytes (useful for debugging)
        CON.write(f"bytesSent                  = {stats['bytes_sent']}")
        CON.write(f"bytesReceived              = {stats['bytes_received']}")
        return True

    # --- say_randX / say_randomX [team|-t] ---
    m_srand = re.match(r'(?i)^\s*say_rand(?:om)?([0-9])(?:\s+(.*))?\s*$', raw)
    if m_srand:
        idx = int(m_srand.group(1))
        rest = (m_srand.group(2) or "").strip()
        team = rest.lower() in ("team", "-t")
        _sayrand_ensure_loaded(idx)
        if not _sayrand_msgs[idx]:
            CON.write(f"no messages found for msgrand{idx}.txt")
            return True
        # start a new shuffled cycle if needed
        if not _sayrand_order[idx] or _sayrand_next[idx] >= len(_sayrand_order[idx]):
            _sayrand_order[idx] = list(range(len(_sayrand_msgs[idx])))
            random.shuffle(_sayrand_order[idx])
            _sayrand_next[idx] = 0
        pick = _sayrand_msgs[idx][_sayrand_order[idx][_sayrand_next[idx]]]
        _sayrand_next[idx] += 1
        # send
        if CURRENT_CLIENT and CURRENT_CLIENT._alive and not CURRENT_CLIENT.disconnected:
            cmd = "say_team" if team else "say"
            await CURRENT_CLIENT.send_command(f"{cmd} {pick}")
        else:
            prefix = "👪" if team else "💬"
            CON.write(f"{prefix} {pick}")
        return True

    # --- say/say_team usage (no args) ---
    # 'say' / 'say_team' with no args -> show usage like s/st
    m_say = re.match(r'(?i)^\s*say(?:\s+(.*))?$', raw)
    if m_say:
        msg = (m_say.group(1) or "").strip()
        if not msg:
            CON.write("usage:\n  say <message>\n")
            return True
        # have an arg → let normal path forward to server
        return False

    m_sayteam = re.match(r'(?i)^\s*say_team(?:\s+(.*))?$', raw)
    if m_sayteam:
        msg = (m_sayteam.group(1) or "").strip()
        if not msg:
            CON.write("usage:\n  say_team <message>\n")
            return True
        # have an arg → let normal path forward to server
        return False

    # --- chat shortcuts ---
    # s <msg>  → say <msg>
    m_s = re.match(r'(?i)^\s*s(?:\s+(.*))?$', raw)
    if m_s:
        msg = (m_s.group(1) or "").strip()
        if not msg:
            CON.write("usage:\n  s <message>   — alias for 'say'\n")
            return True
        if CURRENT_CLIENT and CURRENT_CLIENT._alive and not CURRENT_CLIENT.disconnected:
            await CURRENT_CLIENT.send_command(f"say {msg}")
        else:
            CON.write(f"💬 You: {msg}")
        return True

    # st <msg> → say_team <msg>
    m_st = re.match(r'(?i)^\s*st(?:\s+(.*))?$', raw)
    if m_st:
        msg = (m_st.group(1) or "").strip()
        if not msg:
            CON.write("usage:\n  st <message>  — alias for 'say_team'\n")
            return True
        if CURRENT_CLIENT and CURRENT_CLIENT._alive and not CURRENT_CLIENT.disconnected:
            await CURRENT_CLIENT.send_command(f"say_team {msg}")
        else:
            CON.write(f"👪 TEAM • You: {msg}")
        return True

    # --- reset <cvar>: reset to default (johnfitz) ---
    m_reset = re.match(r'(?i)^\s*reset(?:\s+(\S+))?\s*$', raw)
    if m_reset:
        name = (m_reset.group(1) or "").strip()
        if not name:
            CON.write("reset <cvar> : reset cvar to default")
            return True
        cv = cvar_system.find(name)
        if not cv:
            CON.write(f"variable '{name}' not found")
            return True
        cvar_system.reset(name)  # respects ROM/LOCKED and triggers callbacks
        return True

    # --- resetall: reset all cvars (johnfitz) ---
    if re.match(r'(?i)^\s*resetall\s*$', raw):
        try:
            cvar_system.reset_all()
        except Exception:
            # Fallback manual loop if needed
            try:
                for cv in list(getattr(cvar_system, "_cvars", {}).values()):
                    cvar_system.reset(cv.name)
            except Exception:
                pass
        return True

    # --- resetcfg: reset only archived cvars (QuakeSpasm) ---
    if re.match(r'(?i)^\s*resetcfg\s*$', raw):
        try:
            for cv in list(getattr(cvar_system, "_cvars", {}).values()):
                if cv.flags & CvarFlags.ARCHIVE:
                    cvar_system.reset(cv.name)
        except Exception:
            pass
        return True

    # --- random <option1> <option2> ... : pick one uniformly ---
    m_rand = re.match(r'(?i)^\s*random(?:\s+.+)?$', raw)
    if m_rand:
        # Tokenize like the console; quoted args are respected
        try:
            toks = shlex.split(raw)
        except Exception:
            toks = raw.split()
        if len(toks) <= 1:
            CON.write("\nusage: random <option1> <option2> ... <optionN>\n")
            return True
        options = toks[1:]
        picked = random.choice(options)
        args_string = ", ".join(options)
        # If connected, announce via say; else print locally
        if CURRENT_CLIENT and CURRENT_CLIENT._alive and not CURRENT_CLIENT.disconnected:
            try:
                await CURRENT_CLIENT.send_command(f"say randomize: {args_string}")
                await CURRENT_CLIENT.send_command(f"say result: {picked}")
            except Exception:
                # Fallback local print if networking fails
                CON.write(f"result: ^m{picked}^m")
        else:
            CON.write(f"result: ^m{picked}^m")
        return True

    # --- port [n]: show/set default server port used by 'connect host' ---
    m_port = re.match(r'(?i)^\s*port(?:\s+(\d+))?\s*$', raw)
    if m_port:
        _ensure_net_hostport_cvar()
        arg = m_port.group(1)
        if not arg:
            # Print like Quake: "\"port\" is \"<value>\""
            v = int(cvar_system.get_value("net_hostport", 26000))
            CON.write(f"\"port\" is \"{v}\"")
            return True
        try:
            n = int(arg)
        except Exception:
            CON.write("Bad value, must be between 1 and 65534")
            return True
        if n < 1 or n > 65534:
            CON.write("Bad value, must be between 1 and 65534")
            return True
        cv = cvar_system.find("net_hostport")
        if cv:
            cvar_system.set_quick(cv, str(n))
        else:
            # Fallback in case registration failed earlier
            cvar_system.create("net_hostport", str(n), flags=CvarFlags.ARCHIVE, cvar_type=CvarType.INT)
        return True

    # --- ip / extip: show or copy local/external IP ---
    m_ip = re.match(r'(?i)^\s*(ip|extip)(?:\s+(\S+))?\s*$', raw)
    if m_ip:
        sub = (m_ip.group(2) or "").strip().lower()
        local_ip = _primary_local_ip() or ""
        ext_ip = _external_ip() or ""
        if sub in ("ext", "external"):
            CON.write(f"\nexternal: {ext_ip}\n" if ext_ip else "\nexternal: UNKNOWN\n")
        elif sub in ("local", "lan", "int", "internal"):
            CON.write(f"\nlocal: {local_ip}\n" if local_ip else "\nno local IP addresses found\n")
        elif sub in ("",):
            if local_ip:
                CON.write(f"\n\nlocal:    {local_ip}")
            else:
                CON.write("\n\nlocal:    UNKNOWN")
            if ext_ip:
                CON.write(f"\nexternal: {ext_ip}\n")
            else:
                CON.write("\nexternal: UNKNOWN\n")
        else:
            CON.write(
                "\ninvalid argument. usage:\n"
                "  ip                    - display local and external IPs\n"
                "  ip ext                - display external IP only\n"
                "  ip local              - display local IP only\n\n"
            )
        return True

    # --- echo: print the rest of the line locally (no server forward) ---
    m_echo = re.match(r'(?i)^\s*echo(?:\s+(.*))?$', raw)
    if m_echo:
        # Match Quake behavior: tokenize like the console would, then join argv[1..]
        try:
            toks = shlex.split(raw)
        except Exception:
            toks = raw.split()
        msg = " ".join(toks[1:]) if len(toks) > 1 else ""
        CON.write(msg)
        return True

    # --- find/apropos <substring> ---
    m_find = re.match(r'(?i)^\s*(find|apropos)\s*(.*)$', raw)
    if m_find:
        substr = (m_find.group(2) or "").strip()
        if not substr:
            CON.write("usage:\n  find <substring>  — search commands, cvars, and aliases\n")
            return True
        _apropos_search(substr)
        return True

    # --- aliasedit <name> --- place alias command onto the edit line for modification
    m_edit = re.match(r'(?i)^\s*aliasedit\s+(\S+)\s*$', raw)
    if m_edit:
        alias_name = m_edit.group(1)
        # Exact match, then conflict-renamed "<name>_a", then case-insensitive
        body = cvar_system.command_aliases.get(alias_name)
        key_used = alias_name
        if body is None:
            if alias_name + "_a" in cvar_system.command_aliases:
                key_used = alias_name + "_a"
                body = cvar_system.command_aliases[key_used]
            else:
                for k, v in cvar_system.command_aliases.items():
                    if k.lower() == alias_name.lower():
                        key_used, body = k, v
                        break
        if body is None:
            CON.write(f'\nno alias named "{alias_name}" found\n')
            return True
        final_cmd = f"alias {quote_arg(alias_name)} {quote_arg(body.rstrip())}"

        # Try to prefill the *next* input line using readline (POSIX).
        def _prefill_next_input(s: str) -> bool:
            if _readline:
                def _hook():
                    try:
                        _readline.insert_text(s)
                        _readline.redisplay()
                    finally:
                        _readline.set_pre_input_hook(None)
                try:
                    _readline.set_pre_input_hook(_hook)
                    globals()["_EDIT_PREFILL"] = True
                    return True
                except Exception:
                    pass
            # Clipboard fallback (macOS pbcopy / Windows clip)
            try:
                if sys.platform == "darwin" and shutil.which("pbcopy"):
                    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                    p.communicate(final_cmd.encode("utf-8", "ignore"))
                    return False
                if os.name == "nt" and shutil.which("clip"):
                    p = subprocess.Popen(["clip"], stdin=subprocess.PIPE)
                    p.communicate(final_cmd.encode("utf-8", "ignore"))
                    return False
            except Exception:
                pass
            return False

        used_readline = _prefill_next_input(final_cmd)
        if used_readline:
            CON.write("✏️  placed on edit line; press Enter to submit or edit it first.")
        else:
            # Show a console-prompt-style line for manual editing/copying.
            CON.write("]" + final_cmd)
        return True

    if low == "infotest":
        _do_infotest()
        return True
    if low == "cmdlist":
        _print_cmdlist()
        return True

    # --- version: print client version tag ---
    if low == "version":
        CON.write(CLIENT_VER)
        return True

    # rcon <args...>  -> uses rcon_password cvar
    m = re.match(r"(?i)^rcon(?:\s+(.*))?$", raw)
    if m:
        args = (m.group(1) or "").strip()
        if CURRENT_CLIENT and not CURRENT_CLIENT.disconnected:
            await CURRENT_CLIENT.send_rcon_nq(args)
        else:
            CON.write("Not connected.")
        return True

    return False

# ---------- apropos/find ----------
def _apropos_search(substr: str) -> None:
    """Search commands, CVARs, and aliases for case-insensitive substring."""
    try:
        needle = substr.casefold()
        # Commands: reserved + registered with cvar_system
        cmd_names = set(_RESERVED_CMDS)
        try:
            cmd_names.update(getattr(cvar_system, "_commands", {}).keys())
        except Exception:
            pass
        cmds = sorted([c for c in cmd_names if needle in c.casefold()])

        # CVARs (names + aliases)
        cvars = []
        try:
            for c in getattr(cvar_system, "_cvars", {}).values():
                if needle in c.name.casefold():
                    cvars.append(c.name)
            for a in getattr(cvar_system, "_aliases", {}).keys():
                if needle in a.casefold():
                    cvars.append(a + " (alias)")
        except Exception:
            pass
        cvars = sorted(dict.fromkeys(cvars))  # de-dup

        # Aliases (server/local)
        aliases = []
        try:
            aliases = sorted([k for k in cvar_system.command_aliases if needle in k.casefold()])
        except Exception:
            pass

        # Output
        if not (cmds or cvars or aliases):
            CON.write(f'No matches for "{substr}".')
            return
        if cmds:
            CON.write(f"Commands ({len(cmds)}): " + ", ".join(cmds))
        if cvars:
            CON.write(f"CVARs ({len(cvars)}): " + ", ".join(cvars))
        if aliases:
            CON.write(f"Aliases ({len(aliases)}): " + ", ".join(aliases))
    except Exception as e:
        CON.write(f"find error: {e}")

# -------------------- IP helpers --------------------
def _primary_local_ip() -> Optional[str]:
    """
    Best-effort primary LAN IPv4:
      - Use UDP connect trick to a public IP to learn the outbound interface IP
      - Fallback to host lookup if needed; ignore loopback
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        try:
            # No packets are actually sent; connect assigns a local address.
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        finally:
            s.close()
    except Exception:
        pass
    # Fallbacks
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        # Last resort: enumerate getaddrinfo results
        infos = socket.getaddrinfo(None, 0, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        for fam, typ, proto_, canon, sockaddr in infos:
            ip = sockaddr[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return None

def _external_ip(timeout: float = 1.5, cache_sec: float = 600.0) -> Optional[str]:
    """
    Query a few simple HTTPS endpoints for our public IPv4. Cache for a bit.
    """
    now = time.monotonic()
    ip_cached = _EXTIP_CACHE.get("ip")
    ts_cached = float(_EXTIP_CACHE.get("at") or 0.0)
    if ip_cached and (now - ts_cached) < cache_sec:
        return ip_cached
    urls = (
        "https://api.ipify.org",
        "https://ipv4.icanhazip.com",
        "https://ifconfig.me/ip",
    )
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = r.read().decode("ascii", "ignore").strip()
            # Validate it's an IPv4
            try:
                ipaddress.IPv4Address(data)
                _EXTIP_CACHE["ip"] = data
                _EXTIP_CACHE["at"] = now
                return data
            except Exception:
                continue
        except Exception:
            continue
    return None

# Ensure net_hostport cvar exists (archived, int, clamped)
def _ensure_net_hostport_cvar() -> None:
    try:
        cv = cvar_system.find("net_hostport")
        if cv is None:
            cvar_system.register(Cvar(
                "net_hostport", "26000",
                flags=CvarFlags.ARCHIVE,
                cvar_type=CvarType.INT,
                minimum=1, maximum=65534,
                description="Default server port for 'connect host' (when :port omitted)"
            ))
    except Exception:
        pass

# -------------------- say_random helpers --------------------
def _sayrand_base_dir() -> str:
    """
    Use the same directory as config.cfg.
    - If cvar_system._config_file is absolute, use its parent.
    - If it's relative (default "config.cfg"), use the current working directory.
    """
    try:
        cfg = getattr(cvar_system, "_config_file", "config.cfg") or "config.cfg"
        if os.path.isabs(cfg):
            return os.path.dirname(cfg) or os.getcwd()
        return os.getcwd()
    except Exception:
        return os.getcwd()

def _sayrand_path(slot: int) -> str:
    return os.path.join(_sayrand_base_dir(), f"msgrand{slot}.txt")

def _sayrand_ensure_loaded(slot: int) -> None:
    """Lazy-load msgrand{slot}.txt into memory once."""
    if _sayrand_loaded[slot]:
        return
    _sayrand_loaded[slot] = True
    path = _sayrand_path(slot)
    msgs: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip("\r\n")
                if s:
                    msgs.append(s)
    except Exception:
        # not found or unreadable -> keep empty (silent, like engines do)
        msgs = []
    _sayrand_msgs[slot] = msgs
    _sayrand_order[slot] = []
    _sayrand_next[slot] = 0

def _clear_console():
    """Clear the terminal/console screen cross-platform."""
    try:
        # Windows traditional clear
        if os.name == "nt":
            os.system("cls")
            return
        # ANSI-capable terminals (macOS/Linux and modern Windows)
        print("\033[2J\033[H", end="", flush=True)
    except Exception:
        # Fallback: scroll off
        print("\n" * 100)


class Demo:
    def __init__(self):
        self._msgs = []
        self._angles = []
        self.record_pending = False
        self.recording_started = False
        self.recording_complete = False

    def start_recording(self):
        self.record_pending = True

    def stop_recording(self):
        if self.recording_started:
            # QSS-M expects svc_disconnect rather than an abrupt EOF.
            self._msgs.append(b'\x02')
            self._angles.append(self._angles[-1])
        self.record_pending = self.recording_started = False
        self.recording_complete = True

    def add_message(self, angles, msg, has_server_info):
        if self.record_pending:
            if has_server_info:
                self.record_pending = False
                self.recording_started = True
        elif self.recording_complete:
            raise Exception("add_message called when recording is complete")

        if self.recording_started:
            self._msgs.append(msg)
            self._angles.append(tuple(angles))

    def _parse_messages(self):
        protocol = proto.Protocol(proto.ProtocolVersion.NETQUAKE, proto.ProtocolFlags(0))
        for msg in self._msgs:
            parsed_msgs = []
            remaining_msg = msg
            while remaining_msg:
                parsed, remaining_msg = proto.ServerMessage.parse_message(
                        remaining_msg, protocol)
                if parsed.msg_type in (proto.ServerMessageType.SERVERINFO, proto.ServerMessageType.VERSION):
                    protocol = parsed.protocol
                parsed_msgs.append(parsed)
            yield parsed_msgs

    def dump(self, f, *, angle_calculator=None):
        f.write(b'-1\n')   # cd track
        demo_header_fmt = "<Ifff"

        if angle_calculator is not None:
            all_angles = angle_calculator.calculate(self._parse_messages())
        else:
            all_angles = self._angles

        for msg, angles in zip(self._msgs, all_angles):
            f.write(struct.pack(demo_header_fmt,
                                len(msg),
                                *(180. * a / math.pi for a in angles)))
            if isinstance(msg, str):
                f.write(msg.encode('latin-1'))  # Convert string to bytes
            else:
                f.write(msg)  # Already bytes


class _BaseAngleCalculator:
    """Pass into `Demo.dump` to override view angles."""

    def _angle_difference(self, a, b):
        t = (a - b) % math.tau
        return min(t, math.tau - t)

    def _get_movement_yaws(self, parsed_msg_lists):
        view_entity = None
        pos = (0., 0., 0.)
        time = 0.
        move_time = None
        yaw = 0.

        for parsed_msgs in parsed_msg_lists:
            for parsed_msg in parsed_msgs:
                if parsed_msg.msg_type == proto.ServerMessageType.SETVIEW:
                    view_entity = parsed_msg.viewentity

                if (parsed_msg.msg_type in (proto.ServerMessageType.SPAWNBASELINE,
                                            proto.ServerMessageType.SPAWNBASELINE2)
                        and parsed_msg.entity_num == view_entity):
                    pos = parsed_msg.origin

                if parsed_msg.msg_type == proto.ServerMessageType.TIME:
                    time = parsed_msg.time

                if (parsed_msg.msg_type == proto.ServerMessageType.UPDATE and parsed_msg.entity_num == view_entity):
                    new_pos = _patch_vec(pos, parsed_msg.origin)
                    if move_time is not None:
                        dt = time - move_time
                        # Guard zero/negative dt to avoid division errors and noise.
                        if dt and dt > 1e-6:
                            vel = tuple((new - old) / dt for new, old in zip(new_pos, pos))
                            if math.hypot(*vel) > 50:
                                yaw = math.atan2(vel[1], vel[0])
                    move_time = time
                    pos = new_pos

            yield yaw


class AngleCalculatorSmoothed(_BaseAngleCalculator):
    def _angle_cost(self, movement_yaw, yaw):
        diff = self._angle_difference(movement_yaw, yaw)
        if diff < math.pi / 8:
            return 0
        return 0.4

    def calculate(self, parsed_msg_lists):
        paths = [([], 0.)]
        for j, movement_yaw in enumerate(self._get_movement_yaws(parsed_msg_lists)):
            new_paths = []
            for direction in range(8):
                yaw = direction * math.tau / 8
                c = self._angle_cost(movement_yaw, yaw)
                new_dists = [int(bool(prev_path) and prev_path[-1] != yaw) + c + prev_dist
                             for prev_path, prev_dist in paths]
                i = min(range(len(new_dists)), key=new_dists.__getitem__)
                new_paths.append((paths[i][0] + [yaw], new_dists[i]))
            paths = new_paths

        best_path = min(paths, key=lambda p: p[1])[0]
        return ((0., yaw, 0.) for yaw in best_path)


class AngleCalculatorHysteresis(_BaseAngleCalculator):
    def __init__(self, hysteresis_angle):
        self._hysteresis_angle = hysteresis_angle

    def calculate(self, parsed_msg_lists):
        yaw = 0.
        for movement_yaw in self._get_movement_yaws(parsed_msg_lists):
            if self._angle_difference(movement_yaw, yaw) > self._hysteresis_angle:
                yaw = math.pi * round((4 * movement_yaw / math.pi)) / 4
            yield (0., yaw, 0.)


@dataclasses.dataclass(slots=True)
class Entity:
    entity_num: int
    model_num: int
    frame: int
    colormap: int
    skin: int
    origin: tuple[float, float, float]
    angles: tuple[float, float, float]

    @classmethod
    def make_zero_baseline(cls) -> "Entity":
        return cls(
            0, 0, 0, 0, 0, (0., 0., 0.), (0., 0., 0.)
        )

    @classmethod
    def from_baseline(cls, baseline: proto.ServerMessageSpawnBaseline) \
            -> "Entity":
        return cls(
            baseline.entity_num,
            baseline.model_num,
            baseline.frame,
            baseline.colormap,
            baseline.skin,
            baseline.origin,
            baseline.angles,
        )

    def update(self,
               update: proto.ServerMessageUpdate) -> "Entity":
        entity_num = update.entity_num
        model_num = (
            update.model_num
            if update.model_num is not None else self.model_num
        )
        frame = (
            update.frame
            if update.frame is not None else self.frame
        )
        colormap = (
            update.colormap
            if update.colormap is not None else self.colormap
        )
        skin = (
            update.skin
            if update.skin is not None else self.skin
        )
        origin = _patch_vec(self.origin, update.origin)
        angles = _patch_vec(self.angles, update.angle)

        return Entity(entity_num, model_num, frame, colormap, skin, origin,
                      angles)


class AsyncClient:
    def __init__(self, conn):
        self._conn = conn
        self._read_task = None
        self._cvar_listener = None
        self._pending_impulse = 0  # deliver via next clc_move (ClanRing ping uses impulse 39)
        # RX/Timing monitors
        self._mono0 = time.monotonic()
        self.last_rx_at = self._mono0
        self.last_tx_at = self._mono0
        self._alive = True
        self._spawned_fut = asyncio.Future()
        self._center_print_fut = asyncio.Future()
        self.protocol = proto.Protocol(
            proto.ProtocolVersion.NETQUAKE,
            proto.ProtocolFlags(0)
        )  # May be changed when server info is received.
        self.level_name = None
        self.view_entity = None
        self.level_finished = False
        self.time = None
        self._updated_fut = asyncio.Future()
        self.entities = {}
        self.baselines = {}
        self._fte_entities = {}
        self._fte_baselines = {}
        self.stats = {}
        self.stat_strings = {}
        self._move_sequence = 0
        self.input_ack = None
        self.gamedir = ''
        self.sounds = None
        self.angles = (0., 0., 0.)
        self.velocity = (0., 0., 0.)
        self.on_ground = None
        self.view_height = None
        self.models = None
        self.disconnected = False
        # Protocol-ID/mask pairs, populated from negotiated serverinfo headers.
        self._pext_bits: list[int] = [0, 0, 0, 0]
        self._saw_cmd_pext: bool = False

        self._demos = []
        # Keep the current map's sign-on, plus the latest persistent state,
        # so recording can begin after joining without waiting for a new map.
        self._demo_signon = []
        self._demo_signon_complete = False
        self._demo_state = {}
        # Until we see any real inbound server message (serverinfo/signon/time),
        # we aggressively NAT-punch with small UNRELIABLE packets.
        self._signon_seen = False

        # --- Live scoreboard state (no parsing of 'status' needed) ---
        # idx -> {'active', 'name','frags','shirt','pants','ping','pl','t_last'}
        self._score_state: dict[int, dict] = {}
        # name -> latest parsed ping from 'ping' command
        self._latest_ping_by_name: dict[str, int] = {}
        self.iplog = IpLog()  # ConsoleSession supplies the persistent shared store.
        self._status_parser = StatusParser()
        self._status_players = {}
        self._last_connected_slot: int | None = None
        self._status_waiter = None
        self._status_lock = asyncio.Lock()

        # --- Internal capture for silent 'ping' sweep used by showscores ---
        self._suppress_prints: bool = False
        self._captured_ping_lines: list[str] = []

        # rate limit for special chat replies (seconds)
        self._last_version_reply_at = self._mono0 - 100.0
        self._last_sysinfo_reply_at = self._mono0 - 100.0
        self._last_random_reply_at = self._mono0 - 100.0

        # --- chat memory for 'like' ---
        self.last_chat_msg: str = ""        # last seen chat message text (no nick)
        self.last_chat_is_team: bool = False
        self._last_like_at: float = self._mono0 - 10.0  # rate-limit (>=1s)

        self.aliases = cvar_system.command_aliases if "cvar_system" in globals() else {}
        # subscribe cvar updates to push userinfo on change
        self._cvar_subscribed = False

        # --- Centerprint logging control (default: suppressed) ---
        _ensure_centerprint_cvar()
        # Keep last logged centerprint to suppress identical spam bursts.
        self._last_centerprint_logged: str | None = None

        # --- ProQuake team/timer state ---
        self._pq_minutes: int | None = None
        self._pq_seconds: int | None = None
        self._pq_last_time_seen: float | None = None
        self._pq_paused: bool = False
        self._pq_pause_time: float | None = None
        self._pq_uses_server_time: bool = False
        self._pq_team_frags: dict[int, int] = {}      # team(bottomcolor) -> frags
        self._pq_team_colors: dict[int, int] = {}     # team -> packed colors (16*shirt + team)
        self._pq_teamgame_hint: bool = False          # set true when we see any PQ team msg
        # --- Quake-style info caches (QSS-M Info_GetKey vibe) ---
        self._serverinfo_is: 'InfoString' = InfoString()
        self._myinfo_is: 'InfoString' = InfoString()
        self._userinfo_is: dict[int, 'InfoString'] = {}
        self._synthetic_userinfo: dict[int, 'InfoString'] = {}

        # --- Client-side network counters (best-effort) ---
        # Names chosen to mirror Quake's NET_Stats_f output where possible.
        self._net_stats = {
            "unreliable_messages_sent": 0,
            "unreliable_messages_received": 0,  # not observable here
            "reliable_messages_sent": 0,
            "reliable_messages_received": 0,    # approximated as frames read
            "packets_sent": 0,
            "packets_resent": 0,                # not observable here
            "packets_received": 0,
            "received_duplicate_count": 0,      # not observable here
            "short_packet_count": 0,            # not observable here
            "dropped_datagrams": 0,             # not observable here
            "bytes_sent": 0,
            "bytes_received": 0,
        }

    # -------------------- InfoString accessors --------------------
    def serverinfo_get(self, key: str, default: str = "") -> str:
        try:
            return info_get_key(self._serverinfo_is, key, default)
        except Exception:
            return default

    def userinfo_get(self, slot: int, key: str, default: str = "") -> str:
        try:
            islots = self._userinfo_is.get(int(slot))
            return info_get_key(islots or InfoString(), key, default)
        except Exception:
            return default

    def _on_serverinfo_updated(self) -> None:
        """
        Treat receipt of serverinfo as a real inbound server message for any
        code that gatekeeps on '_signon_seen' (NAT punching, spawn wait, etc).
        """
        try:
            self._signon_seen = True  # stop NAT punching / mark progress
        except Exception:
            pass

    def _on_userinfo_changed(self, slot: int) -> None:
        """Reflect server-supplied player colors in the live scoreboard."""
        info = self._userinfo_is.get(slot, {})
        row = self._score_state.setdefault(slot, {})
        for key in ("topcolor", "bottomcolor"):
            value = _safe_int(info.get(key))
            if value is not None:
                row[key] = value & 15

    # -------------------- Stufftext info-extension handlers --------------------
    def _handle_info_stufftext(self, line: str) -> bool:
        """
        Handle FTE/QSS predictable info commands delivered via stufftext:
          - fullserverinfo "<\\key\\value...>"
          - svi "<key>" "<value>"
          - fui <slot> "<\\key\\value...>"
          - ui  <slot> "<key>" "<value>"
        Also accepts the //fullserverinfo, //svi, //fui, //ui variants.
        Returns True if consumed.
        """
        try:
            # IMPORTANT: Use posix=False so backslashes inside quotes are not eaten.
            try:
                toks = shlex.split(line, posix=False)
            except Exception:
                toks = line.split()
            if not toks:
                return False
            cmd = _strip_slash_command_name(toks[0].strip().lower())
            if cmd not in ("fullserverinfo", "svi", "fui", "ui"):
                return False

            # Helpers
            def _qs(i: int) -> str:
                s = toks[i] if i < len(toks) else ""
                s = s.strip()
                if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
                    return s[1:-1]
                return s

            if cmd == "fullserverinfo":
                # Accept both quoted and unquoted payloads: take the remainder of the line if needed.
                payload = ""
                if len(toks) >= 2:
                    payload = _qs(1)
                if not payload:
                    # fallback: everything after the first space
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        payload = parts[1].strip()
                        if (payload[:1], payload[-1:]) in {('"','"'), ("'","'")}:
                            payload = payload[1:-1]
                if len(toks) >= 2:
                    self._serverinfo_is = InfoString.parse(payload)
                    self._on_serverinfo_updated()
                return True

            if cmd == "svi":
                if len(toks) >= 3:
                    key = _qs(1)
                    val = _qs(2)
                    info_set_key(self._serverinfo_is, key, val)
                    self._on_serverinfo_updated()
                return True

            if cmd == "fui":
                if len(toks) >= 3:
                    try:
                        slot = int(toks[1])
                    except Exception:
                        return True  # consume quietly
                    self._userinfo_is[slot] = InfoString.parse(_qs(2))
                    self._on_userinfo_changed(slot)
                return True

            if cmd == "ui":
                if len(toks) >= 4:
                    try:
                        slot = int(toks[1])
                    except Exception:
                        return True
                    islots = InfoString(self._userinfo_is.get(slot) or {})
                    info_set_key(islots, _qs(2), _qs(3))
                    self._userinfo_is[slot] = islots
                    self._on_userinfo_changed(slot)
                return True

        except Exception:
            # IMPORTANT: do NOT consume on error; let other handlers see the line.
            return False
        return False

    async def _send_unreliable(self, payload: bytes):
        """
        Send payload as a proper *netchan-framed* UNRELIABLE message if the transport
        supports it; otherwise fall back to RELIABLE so the server still sees seq/ack.
        This guarantees the server will bump last_message/lastmovemessage.
        """
        if hasattr(self._conn, "send_unreliable"):
            maybe = self._conn.send_unreliable(payload)
        elif hasattr(self._conn, "send"):
            # Some transports name it `send` but still frame it correctly.
            maybe = self._conn.send(payload)
        else:
            await self.send_reliable_raw(payload)  # Counts the reliable fallback.
            return
        await self._maybe_await(maybe)
        self.last_tx_at = time.monotonic()
        self._net_stats["packets_sent"] += 1
        self._net_stats["bytes_sent"] += len(payload)
        self._net_stats["unreliable_messages_sent"] += 1

    @property
    def player_entity(self):
        return self.entities[self.view_entity]

    def record_demo(self):
        d = Demo()
        d.start_recording()
        for index, message in enumerate(self._demo_signon):
            d.add_message(self.angles, message, index == 0)
        for message in self._demo_state.values():
            d.add_message(self.angles, message, False)
        if self.protocol.pext2 & proto.PEXT2_REPLACEMENTDELTAS and self.time is not None:
            header = b'\x56'
            if self.protocol.pext2 & proto.PEXT2_PREDINFO:
                header += struct.pack('<H', self.input_ack or 0)
            header += struct.pack('<f', self.time)
            # One complete entity per message keeps large snapshots bounded by
            # the demo/network message limit. The first message resets the world.
            d.add_message(self.angles, header + b'\x00\x80\x00\x00', False)
            for number, state in self._fte_entities.items():
                d.add_message(self.angles, header + proto.encode_fte_entity_number(number)
                              + proto.encode_fte_delta(state, self.protocol) + b'\0\0', False)
        self._demos.append(d)
        return d

    def _finish_status(self, success: bool) -> None:
        if self._status_waiter is not None and not self._status_waiter.done():
            self._status_waiter.set_result(success)

    def _read_status_print(self, text: str) -> None:
        if text.startswith("\x01"):  # Marked chat cannot supply status records.
            return
        clean = _strip_quake_colors(text, colorize_masked_red=False, strip_ansi_codes=True)
        for players in self._status_parser.feed(clean):
            self._status_players = {
                slot: player for slot, player in players.items()
                if slot not in self._score_state or (
                    self._score_state[slot].get("active")
                    and _name_key(self._score_state[slot].get("name", "")) == _name_key(player.name))
            }
            for player in players.values():
                if player.address is not None:
                    self.iplog.add(player.address, player.name)
            try:
                self.iplog.save()
            except (OSError, ValueError) as exc:
                logger.warning("Could not save IP history: %s", exc)
            self._finish_status(True)

    async def refresh_status(self, timeout: float = 1.5) -> bool:
        """Request a complete status reply without suppressing server prints."""
        async with self._status_lock:
            if self.disconnected:
                return False
            self._status_parser.reset()
            self._status_waiter = asyncio.get_running_loop().create_future()
            try:
                await self.send_command("status")
                return await asyncio.wait_for(asyncio.shield(self._status_waiter), timeout)
            except asyncio.TimeoutError:
                return False
            finally:
                if not self._status_waiter.done():
                    self._status_waiter.cancel()
                self._status_waiter = None

    def _cache_demo_state(self, parsed, raw):
        kind = parsed.msg_type
        if kind == proto.ServerMessageType.SERVERINFO:
            self._demo_signon.clear()
            self._demo_state.clear()
            self._demo_signon_complete = False
        indexed = {
            proto.ServerMessageType.UPDATENAME: "client_num",
            proto.ServerMessageType.UPDATEFRAGS: "client_num",
            proto.ServerMessageType.UPDATECOLORS: "client_num",
            proto.ServerMessageType.LIGHTSTYLE: "index",
            proto.ServerMessageType.UPDATESTAT: "index",
            proto.ServerMessageType.UPDATESTATBYTE: "index",
            proto.ServerMessageType.UPDATESTATFLOAT: "index",
            proto.ServerMessageType.UPDATESTATSTRING: "index",
            proto.ServerMessageType.PRECACHE: "index",
        }
        if kind in indexed:
            stat_kind = proto.ServerMessageType.UPDATESTAT if kind in (
                proto.ServerMessageType.UPDATESTATBYTE, proto.ServerMessageType.UPDATESTATFLOAT) else kind
            key = (stat_kind, getattr(parsed, indexed[kind]))
        elif kind in (proto.ServerMessageType.TIME, proto.ServerMessageType.SETVIEW,
                      proto.ServerMessageType.SETANGLE, proto.ServerMessageType.SETPAUSE,
                      proto.ServerMessageType.CDTRACK, proto.ServerMessageType.CLIENTDATA,
                      proto.ServerMessageType.SKYBOX, proto.ServerMessageType.FOG):
            key = (kind, None)
        elif self._demo_signon_complete and kind in (
                proto.ServerMessageType.SPAWNSTATIC, proto.ServerMessageType.SPAWNSTATIC2,
                proto.ServerMessageType.FTE_SPAWNSTATIC, proto.ServerMessageType.SPAWNSTATICSOUND,
                proto.ServerMessageType.SPAWNSTATICSOUND2, proto.ServerMessageType.DP_SPAWNSTATICSOUND):
            # Statics persist for the map, including ones created after sign-on.
            key = (kind, len(self._demo_state))
        else:
            return
        self._demo_state[key] = raw

    def _mono_now(self):
        return time.monotonic() - self._mono0

    def _pq_read_byte_pq(self, b: bytes, i: int) -> tuple[int, int]:
        """ProQuake MSG_ReadBytePQ: next two bytes are high/low nibbles; returns value and new index."""
        if i + 1 >= len(b):
            raise ValueError("truncated PQ byte")
        if not (16 <= b[i] <= 31 and 16 <= b[i + 1] <= 31):
            raise ValueError("invalid PQ nibbles")
        return (b[i] * 16 + b[i + 1] - 272, i + 2)

    def _pq_read_short_pq(self, b: bytes, i: int) -> tuple[int, int]:
        """ProQuake MSG_ReadShortPQ (big-endian of two PQ-bytes); signed 16-bit."""
        hi, i = self._pq_read_byte_pq(b, i)
        lo, i = self._pq_read_byte_pq(b, i)
        val = (hi << 8) | lo
        if val & 0x8000:
            val -= 0x10000
        return (int(val), i)

    def _pq_reset_teams(self):
        self._pq_team_frags.clear()
        self._pq_team_colors.clear()

    def _handle_proquake_stufftext(self, s: str) -> str:
        """Apply a PQ command prefix and return any following ordinary stufftext.

        PQ shorts occupy four encoded bytes. Unknown or malformed payloads are
        discarded without changing state or interpreting binary data as commands.
        """
        if not s.startswith("\x01"):
            return s
        b = s.encode("latin-1")
        if len(b) < 2:
            return ""
        cmd = b[1]
        # pqc_nop, new_team, erase_team, team_frags, match_time, match_reset
        size = {1: 2, 2: 4, 3: 3, 4: 7, 5: 6, 6: 2}.get(cmd)
        if size is None:
            logger.debug("Ignoring unknown ProQuake command %d", cmd)
            return ""
        try:
            if len(b) < size:
                raise ValueError("truncated payload")
            if cmd in (2, 3, 4):
                team = b[2] - 16
                if not 0 <= team <= 13:
                    raise ValueError("invalid team color")
            if cmd == 2:
                shirt = b[3] - 16
                if not 0 <= shirt <= 15:
                    raise ValueError("invalid shirt color")
                self._pq_team_colors[team] = 16 * shirt + team
                self._pq_team_frags[team] = 0
            elif cmd == 3:
                self._pq_team_colors.pop(team, None)
                self._pq_team_frags.pop(team, None)
            elif cmd == 4:
                frags, _ = self._pq_read_short_pq(b, 3)
                self._pq_team_frags[team] = frags
            elif cmd == 5:
                minutes, i = self._pq_read_byte_pq(b, 2)
                seconds, _ = self._pq_read_byte_pq(b, i)
                self._set_match_time(minutes, seconds)
            elif cmd == 6:
                self._pq_reset_teams()
            if cmd in (2, 4, 5, 6):
                self._pq_teamgame_hint = True
        except ValueError as exc:
            logger.warning("Ignoring malformed ProQuake command %d: %s", cmd, exc)
            return ""
        return s[size:]

    def _pq_clock_now(self) -> float:
        # Keep the clock source of the last sample until another sample arrives,
        # so a timer received before the first svc_time cannot jump on signon.
        if self._pq_uses_server_time and self.time is not None:
            return self.time
        return self._mono_now()

    def _set_match_time(self, minutes: int, seconds: int) -> None:
        self._pq_minutes, self._pq_seconds = minutes, seconds
        self._pq_uses_server_time = self.time is not None
        self._pq_last_time_seen = self._pq_clock_now()
        if self._pq_paused:
            self._pq_pause_time = self._pq_last_time_seen

    def _handle_proquake_print(self, text: str) -> None:
        """Handle legacy printed timer samples and match pause notifications."""
        text = text.strip()
        match = re.fullmatch(r"Match ends in (\d+) minutes?(?: remaining)?|(\d+) minutes? remaining",
                             text)
        if match:
            self._set_match_time(int(match.group(1) or match.group(2)), 0)
            self._pq_teamgame_hint = True
        elif text == "Match paused":
            if not self._pq_paused:
                self._pq_pause_time = self._pq_clock_now()
                self._pq_paused = True
        elif text == "Match unpaused":
            if self._pq_paused and self._pq_pause_time is not None:
                if self._pq_last_time_seen is not None:
                    self._pq_last_time_seen += max(0.0, self._pq_clock_now() - self._pq_pause_time)
            self._pq_paused = False
            self._pq_pause_time = None
        elif text == "The match is over":
            self._pq_paused = False
            self._pq_pause_time = None
            self._set_match_time(255, 0)

    def _current_match_time(self) -> tuple[int, int] | None:
        """Return remaining (mm, ss), SD (254, 0), or a negative countdown."""
        if self._pq_minutes in (None, 255) or self._pq_seconds is None:
            return None
        mm = int(self._pq_minutes)
        ss = int(self._pq_seconds)
        if mm == 254:
            return (254, 0)
        if ss >= 128:
            return (0, -(ss - 128))
        if self._pq_last_time_seen is not None:
            now = self._pq_pause_time if self._pq_paused else self._pq_clock_now()
            if now is None:
                now = self._pq_last_time_seen
            dt = max(0.0, float(now) - float(self._pq_last_time_seen))
            total = max(0, math.ceil(mm * 60 + ss - dt))
            return (total // 60, total % 60)
        return (mm, ss)

    def _parse_matchtime_string(self, s: str) -> tuple[int, int] | None:
        """
        Accepts 'mm:ss' or a single integer. Heuristics:
          - 'mm:ss' → obvious
          - integer >= 100 → treat as seconds
          - integer < 100  → treat as minutes
        """
        if not isinstance(s, str) or not s.strip():
            return None
        s = s.strip()
        m = re.match(r"^\s*(\d+)\s*:\s*(\d+)\s*$", s)
        if m:
            mm = int(m.group(1)); ss = int(m.group(2))
            return (max(0, mm), max(0, min(59, ss)))
        # plain integer
        try:
            val = int(s)
        except Exception:
            return None
        if val >= 100:  # likely seconds
            mm, ss = divmod(val, 60)
            return (max(0, mm), max(0, min(59, ss)))
        # likely minutes
        return (max(0, val), 0)

    def _fallback_matchtime_from_serverinfo(self) -> tuple[int, int] | None:
        """
        Prefer serverinfo 'matchtime'; if missing, try 'timelimit'.
        """
        s = self.serverinfo_get("matchtime") or self.serverinfo_get("timelimit")
        return self._parse_matchtime_string(s) if s else None

    def _derive_team_frags_from_players(self) -> dict[int, int]:
        """Fallback if server doesn't send pqc_team_frags: sum frags by pants color."""
        totals: dict[int, int] = {}
        for r in self._score_state.values():
            if not r.get("active"):
                continue
            bc = r.get("bottomcolor")
            fr = r.get("frags")
            if bc is None or fr is None:
                continue
            totals[int(bc) & 15] = totals.get(int(bc) & 15, 0) + int(fr)
        return totals

    def _teams_for_footer(self) -> tuple[dict[int,int], bool]:
        """
        Returns (totals_by_team, have_teams).
        Prefers PQ team_frags; falls back to derived red/blue if meaningful.
        """
        if self._pq_team_frags:
            return (dict(self._pq_team_frags), True)
        derived = self._derive_team_frags_from_players()
        # If classic red/blue show up, treat as teams
        have_rb = (4 in derived) or (13 in derived)
        return (derived if have_rb else {}, bool(have_rb or self._pq_teamgame_hint))

    def format_showscores_footer(self, width: int = 47) -> str:
        """
        Build the single footer line:
            "          10:00     🟥: 7   🟦: 3"
        or
            "          0:00     No Teams"
        """
        t = self._current_match_time()
        if self._pq_minutes == 255:
            timer = ""  # An explicit hidden clock must not use serverinfo fallback.
        elif self._pq_minutes == 254:
            timer = "SD"
        elif self._pq_seconds is not None and self._pq_seconds >= 128:
            timer = f"-0:{self._pq_seconds - 128:02d}"
        else:
            if t is None:
                t = self._fallback_matchtime_from_serverinfo()
            mm, ss = (t if t is not None else (0, 0))
            timer = f"{mm}:{ss:02d}"

        # Teams
        totals, have_teams = self._teams_for_footer()
        if not have_teams or not totals:
            return f"{'':>10}{timer:<7}     No Teams"

        # Prefer classic red(4) and blue(13); fall back to two most common
        def pick_two(d: dict[int, int]) -> list[int]:
            pref = []
            for k in (4, 13):
                if k in d:
                    pref.append(k)
            if len(pref) >= 2:
                return pref[:2]
            # fill with the best remaining by abs score then key
            rest = sorted([k for k in d.keys() if k not in pref], key=lambda k: (-abs(d[k]), k))
            return (pref + rest)[:2]

        k1, k2 = (pick_two(totals) + [None, None])[:2]
        parts = []
        if k1 is not None:
            parts.append(f"{_pants_color_emoji(k1)}: {totals.get(k1, 0)}")
        if k2 is not None:
            parts.append(f"{_pants_color_emoji(k2)}: {totals.get(k2, 0)}")
        if not parts:
            return f"{'':>10}{timer:<7}     No Teams"

        body = "   ".join(parts)
        return f"{'':>10}{timer:<7}     {body}"

    async def _maybe_await(self, maybe):
        """Handle both sync and async send_reliable implementations safely."""
        if inspect.isawaitable(maybe):
            return await maybe
        return maybe

    async def send_reliable_raw(self, payload: bytes):
        # If we're offline (or transport is gone), do nothing.
        if self.disconnected or not self._alive:
            return
        if not hasattr(self._conn, "send_reliable"):
            return
        await self._maybe_await(self._conn.send_reliable(payload))
        self.last_tx_at = time.monotonic()
        # Count packet/message/bytes
        try:
            self._net_stats["packets_sent"] += 1
            self._net_stats["reliable_messages_sent"] += 1
            self._net_stats["bytes_sent"] += int(len(payload) or 0)
        except Exception:
            pass

    async def send_reliable_nop(self):
        await self.send_reliable_raw(b"\x01")

    async def send_unreliable_nop(self):
        """Send clc_nop as UNRELIABLE (or reliable fallback) with netchan header."""
        await self._send_unreliable(b"\x01")

    async def send_rcon_nq(self, cmd_args: str):
        """
        NetQuake RCON control datagram:
          [be32 NETFLAG_CTL|len] [byte CCREQ_RCON] [pw\0] [args\0]
        """
        pw = cvar_system.get_string("rcon_password", "")
        if not pw:
            CON.write("⚠️  rcon_password is empty. Use: set rcon_password \"yourpw\"")
            return
        body = _nq_rcon_body(pw, cmd_args or "")
        pkt  = _nq_pack_control(body)
        if not hasattr(self._conn, "send_oob"):
            CON.write("⚠️  Transport has no send_oob(); cannot send RCON.")
            return
        maybe = self._conn.send_oob(pkt)
        if inspect.isawaitable(maybe):
            await maybe

    async def _small_move_unreliable(self):
        """Tiny clc_move to tick server lastMessageTime using proper framing."""
        try:
            p, y, r = self.angles
        except Exception:
            p = y = r = 0.0
        # 1 unit forward, no buttons/impulse; this is UNRELIABLE.
        payload = self._make_move_body(
            p, y, r, 1, 0, 0, 0, 0,
            self.high_res_inputs,
            self.time if self.time is not None else self._mono_now()
        )
        await self._send_unreliable(payload)

    async def _small_move_reliable(self):
        """Tiny clc_move, but sent RELIABLY to punch through bad paths/NATs."""
        try:
            p, y, r = self.angles
        except Exception:
            p = y = r = 0.0
        payload = self._make_move_body(
            p, y, r, 1, 0, 0, 0, 0,
            self.high_res_inputs,
            self.time if self.time is not None else self._mono_now()
        )
        await self.send_reliable_raw(payload)

    async def _read_messages(self):
        zero_baseline = Entity.make_zero_baseline()

        while True:
            remaining_msg = msg = wire_msg = await self._conn.read_message()
            frame_sequence = getattr(self._conn, 'last_message_sequence', None)
            has_fte_update = False
            # Count inbound packet/frame + bytes
            try:
                self._net_stats["packets_received"] += 1
                self._net_stats["reliable_messages_received"] += 1  # frame-based approximation
                self._net_stats["bytes_received"] += int(len(msg) or 0)
            except Exception:
                pass
            # Bump last RX to keep watchdog satisfied
            self.last_rx_at = time.monotonic()
            # NOTE: from here on we may print async while input() waits.



            has_client_update = False
            has_server_info = False
            signon_complete = False
            next_entities = {}
            while remaining_msg:
                unparsed = remaining_msg
                parsed, remaining_msg = proto.ServerMessage.parse_message(
                    remaining_msg, self.protocol
                )
                self._cache_demo_state(parsed, unparsed[:len(unparsed) - len(remaining_msg)])
                if parsed.msg_type == proto.ServerMessageType.SIGNONNUM and parsed.num >= 3:
                    signon_complete = True
                # Any of these means we've definitely got inbound data flowing.
                if parsed.msg_type in (
                    proto.ServerMessageType.SERVERINFO,
                    proto.ServerMessageType.SIGNONNUM,
                    proto.ServerMessageType.TIME,
                    proto.ServerMessageType.SETVIEW,
                    proto.ServerMessageType.CLIENTDATA,
                ):
                    self._signon_seen = True
                if parsed.msg_type == proto.ServerMessageType.STUFFTEXT:
                    DBG_ONCE("stufftext", f"STUFFTEXT: {parsed.string.strip()[:90]!r}")
                    # PQ binary prefixes can be followed by ordinary stufftext.
                    parsed.string = self._handle_proquake_stufftext(parsed.string)
                    if not parsed.string:
                        continue

                    # Handle protocol extension negotiation
                    if parsed.string.strip() == "cmd pext":
                        # Server is negotiating FTE/QSS extensions.
                        self._saw_cmd_pext = True
                        await self.send_reliable_raw(_make_cmd_body(proto.pext_command() + '\n'))
                    # Handle ping requests to prevent lag detection
                    if parsed.string.strip() == "cmd ping":
                        await self.send_reliable_raw(_make_cmd_body("ping"))
                    # Some servers send generic 'cmd' wrappers; respond to a few safe ones
                    elif parsed.string.strip() == "cmd status":
                        await self.send_reliable_raw(_make_cmd_body("status"))
                    elif parsed.string.strip().startswith("cmd say "):
                        # ignore; no auto-chat
                        pass
                    # --- IMPORTANT: only react to a standalone 'impulse N' line (not inside alias bodies) ---
                    # Scan line-by-line; require an exact 'impulse <num>' line.
                    try:
                        for _line in parsed.string.splitlines():
                            s = _line.strip()
                            m = re.match(r"(?i)^impulse\s+(\d+)$", s)
                            if not m:
                                continue
                            imp = int(m.group(1)) & 0xFF
                            # Optional allowlist to avoid accidental weapon/menu impulses from mods
                            if imp not in ALLOWED_STUFFCMD_IMPULSES:
                                logger.debug("Ignoring STUFFCMD impulse %d (not in allowlist)", imp)
                                continue
                            # Queue once and also flush a tiny move immediately
                            self._pending_impulse = imp
                            try:
                                logger.debug("Responding to STUFFCMD impulse %d", imp)
                                await self._send_heartbeat(impulse=imp, reliable=False)
                            except Exception:
                                try:
                                    await self._send_heartbeat(impulse=imp, reliable=True)
                                except Exception:
                                    pass
                            break  # handle only the first valid standalone impulse line
                    except Exception:
                        pass

                    # Capture server-sent alias definitions:  alias <name> <body...>
                    try:
                        for line in parsed.string.splitlines():
                            # --- InfoString updates (handle per-line) ---
                            sline = line.strip()

                            # NEW: strip comment & 'cmd' wrapper some servers add
                            if sline.startswith("//"):
                                sline = sline[2:].lstrip()
                            if sline.lower().startswith("cmd "):
                                sline = sline[4:].lstrip()

                            low = sline.lower()

                            if self._handle_info_stufftext(sline):
                                continue

                            # fullserverinfo / serverinfo "<info>"  (quotes optional)
                            if low.startswith("fullserverinfo ") or low.startswith("serverinfo "):
                                payload = sline.split(None, 1)[1]
                                self._serverinfo_is = InfoString.parse(payload)
                                DBG_ONCE("serverinfo-captured", f"captured serverinfo ({len(self._serverinfo_is)} keys)")
                                continue
                            # userinfo <slot> <info>    (others)
                            m = re.match(r'(?i)^userinfo\s+(\d+)\s+(.+)$', sline)
                            if m:
                                slot = int(m.group(1))
                                self._userinfo_is[slot] = InfoString.parse(m.group(2))
                                self._on_userinfo_changed(slot)
                                DBG_ONCE("userinfo-slot", f"userinfo for slot {slot} (keys={len(self._userinfo_is[slot])})")
                                continue
                            # userinfo <info>           (ours)
                            m = re.match(r'(?i)^userinfo\s+(.+)$', sline)
                            if m:
                                self._myinfo_is = InfoString.parse(m.group(1))
                                DBG_ONCE("userinfo-self", f"my userinfo captured (keys={len(self._myinfo_is)})")
                                continue
                            # setinfo key value         (single kv updates)
                            m = re.match(r'(?i)^setinfo\s+(?:"([^"]+)"|(\S+))\s+(?:"([^"]*)"|(\S+))\s*$', sline)
                            if m:
                                k = (m.group(1) or m.group(2) or "").strip()
                                v = (m.group(3) or m.group(4) or "").strip()
                                if k:
                                    self._myinfo_is[k] = v
                                    DBG_ONCE(f"setinfo-{k}", f"setinfo {k}={v!r}")
                            # (fall through to alias capture as you already had)
                            if re.match(r"(?i)^\s*alias\s", line):
                                alias_tokens = shlex.split(line)
                                if len(alias_tokens) >= 3:
                                    cvars = globals().get("cvar_system")
                                    define_alias(
                                        self.aliases, alias_tokens[1], " ".join(alias_tokens[2:]),
                                        reserved=lambda key: key in _RESERVED_CMDS or
                                        (cvars is not None and key in cvars._commands),
                                        is_cvar=lambda key: cvars is not None and cvars.find(key) is not None,
                                        allow_overlap=cvars.get_bool("cl_aliasoverlap") if cvars is not None else False)
                    except Exception:
                        # Bad alias lines should never kill the RX loop
                        pass

                # Human-visible server text (console lines, chat, kills, etc.)
                if parsed.msg_type == proto.ServerMessageType.PRINT:
                    self._read_status_print(parsed.string)
                    clean = _strip_quake_colors(parsed.string).strip()
                    DBG_ONCE("print", f"PRINT: {clean[:90]!r}")
                    # Capture last chat message for 'like' command (team-aware)
                    try:
                        _clean = _strip_quake_colors(parsed.string).strip()
                        mchat = _CHAT_RE.match(_clean)
                        if mchat:
                            self.last_chat_msg = mchat.group("msg").strip()
                            self.last_chat_is_team = bool(mchat.group("t"))
                    except Exception:
                        pass
                    # Use a cleaned/dequaked view for pattern matching
                    msg_raw = parsed.string
                    msg = _strip_quake_colors(msg_raw).rstrip()
                    msg_lower = msg.lower()

                    # Minimal surface: still surface auth/ban/full hints
                    if "server is full" in msg_lower or "full" in msg_lower:
                        print("👥 Server is full!")
                    elif "banned" in msg_lower or "ban" in msg_lower:
                        print("🚫 You are banned from this server!")

                    # --- QSS-M style triggers: q_version / f_version / q_sysinfo / f_system ---
                    # Use only the chat message payload for matching (ignores "nick:" and team tags).
                    # Then aggressively strip control chars and stray '#', which can appear from backspace flips.
                    try:
                        chat_payload = (mchat.group("msg").strip() if mchat else msg)
                        # Remove ASCII controls + DEL + stray '#'
                        trigger_text = re.sub(r"[\x00-\x1F\x7F#]+", "", chat_payload).strip()

                        # Optional: ignore our own asks to avoid echo (uncomment to enable)
                        # my_name = _sanitize_player_name(cvar_system.get_string("name","pyquake"))
                        # if mchat and _sanitize_player_name(mchat.group("nick")) == my_name:
                        #     pass

                        # Normalize hard for matching: strip non [a-z0-9]
                        norm = re.sub(r"[^a-z0-9]+", "", trigger_text.lower())

                        # version?
                        if ("qversion" in norm) or ("fversion" in norm):
                            now = time.monotonic()
                            if now - self._last_version_reply_at >= 20.0:
                                plat = _platform_label()
                                bits = _bitness()
                                await self.send_reliable_raw(_make_cmd_body(f"say {CLIENT_VER} {plat} {bits}-bit"))
                                self._last_version_reply_at = now

                        # sysinfo? -> send multiple lines (like QSS-M), with small delays to avoid flood kicks
                        if ("qsysinfo" in norm) or ("fsystem" in norm):
                            now = time.monotonic()
                            if now - self._last_sysinfo_reply_at >= 20.0:
                                if platform.system() == "Darwin":
                                    lines = _compose_multi_sysinfo_lines_mac()
                                else:
                                    # Fallback: keep compact one-liner on non-mac
                                    lines = [re.sub(r"[^\x20-\x7E]", "", _compose_sysinfo_line())[:200]]
                                for s in lines:
                                    await self.send_reliable_raw(_make_cmd_body(f"say {s}"))
                                    await asyncio.sleep(0.20)
                                self._last_sysinfo_reply_at = now

                        # f_random? -> reply with random bundle (coin, rps, color, blackjack, 1-100)
                        if ("qrandom" in norm) or ("frandom" in norm):
                            now = time.monotonic()
                            if now - self._last_random_reply_at >= 20.0:
                                random_bundle = _generate_random_bundle()
                                await self.send_reliable_raw(_make_cmd_body(f"say {random_bundle}"))
                                self._last_random_reply_at = now
                    except Exception:
                        pass

                    self._handle_proquake_print(msg)

                    _display_server_print(parsed.string)

                # Player goes "unspawned" when server info received (see SV_SendServerinfo).
                if parsed.msg_type == proto.ServerMessageType.SERVERINFO:
                    # Auto-save on map change: any new SERVERINFO with a different level
                    try:
                        if self.level_name and parsed.level_name and parsed.level_name != self.level_name:
                            # autosave on map change; only *announce* when developer=1
                            _autosave_config(silent=True)
                            if _dev_enabled():
                                try: CON.write(f"💾 Auto-saved config (map change: {self.level_name} → {parsed.level_name})")
                                except Exception: pass
                    except Exception: pass
                    # Reset centerprint dedupe on new level/serverinfo.
                    self._last_centerprint_logged = None
                    self.level_name = parsed.level_name
                    self.view_entity = None
                    self.level_finished = False
                    self.time = None
                    self.entities.clear()
                    self.baselines.clear()
                    self._fte_entities.clear()
                    self._fte_baselines.clear()
                    self.stats.clear()
                    self.stat_strings.clear()
                    self._move_sequence = 0
                    self.input_ack = None
                    self.velocity = (0., 0., 0.)
                    self.on_ground = None
                    self.view_height = None
                    self.gamedir = parsed.gamedir
                    self.sounds = parsed.sounds
                    self._userinfo_is.clear()
                    self._synthetic_userinfo.clear()
                    self._latest_ping_by_name.clear()
                    self._status_players.clear()
                    self._status_parser.reset()
                    self._last_connected_slot = None
                    self._finish_status(False)
                    self._pq_reset_teams()
                    self._pq_teamgame_hint = False
                    self._pq_minutes = self._pq_seconds = self._pq_last_time_seen = None
                    self._pq_paused = False
                    self._pq_pause_time = None
                    self._pq_uses_server_time = False
                    if self._spawned_fut.done():
                        self._spawned_fut = asyncio.Future()
                    self.models = parsed.models
                    has_server_info = True

                    if parsed.protocol != self.protocol:
                        CON.write(f"Using protocol {parsed.protocol.name}")
                    self.protocol = parsed.protocol
                    self._pext_bits = [proto.PROTOCOL_FTE_PEXT1, self.protocol.pext1,
                                       proto.PROTOCOL_FTE_PEXT2, self.protocol.pext2]
                    if self.protocol.pext2 & proto.PEXT2_REPLACEMENTDELTAS:
                        await self._send_unreliable(b'\x32\xff\xff\xff\xff')

                    # New level/serverinfo → clear scoreboard state (prevents ghosts)
                    try:
                        self._score_state.clear()
                    except Exception:
                        pass

                if parsed.msg_type == proto.ServerMessageType.VERSION:
                    self.protocol = parsed.protocol

                # --- Live scoreboard updates (names, frags, colors) -------------
                # These are classic NetQuake svc_* messages; proto enums vary a bit across forks,
                # so we guard with hasattr/getattr to avoid hard-failing if absent.
                try:
                    if hasattr(proto.ServerMessageType, "UPDATENAME") and parsed.msg_type == proto.ServerMessageType.UPDATENAME:
                        i = getattr(parsed, "client", getattr(parsed, "client_num", None))
                        nm_raw = getattr(parsed, "name", None)
                        if i is not None and nm_raw is not None:
                            row = self._score_state.setdefault(int(i), {})
                            nm_full = _sanitize_player_name_full(nm_raw)
                            nm_short = nm_full[:15]  # Empty names mark unused slots.
                            if not nm_short or row.get("name") != nm_short:
                                self._status_players.pop(int(i), None)
                            if nm_short and not row.get("active"):
                                self._last_connected_slot = int(i)
                            if nm_short:
                                if row.get("name") != nm_short:
                                    self._latest_ping_by_name.pop(row.get("name", ""), None)
                                    row.pop("name_full", None)
                                row["active"] = True
                                row["name"] = nm_short
                                # Only upgrade; never downgrade name_full
                                _maybe_upgrade_name_full(row, nm_short, nm_full)
                            else:
                                # Release cached state before this slot is reused.
                                self._latest_ping_by_name.pop(row.get("name", ""), None)
                                self._userinfo_is.pop(int(i), None)
                                self._synthetic_userinfo.pop(int(i), None)
                                row.clear()
                                row["active"] = False
                            row["t_last"] = time.monotonic()
                except Exception:
                    pass

                try:
                    if hasattr(proto.ServerMessageType, "UPDATEFRAGS") and parsed.msg_type == proto.ServerMessageType.UPDATEFRAGS:
                        i = getattr(parsed, "client", getattr(parsed, "client_num", None))
                        fr = parsed.count
                        if i is not None and fr is not None:
                            row = self._score_state.setdefault(int(i), {})
                            row["frags"] = int(fr)
                            row["t_last"] = time.monotonic()
                except Exception:
                    pass

                # --- Userinfo / setinfo updates (most reliable color source) ---
                try:
                    got_uinfo = False
                    if hasattr(proto.ServerMessageType, "UPDATEUSERINFO") and parsed.msg_type == proto.ServerMessageType.UPDATEUSERINFO:
                        got_uinfo = True
                    elif hasattr(proto.ServerMessageType, "UPDATEINFO") and parsed.msg_type == proto.ServerMessageType.UPDATEINFO:
                        got_uinfo = True
                    elif hasattr(proto.ServerMessageType, "SETINFO") and parsed.msg_type == proto.ServerMessageType.SETINFO:
                        got_uinfo = True
                    if got_uinfo:
                        DBG_ONCE("colors", "Received UPDATECOLORS/SETINFO (color refresh observed)")
                        i = getattr(parsed, "client", getattr(parsed, "client_num", None))
                        info = getattr(parsed, "info", getattr(parsed, "string", None))
                        if i is not None and isinstance(info, str):
                            idx = int(i)
                            row = self._score_state.setdefault(idx, {})
                            row["userinfo"] = info
                            # keep InfoString cache in sync
                            try:
                                self._userinfo_is[idx] = InfoString.parse(info)
                            except Exception:
                                pass
                            bc = _info_get_int(info, "bottomcolor")
                            if bc is not None:
                                row["bottomcolor"] = int(bc) & 15
                            # debug line
                            try:
                                base = self.baselines.get(idx + 1)
                                baseline_cm = getattr(base, "colormap", None)
                            except Exception:
                                baseline_cm = None
                            _debug_print_color_event("setinfo", idx, row.get("name"), bc, None, baseline_cm, row.get("bottomcolor"), _pants_color_emoji(row.get("bottomcolor")))
                except Exception:
                    pass

                try:
                    got_uclr = (
                        (hasattr(proto.ServerMessageType, "UPDATECOLORS") and parsed.msg_type == proto.ServerMessageType.UPDATECOLORS)
                        or (hasattr(proto.ServerMessageType, "UPDATECOLOURS") and parsed.msg_type == proto.ServerMessageType.UPDATECOLOURS)
                    )
                    if got_uclr:
                        i = getattr(parsed, "client", getattr(parsed, "client_num", None))
                        # Broad extraction
                        bottom = _extract_bottom_from_updatecolors(parsed)
                        # One-time concise dump to see what fields exist on this server
                        global _UCLR_RAW_DUMPS
                        if _COLOR_DEBUG and _UCLR_RAW_DUMPS < 1:  # Only show once
                            try:
                                keys = [k for k in dir(parsed) if not k.startswith("_") and not callable(getattr(parsed, k, None))]
                                key_list = [k for k in keys if hasattr(parsed, k)][:4]  # Show max 4 fields
                                CON.write(f"[c] uclr-fields: {','.join(key_list)}")
                            except Exception:
                                pass
                            _UCLR_RAW_DUMPS += 1

                        if i is not None:
                            row = self._score_state.setdefault(int(i), {})
                            if bottom is not None:
                                row["bottomcolor"] = int(bottom) & 15
                            row["topcolor"] = (parsed.color >> 4) & 15
                            # debug line
                            try:
                                base = self.baselines.get(int(i) + 1)
                                baseline_cm = getattr(base, "colormap", None)
                            except Exception:
                                baseline_cm = None
                            nm = row.get("name")
                            _debug_print_color_event(
                                "updatecolors",
                                int(i), nm,
                                bottom, None, baseline_cm,
                                row.get("bottomcolor"),
                                _pants_color_emoji(row.get("bottomcolor"))
                            )
                            row["t_last"] = time.monotonic()
                except Exception:
                    pass

                # Optional: some engines also push pings/loss directly.
                try:
                    if hasattr(proto.ServerMessageType, "UPDATEPING") and parsed.msg_type == proto.ServerMessageType.UPDATEPING:
                        i = getattr(parsed, "client", getattr(parsed, "client_num", None))
                        pg = getattr(parsed, "ping", None)
                        if i is not None and pg is not None:
                            row = self._score_state.setdefault(int(i), {})
                            row["ping"] = int(pg)
                            row["t_last"] = time.monotonic()
                except Exception:
                    pass

                try:
                    if hasattr(proto.ServerMessageType, "UPDATEPL") and parsed.msg_type == proto.ServerMessageType.UPDATEPL:
                        i = getattr(parsed, "client", getattr(parsed, "client_num", None))
                        pl = getattr(parsed, "pl", getattr(parsed, "loss", None))
                        if i is not None and pl is not None:
                            row = self._score_state.setdefault(int(i), {})
                            row["pl"] = int(pl)
                            row["t_last"] = time.monotonic()
                except Exception:
                    pass

                # Handle sign-on.
                if parsed.msg_type == proto.ServerMessageType.SIGNONNUM:
                    if parsed.num == 1:
                        # Mirror QS: send *ver as early userinfo during signon 1
                        await self.send_reliable_raw(
                            _make_cmd_body(f'setinfo "*ver" "{CLIENT_VER}"\n')
                        )
                        # Also push all userinfo CVARs
                        await self._push_all_userinfo()
                        # --- IMPORTANT: set name/color from CVARs *before* prespawn so the server banner uses them ---
                        try:
                            nm = _sanitize_player_name(cvar_system.get_string("name", "pyquake-m"))
                            top = _clamp_color(int(cvar_system.get_value("topcolor", 0)))
                            bot = _clamp_color(int(cvar_system.get_value("bottomcolor", 0)))
                            await self.send_reliable_raw(_make_cmd_body(f'name "{nm}"'))
                            await self.send_reliable_raw(_make_cmd_body(f"color {top} {bot}"))
                        except Exception:
                            pass
                        # Proceed with prespawn
                        await self.send_reliable_raw(_make_cmd_body("prespawn"))
                    elif parsed.num == 2:
                        # Do not override name/color here; just spawn.
                        await self.send_reliable_raw(_make_cmd_body("spawn "))
                    elif parsed.num == 3:
                        # finish signon
                        await self.send_reliable_raw(_make_cmd_body("begin"))
                        if not self._spawned_fut.done():
                            self._spawned_fut.set_result(None)

                # Set view entity
                if parsed.msg_type == proto.ServerMessageType.SETVIEW:
                    self.view_entity = parsed.viewentity

                # Set view angle
                if parsed.msg_type == proto.ServerMessageType.SETANGLE:
                    self.angles = parsed.view_angles

                # Set velocity and on_ground
                if parsed.msg_type == proto.ServerMessageType.CLIENTDATA:
                    self.velocity = parsed.m_velocity
                    self.on_ground = parsed.on_ground
                    self.view_height = parsed.view_height
                    has_client_update = True
                    for index, name in ((0, 'health'), (2, 'weapon_model_index'), (3, 'ammo'),
                                        (4, 'armor'), (5, 'weapon_frame'), (6, 'shells'),
                                        (7, 'nails'), (8, 'rockets'), (9, 'cells'),
                                        (10, 'active_weapon'), (15, 'items'), (16, 'view_height')):
                        value = getattr(parsed, name, None)
                        if value is not None:
                            self.stats[index] = value

                if parsed.msg_type in (proto.ServerMessageType.UPDATESTAT,
                                       proto.ServerMessageType.UPDATESTATBYTE,
                                       proto.ServerMessageType.UPDATESTATFLOAT):
                    self.stats[parsed.index] = parsed.value
                if parsed.msg_type == proto.ServerMessageType.UPDATESTATSTRING:
                    self.stat_strings[parsed.index] = parsed.value
                if parsed.msg_type == proto.ServerMessageType.PRECACHE:
                    category, index = parsed.index & 0xc000, parsed.index & 0x3fff
                    table = self.models if category == 0 else self.sounds if category == 0x8000 else None
                    if table is not None and index:
                        while len(table) < index:
                            table.append('')
                        table[index - 1] = parsed.name

                if parsed.msg_type == proto.ServerMessageType.FTE_SPAWNBASELINE:
                    state = proto.merge_fte_delta(None, None, parsed.delta)
                    self._fte_baselines[parsed.entity_num] = state
                    self.baselines[parsed.entity_num] = self._fte_entity(parsed.entity_num, state)

                if parsed.msg_type == proto.ServerMessageType.UPDATEENTITIES:
                    has_fte_update = has_client_update = True
                    self.time = parsed.time
                    self.input_ack = parsed.input_ack
                    for number, delta in parsed.updates:
                        if delta is None:
                            if number == 0:
                                self._fte_entities.clear()
                                self.entities.clear()
                            else:
                                self._fte_entities.pop(number, None)
                                self.entities.pop(number, None)
                        else:
                            state = proto.merge_fte_delta(self._fte_entities.get(number),
                                                         self._fte_baselines.get(number), delta)
                            self._fte_entities[number] = state
                            self.entities[number] = self._fte_entity(number, state)
                    if self.protocol.pext2 & proto.PEXT2_PREDINFO:
                        view = self._fte_entities.get(self.view_entity)
                        if view is not None:
                            self.velocity = view.values.get('velocity', (0., 0., 0.))
                            self.on_ground = bool(view.values.get('eflags', 0) & 128)
                        self.view_height = self.stats.get(16, 0)

                # Update entity positions
                if parsed.msg_type in (proto.ServerMessageType.SPAWNBASELINE,
                                       proto.ServerMessageType.SPAWNBASELINE2):
                    ent = Entity.from_baseline(parsed)
                    self.baselines[parsed.entity_num] = ent
                if parsed.msg_type == proto.ServerMessageType.UPDATE:
                    ent_num = parsed.entity_num
                    baseline = self.baselines.get(ent_num, zero_baseline)
                    next_entities[ent_num] = baseline.update(parsed)


                if parsed.msg_type == proto.ServerMessageType.CENTERPRINT:
                    # By default, do not echo centerprints to the console.
                    # If enabled, log once per identical run (consecutive duplicates suppressed).
                    try:
                        if cvar_system.get_bool("con_logcenterprint", False):
                            clean = _strip_quake_colors(parsed.string).strip()
                            if clean and clean != self._last_centerprint_logged:
                                CON.write(f"📢 {clean}")
                                self._last_centerprint_logged = clean
                    except Exception:
                        # Never let logging failures break message handling
                        pass
                    # also keep the future behavior for any waiters
                    self._center_print_fut.set_result(parsed.string)
                    self._center_print_fut = asyncio.Future()

                    # Step all tasks that were waiting on the previous future,
                    # so they have a chance to immediately wait on the next
                    # future.
                    await asyncio.sleep(0)

                if parsed.msg_type == proto.ServerMessageType.TIME:
                    self.time = parsed.time
                    self.input_ack = parsed.input_ack

                if parsed.msg_type == proto.ServerMessageType.INTERMISSION:
                    self.level_finished = True

                if parsed.msg_type == proto.ServerMessageType.DISCONNECT:
                    # Preserve the messages preceding this disconnect.
                    # stop_recording supplies the final marker after capture.
                    wire_msg = wire_msg[:len(wire_msg) - len(remaining_msg) - 1]
                    self.disconnected = True
                    self._alive = False
                    # Auto-save on server-initiated disconnect
                    try:
                        _autosave_config()
                        try: CON.write("💾 Auto-saved config (disconnect).")
                        except Exception: pass
                    except Exception: pass
                    break

            if has_client_update:
                if not self.protocol.pext2 & proto.PEXT2_REPLACEMENTDELTAS:
                    self.entities = next_entities
                self._updated_fut.set_result(None)
                self._updated_fut = asyncio.Future()
            if has_fte_update and frame_sequence is not None and not self.disconnected:
                # Acknowledge only after the complete packet has parsed successfully.
                await self._send_unreliable(b'\x32' + struct.pack('<I', frame_sequence))

            # Synthesize userinfo from scoreboard when real userinfo is missing
            try:
                self._synthesize_userinfo_from_scoreboard()
            except Exception:
                pass

            if has_server_info or (self._demo_signon and not self._demo_signon_complete):
                self._demo_signon.append(bytes(wire_msg))
            if signon_complete:
                self._demo_signon_complete = True
            # Only add messages to demo if demo recording is enabled.
            if hasattr(self, '_demos') and self._demos:
                self._demos = [d for d in self._demos if not d.recording_complete]
                for demo in self._demos:
                    if wire_msg:
                        demo.add_message(self.angles, wire_msg, has_server_info)
            if self.disconnected:
                return

    async def wait_for_update(self):
        await asyncio.shield(self._updated_fut)

    async def wait_until_spawn(self):
        await asyncio.shield(self._spawned_fut)

    async def wait_for_center_print(self):
        return await asyncio.shield(self._center_print_fut)

    def _reader_finished(self, task):
        for demo in self._demos:
            demo.stop_recording()
        exc = None if task.cancelled() else task.exception()
        if exc is not None:
            logger.warning("Connection closed: %s", exc)
        self.disconnected = True
        self._alive = False
        self._finish_status(False)
        for future in (self._spawned_fut, self._updated_fut, self._center_print_fut):
            if not future.done():
                future.cancel()
        if self._cvar_listener is not None:
            cvar_system.unsubscribe(self._cvar_listener)
            self._cvar_listener = None
            self._cvar_subscribed = False
        self._conn.disconnect()

    @classmethod
    async def connect(cls, host, port, joequake_version=None, *, password=None):
        """Connect to the given host and port, and start listening for messages.

        At the point this coroutine returns, no messages have yet been read.

        """
        if password is None:
            password = cvar_system.get_string("password", "") if "cvar_system" in globals() else ""
        conn = await aiodgram.DatagramConnection.connect(host, port, joequake_version,
                                                        password=password)

        client = cls(conn)
        client._read_task = asyncio.create_task(client._read_messages())
        client._read_task.add_done_callback(client._reader_finished)
        return client

    def move(self, pitch, yaw, roll, forward, side, up, buttons, impulse=None):
        self.angles = (pitch, yaw, roll)
        # Choose best cmd_time: server TIME if known, else monotonic since start
        cmd_time = self.time if (self.time is not None) else self._mono_now()
        # Inject pending impulse once (from SVC_STUFFCMD "impulse N")
        if self._pending_impulse:
            impulse = self._pending_impulse
            self._pending_impulse = 0
        if impulse is None:
            impulse = 0
        # NOTE: fire-and-forget; framed by transport or promoted to reliable via wrapper
        asyncio.create_task(self._send_unreliable(
            self._make_move_body(pitch, yaw, roll,
                            forward, side, up, buttons, int(impulse),
                            self.high_res_inputs, cmd_time)))

    async def send_command(self, cmd):
        # Fast-path: many mods only honor impulses that arrive in the next clc_move.
        # We both (a) inject it into a tiny move *now* and (b) still send the string command
        # for engines/mods that accept it that way.
        try:
            m = re.match(r"(?i)^\s*impulse\s+(\d+)\s*$", cmd or "")
            if m:
                imp = int(m.group(1)) & 0xFF
                self._pending_impulse = imp  # in case the next real move happens first
                try:
                    await self._send_heartbeat(impulse=imp, reliable=False)
                except Exception:
                    await self._send_heartbeat(impulse=imp, reliable=True)
        except Exception:
            pass
        # Bail out if offline to avoid waiting on an ACK that will never arrive.
        if self.disconnected or not self._alive:
            return
        # Normal path when connected.
        await self.send_reliable_raw(_make_cmd_body(cmd))

    # ---- CVAR <-> network integration ----
    def _bind_cvar_subscriptions(self):
        if self._cvar_subscribed:
            return
        def _on_cvar_changed(cvar: Cvar, old: str, new: str):
            # Push USERINFO vars through setinfo when changed
            if (cvar.flags & CvarFlags.USERINFO) and self._alive and not self.disconnected:
                try:
                    asyncio.create_task(self.send_reliable_raw(_make_cmd_body(f'setinfo "{cvar.name}" "{cvar.string}"\n')))
                    # also reflect locally so myinfo_get() is instant
                    try:
                        self._myinfo_is[cvar.name] = cvar.string
                    except Exception:
                        pass
                except Exception:
                    pass
        cvar_system.subscribe(_on_cvar_changed)
        self._cvar_listener = _on_cvar_changed
        self._cvar_subscribed = True

    async def _push_all_userinfo(self):
        """Send all USERINFO CVARs to the server."""
        try:
            for k, v in cvar_system.userinfo().items():
                await self.send_reliable_raw(_make_cmd_body(f'setinfo "{k}" "{v}"\n'))
        except Exception:
            pass

    async def disconnect(self):
        self.disconnected = True
        self._alive = False
        self._finish_status(False)
        if self._read_task is not None:
            self._read_task.cancel()
            await asyncio.gather(self._read_task, return_exceptions=True)
        else:
            for future in (self._spawned_fut, self._updated_fut, self._center_print_fut):
                if not future.done():
                    future.cancel()
            if self._cvar_listener is not None:
                cvar_system.unsubscribe(self._cvar_listener)
                self._cvar_listener = None
                self._cvar_subscribed = False
        try:
            self._conn.disconnect()
        except Exception:
            pass
        try:
            await self._conn.wait_until_disconnected()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    @property
    def high_res_inputs(self):
        return (
            getattr(self._conn, "proquake_angle_hack", False)
            or self.protocol.version != proto.ProtocolVersion.NETQUAKE
            or bool(self.protocol.pext2 & proto.PEXT2_PREDINFO)
        )

    def _make_move_body(self, *args):
        sequence = None
        if self.protocol.pext2 & proto.PEXT2_PREDINFO:
            sequence = self._move_sequence
            self._move_sequence += 1
        return _make_move_body(*args, input_sequence=sequence,
                               protocol_flags=self.protocol.flags)

    @staticmethod
    def _fte_entity(number, state):
        values = state.values
        x, y = values.get('originxy', (0., 0.))
        pitch, roll = values.get('anglesxz', (0., 0.))
        return Entity(number, values.get('model_num', 0), values.get('frame', 0),
                      values.get('colormap', 0), values.get('skin', 0),
                      (x, y, values.get('originz', 0.)),
                      (pitch, values.get('anglesy', 0.), roll))

    @property
    def joequake_version(self):
        return self._conn.joequake_version

    async def _send_heartbeat(self, impulse: int | None = None, reliable: bool = False):
        """
        Send a tiny clc_move immediately to tick server lastMessageTime.
        Optionally carry an impulse (e.g., ClanRing's 39). Use reliable if needed.
        """
        try:
            p, y, r = self.angles
        except Exception:
            p = y = r = 0.0
        imp = int(impulse or 0) & 0xFF
        payload = self._make_move_body(
            p, y, r, 1, 0, 0, 0, imp,
            self.high_res_inputs,
            self.time if self.time is not None else self._mono_now()
        )
        if imp:
            logger.debug("HB move carrying impulse %d", imp)
        if reliable:
            await self.send_reliable_raw(payload)
        else:
            await self._send_unreliable(payload)

    # ---------------- InfoString public helpers ----------------
    def serverinfo_get(self, key: str, default: str = "") -> str:
        return self._serverinfo_is.get_key(key, default)
    def myinfo_get(self, key: str, default: str = "") -> str:
        return self._myinfo_is.get_key(key, default)
    def userinfo_get(self, slot: int, key: str, default: str = "") -> str:
        return self._userinfo_is.get(slot, InfoString()).get_key(key, default)

    def _synthesize_userinfo_from_scoreboard(self) -> None:
        """
        If no real userinfo arrived for a slot, create a tiny InfoString with
        name/topcolor/bottomcolor inferred from scoreboard updates.
        """
        for idx, row in self._score_state.items():
            if not row.get("active"):
                continue
            if (idx in self._userinfo_is
                    and self._userinfo_is[idx] is not self._synthetic_userinfo.get(idx)):
                continue  # real userinfo already present

            nm = row.get("name") or row.get("name_full")
            bc = row.get("bottomcolor")
            tc = row.get("topcolor")

            isynth = InfoString()
            if nm: isynth["name"] = nm
            if tc is not None: isynth["topcolor"] = str(int(tc) & 15)
            if bc is not None: isynth["bottomcolor"] = str(int(bc) & 15)

            if isynth:
                self._userinfo_is[idx] = isynth
                self._synthetic_userinfo[idx] = isynth

    # -------- showscores helpers: ping sweep & parsing ----------
    def _update_pings_from_lines(self, lines: list[str]) -> None:
        """
        Parse classic 'Client ping times' block and merge by player name.
        Populates both _latest_ping_by_name and per-slot pings when we can match.
        """
        try:
            in_block = False
            found_any = False
            # svc_print chunks can split a row or contain the entire ping table.
            for raw in "".join(lines).splitlines():
                s = _strip_quake_colors(raw, colorize_masked_red=False,
                                       strip_ansi_codes=True).strip()
                if not s:
                    continue
                if s.lower().startswith("client ping times"):
                    in_block = True
                    continue
                if not in_block:
                    continue
                # lines like: "  55 woods-py"
                m = re.match(r"^\s*(\d+)\s+(.+?)\s*$", s)
                if not m:
                    continue
                ping = int(m.group(1))
                name = _sanitize_player_name(m.group(2))
                if not name:
                    continue
                self._latest_ping_by_name[name] = ping
                found_any = True
                # Merge into known slots by canonical name key
                name_k = _name_key(name)
                for row in self._score_state.values():
                    if _name_key(row.get("name", "")) == name_k:
                        row["ping"] = ping
                        row["t_last"] = time.monotonic()
            # Optional: clean very old cache entries (keep it tiny)
            if found_any and len(self._latest_ping_by_name) > 64:
                # keep the most-recent names by touching order (not tracking timestamps → simple trim)
                for k in list(self._latest_ping_by_name.keys())[:-32]:
                    self._latest_ping_by_name.pop(k, None)
        except Exception:
            pass

    async def refresh_pings_via_ping(self, timeout: float = 0.6) -> None:
        """
        Issue a 'ping' command, temporarily suppressing PRINTs so the console
        doesn't show the output, capture lines, then parse and merge pings.
        """
        try:
            self._captured_ping_lines.clear()
            self._suppress_prints = True
            # a couple of short attempts to improve reliability on busy servers
            attempts = 2
            per_wait = max(0.5, min(2.0, float(timeout)))
            for _ in range(attempts):
                await self.send_command("ping")
                await asyncio.sleep(per_wait)
                if any(re.search(r"\b\d+\b", ln) for ln in self._captured_ping_lines):
                    break
        except Exception:
            pass
        finally:
            self._suppress_prints = False
        # Parse & merge results
        self._update_pings_from_lines(self._captured_ping_lines)

    # (removed: refresh_fullnames_via_status – we trust UPDATENAME for 31-char names)

def _bottom_for_name_key(score_state: dict[int, dict], name_k: str) -> int | None:
    """Find the most recent bottomcolor for a player by canonical name."""
    for r in score_state.values():
        if r.get("active") and _name_key(r.get("name", "")) == name_k:
            # 1) explicit bottomcolor from UPDATECOLORS / SETINFO
            v = r.get("bottomcolor")
            if v is not None:
                try:
                    return int(v) & 15
                except Exception:
                    pass
            # 2) userinfo bottomcolor (if present)
            bc = _info_get_int(r.get("userinfo"), "bottomcolor")
            if bc is not None:
                return bc & 15
    return None

def _maybe_self_bottom(client: "AsyncClient", name_k: str) -> int | None:
    """If this row is us and no server color is known yet, use our local CVAR."""
    try:
        me = _name_key(cvar_system.get_string("name", "pyquake-m"))
        if name_k == me:
            # Prefer server-reflected myinfo (most accurate)
            try:
                bc = _safe_int(client.myinfo_get("bottomcolor", ""))
                if bc is not None:
                    return bc & 15
            except Exception:
                pass
            # Fallback: packed colors if present. Entity colormaps are player
            # slot references, so they must never be decoded as shirt/pants.
            try:
                colors = _safe_int(client.myinfo_get("colors", ""))
                if colors is not None:
                    return colors & 15
            except Exception:
                pass
            # Final fallback: local CVAR
            return _clamp_color(int(cvar_system.get_value("bottomcolor", 0)))
    except Exception:
        pass
    return None


async def _simple_keep_alive(client):
    """
    Send clc_move every ~100ms (like real clients). This guarantees queued 'impulse N'
    (e.g., 39 from ClanRing) is delivered promptly in the next move.
    """
    while True:
        try:
            if client.disconnected or not client._alive:
                break
            # Use current angles; tiny jitter occasionally to avoid anti-idle heuristics.
            try:
                p, y, r = client.angles
            except Exception:
                p = y = r = 0.0
            if random.random() < 0.10:
                y += random.uniform(-0.1, 0.1)
                fwd = 1
            else:
                fwd = 0
            # buttons=0; side/up=0; impulse=None (auto-injects any pending)
            client.move(p, y, r, fwd, 0, 0, 0, None)
            await asyncio.sleep(0.1)  # ~10 Hz
        except asyncio.CancelledError:
            break
        except Exception:
            # keep quiet; loop will retry until disconnect
            break


async def _monitor_movements(client):
    while True:
        await client.wait_for_update()
        logger.info("%s: Player origin %s",
                    client.time, client.player_entity.origin)





async def _kickstart_handshake(client):
    """
    First ~12s: mirror QS NAT poke:
      - burst a few UNRELIABLE nops+moves right away (game port)
      - drip reliable bytes (nop only, no auto status/ping)
      - stop as soon as any SERVERINFO/SIGNON/TIME is seen
    """
    deadline = time.monotonic() + 12.0
    try:
        # Fast initial burst to open NAT to the server's data port.
        for _ in range(6):  # ~300ms total
            await client.send_unreliable_nop()
            await client._small_move_unreliable()
            await asyncio.sleep(0.05)

        # Some reliable trickle to ensure we have outbound traffic too (no auto status/ping).
        await client.send_reliable_nop()

        # Keep poking until we actually see signon/time flow in.
        while time.monotonic() < deadline and not client.disconnected and not client._signon_seen:
            await client._small_move_unreliable()
            await client.send_unreliable_nop()
            # Every ~1s toss a reliable nop
            await asyncio.sleep(0.25)
            await client.send_reliable_nop()
            await asyncio.sleep(0.25)
    except asyncio.CancelledError:
        return
    except Exception:
        return


# --------------------------- Focus / ChatInfo ---------------------------
def _front_app_name_macos() -> str | None:
    """Frontmost app name on macOS. Try AppKit (no flicker); no AppleScript fallback."""
    try:
        from AppKit import NSWorkspace  # optional; install pyobjc to enable
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName()) if app else None
    except Exception:
        return None  # no osascript fallback → no flicker


def _mac_has_focus() -> bool:
    app = _front_app_name_macos()
    # Treat common terminals/editors as our "focused" host
    return app in ("Terminal", "iTerm2", "MacVim", "Code", "Visual Studio Code", "PyCharm") if app else True


def _win_has_focus() -> bool:
    sysname = platform.system()
    assert sysname == "Windows"
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        fg = user32.GetForegroundWindow()
        cw = kernel32.GetConsoleWindow()
        if cw:
            return fg == cw
        # Likely Windows Terminal / ConPTY (no console window handle) → assume focused.
        return True
    except Exception:
        return True


async def _watch_chatinfo(client):
    """Push setinfo chat; mac uses AppKit focus + idle, Windows uses HWND focus + idle, Linux uses idle only."""
    last_sent = None
    stable = None
    stable_since = 0.0
    POLL = 1.0
    HOLD = 0.5
    while not client.disconnected and client._alive:
        try:
            sysname = platform.system()
            if sysname == "Darwin":
                focused = _mac_has_focus()
                idle_secs = int(cvar_system.get_value("cl_afk_idle", 25))
            elif sysname == "Windows":
                focused = _win_has_focus()
                idle_secs = 25
            else:
                focused = True  # no OS focus on Linux; rely on idle/typing
                idle_secs = 25

            now = time.monotonic()
            idle = (now - _LAST_ACTIVITY) >= max(5, min(600, idle_secs))
            # Always-on (AFK-only): advertise AFK when unfocused or idle; otherwise off.
            candidate = CIF_AFK if ((not focused) or idle) else 0

            if candidate != stable:
                stable, stable_since = candidate, now
            if stable is not None and (stable != last_sent) and (now - stable_since) >= HOLD:
                last_sent = stable
                await client.send_reliable_raw(_make_cmd_body(f'setinfo "chat" "{last_sent}"\n'))
        except asyncio.CancelledError:
            break
        except Exception:
            pass
        await asyncio.sleep(POLL)


async def _connection_guard(client):
    """
    Gentle watchdog: if RX goes quiet, escalate keepalives
    (unreliable micro-move -> reliable micro-move -> ping).
    Runs alongside the 10 Hz keepalive to avoid ClanRing lag kicks.
    """
    try:
        # If we go this long without any RX after escalation, assume we're dropped.
        HARD_TIMEOUT = 24.0
        while not client.disconnected and client._alive:
            await asyncio.sleep(1.0)
            gap = time.monotonic() - client.last_rx_at
            try:
                if gap >= 14.0:
                    # Last resort: explicit ping (reliable command)
                    await client.send_reliable_raw(_make_cmd_body("ping"))
                elif gap >= 9.0:
                    # Try a reliable tiny move to punch through
                    await client._small_move_reliable()
                elif gap >= 4.0:
                    # Nudge with an unreliable tiny move
                    await client._small_move_unreliable()
                # If we've seen nothing for a long stretch, declare connection lost.
                if gap >= HARD_TIMEOUT:
                    client.disconnected = True
                    client._alive = False
                    try:
                        CON.write("🔌 Connection lost (timeout). Type 'reconnect' or 'connect last'.")
                    except Exception:
                        pass
                    break
            except asyncio.CancelledError:
                break
            except Exception:
                # Stay quiet; keep looping until disconnect
                pass
    except asyncio.CancelledError:
        return


def _parse_cli_args(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    testing = bool(argv and argv[0] == "test")
    if testing:
        argv.pop(0)
    parser = argparse.ArgumentParser(description="PyQuake-M multiplayer console client")
    parser.add_argument("host", nargs="?", default="localhost", help="server hostname or IPv4 address")
    parser.add_argument("port", nargs="?", type=int, default=26000, help="UDP port (default: 26000)")
    parser.add_argument("--demo", action="store_true", help="record to pyquake.dem")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    args.test = testing
    return args


async def _stop_tasks(*tasks):
    tasks = [task for task in tasks if task is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _aioclient(args=None):
    args = _parse_cli_args() if args is None else args
    # ---------- Initialize CVAR system (must happen before config loading) ----------
    global cvar_system, _CONSOLE_SESSION
    cvar_system = CvarSystem()
    # a couple of example/handy client cvars (feel free to add more)
    cvar_system.register(Cvar("cl_rate", "10000", CvarFlags.USERINFO, cvar_type=CvarType.INT,
                              minimum=1000, maximum=25000, description="client rate hint"))

    # Password CVAR for server authentication
    cvar_system.register(Cvar("password", "", CvarFlags.NONE,
                              cvar_type=CvarType.STRING, description="server password"))

    # RCON password CVAR
    cvar_system.register(Cvar("rcon_password", "", CvarFlags.NONE,
                              cvar_type=CvarType.STRING, description="Remote console password for RCON control packets"))

    # Register archived settings before loading config.cfg, including those
    # also initialized lazily for programmatic clients.
    _ensure_developer_cvar()
    _ensure_centerprint_cvar()
    _ensure_timeout_cvars()
    _ensure_cl_idle_cvar()
    _ensure_net_hostport_cvar()

    # Allow alias names to overlap CVAR names? 0=no (default, will rename to *_a on conflict), 1=yes
    if cvar_system.find("cl_aliasoverlap") is None:
        cvar_system.register(Cvar("cl_aliasoverlap", "0",
                                  CvarFlags.ARCHIVE,
                                  cvar_type=CvarType.BOOL,
                                  description="allow alias names to overlap CVAR names"))

    # CVAR: cl_afk_idle (macOS only; others use built-in default)
    if platform.system() == "Darwin" and cvar_system.find("cl_afk_idle") is None:
        # Seconds of inactivity to mark AFK (bounds 5..600)
        cvar_system.register(Cvar("cl_afk_idle", "25", CvarFlags.ARCHIVE,
                                  cvar_type=CvarType.INT, minimum=5, maximum=600))

    # ---------- startup banner + exec config (runs once per app launch) ----------
    print("\n========= PyQuake-M Initialized =========\n")
    try:
        # ensure CVARs exist before exec so loads don't spew "variable not found"
        cv_name = cvar_system.find("name")
        if cv_name is None or cv_name.type is not CvarType.STRING:
            old_name_val = (cv_name.string if cv_name else "pyquake-m")
            try: cvar_system.unregister_variable("name")
            except Exception: pass
            cvar_system.register(Cvar(
                "name", old_name_val,
                CvarFlags.USERINFO | CvarFlags.ARCHIVE,
                callback=_on_name_changed,
                cvar_type=CvarType.STRING
            ))
        if cvar_system.find("topcolor") is None:
            cvar_system.register(Cvar("topcolor", "0",
                CvarFlags.USERINFO | CvarFlags.ARCHIVE, callback=_on_color_changed))
        if cvar_system.find("bottomcolor") is None:
            cvar_system.register(Cvar("bottomcolor", "0",
                CvarFlags.USERINFO | CvarFlags.ARCHIVE, callback=_on_color_changed))

    except Exception:
        pass
    _CONSOLE_SESSION = session = ConsoleSession(sys.modules[__name__])
    session.enable_demo = args.demo
    try:
        if os.path.exists(cvar_system._config_file):
            await cvar_system.load_config()
        if not session.dispatcher.stopped and session.client is None:
            await session.dispatcher.execute(f"connect {quote_arg(f'{args.host}:{args.port}')}", source="startup")
        while not session.dispatcher.stopped:
            _INPUT_ACTIVE.set()
            try:
                line = await asyncio.get_running_loop().run_in_executor(None, session.read_line, "> ")
            except (EOFError, KeyboardInterrupt):
                break
            finally:
                _INPUT_ACTIVE.clear()
                _mark_activity()
            await session.dispatcher.execute(line, source="console")
    finally:
        _INPUT_ACTIVE.clear()
        await session.close()
        _CONSOLE_SESSION = None


def aioclient_main(args=None):
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    # --- Quiet unreliable message skips (safest, fastest) ---
    class _SilenceUnreliableSkips(logging.Filter):
        _pat = re.compile(r"^Skipped -?\d+ unreliable messages$")
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                if record.name not in (aiodgram.__name__, proto.__name__, "netchan"):
                    return True
                msg = record.getMessage()
                return not (isinstance(msg, str) and self._pat.match(msg))
            except Exception:
                return True

    logging.getLogger(aiodgram.__name__).addFilter(_SilenceUnreliableSkips())
    logging.getLogger(proto.__name__).addFilter(_SilenceUnreliableSkips())
    # Optional: turn the whole module down
    logging.getLogger(aiodgram.__name__).setLevel(logging.WARNING)

    # Ensure retired CVARs are purged
    try:
        cvar_system.unregister_variable("cl_chat_notify")
    except Exception:
        pass
    try:
        asyncio.run(_aioclient(args))
    except asyncio.CancelledError:
        pass


async def test_connection(host, port):
    """Simple test to check server connectivity without full spawn process"""
    print(f"🧪 Testing connection to {host}:{port}...")

    try:
        conn = await aiodgram.DatagramConnection.connect(host, port)
        print("✅ Quake protocol connection: OK")

        # Try to send a simple command
        try:
            await conn.send_reliable(b'\x04status\x00')
            print("✅ Command transmission: OK")
        except Exception as e:
            print(f"⚠️  Command transmission: FAILED ({e})")

        # Try to disconnect cleanly
        try:
            if hasattr(conn, 'disconnect'):
                conn.disconnect()
            await conn.wait_until_disconnected()
            print("✅ Disconnection: OK")
        except Exception as e:
            print(f"⚠️  Disconnection: FAILED ({e})")

    except Exception as e:
        print(f"❌ Quake protocol: FAILED ({e})")

def _do_infotest() -> None:
    """Dump a few cached InfoString bits and live state."""
    DBG("infotest invoked")
    c = CURRENT_CLIENT
    if not c or c.disconnected:
        CON.write("ℹ️  Not connected.")
        return

    # If we don't have any pings yet, fire a sweep in the background.
    try:
        if not any(r.get("ping") for r in getattr(c, "_score_state", {}).values()):
            asyncio.create_task(c.refresh_pings_via_ping(timeout=0.8))
            DBG("ping sweep scheduled")
    except Exception:
        pass

    CON.write("== InfoTest ==")
    CON.write(f"ℹ️  alive={c._alive}  disconnected={c.disconnected}  signon_seen={c._signon_seen}")
    CON.write(f"ℹ️  protocol={getattr(c.protocol, 'version', c.protocol)}  pext={c._pext_bits}  saw_cmd_pext={c._saw_cmd_pext}")
    CON.write(f"ℹ️  level={c.level_name!r}  view_entity={c.view_entity}")

    # NEW: show how many serverinfo keys we actually captured
    si = dict(c._serverinfo_is)
    si_sample = sorted(list(si.keys()))[:12]
    CON.write(f"ℹ️  serverinfo keys: {len(si)}  sample: {si_sample or '<none>'}")

    # My userinfo snapshot (prefer setinfo cache, fall back to CVARs)
    my_name   = c.myinfo_get("name") or cvar_system.get_string("name", "pyquake-m")
    my_top    = c.myinfo_get("topcolor") or str(int(cvar_system.get_value("topcolor", 0)))
    my_bottom = c.myinfo_get("bottomcolor") or str(int(cvar_system.get_value("bottomcolor", 0)))
    CON.write(f"ℹ️  me.name={my_name}")
    CON.write(f"ℹ️  me.colors top={my_top} bottom={my_bottom}  {_pants_color_emoji(int(my_bottom) if my_bottom.isdigit() else None)}")

    # Synthesize userinfo from scoreboard for infotest display
    try:
        c._synthesize_userinfo_from_scoreboard()
    except Exception:
        pass

    # Cached other players' userinfo (first few)
    slots = sorted(c._userinfo_is.keys())
    if slots:
        CON.write("-- cached userinfo slots --")
        for idx in slots[:6]:
            ui = c._userinfo_is.get(idx)
            nm  = ui.get_key("name") or "(unnamed)"
            top = ui.get_key("topcolor")
            bot = ui.get_key("bottomcolor")
            emj = _pants_color_emoji(int(bot) if bot and bot.isdigit() else None)
            CON.write(f"#{idx:02d}: {nm}  top {top or '-'} / bottom {bot or '-'} {emj}")
    else:
        CON.write("-- no per-player userinfo cached yet --")

    # Scoreboard sample (first few)
    if getattr(c, "_score_state", None):
        CON.write("-- scoreboard sample --")
        for idx in sorted(c._score_state.keys())[:6]:
            r = c._score_state[idx]
            nm = r.get("name") or r.get("name_full") or "(unnamed)"
            fr = r.get("frags")
            pg = r.get("ping")
            bc = r.get("bottomcolor")
            CON.write(f"#{idx:02d}: {nm}  frags={fr} ping={pg} bottom={bc} {_pants_color_emoji(bc)}")
    else:
        CON.write("-- scoreboard empty --")

if __name__ == "__main__":
    args = _parse_cli_args(sys.argv[1:] or ["--help"])
    if args.test:
        asyncio.run(test_connection(args.host, args.port))
    else:
        aioclient_main(args)
