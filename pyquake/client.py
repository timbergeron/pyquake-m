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
import numpy as np
from threading import Event

import aiodgram
import proto
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

    def _format_float(self, value: float) -> str:
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
                if cvar.flags & CvarFlags.NOTIFY:
                    for fn in self._listeners:
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
        return cvar.set_value_quick(value)

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
            return cvar.reset()

    def reset_all(self):
        with self._lock:
            for cvar in self._cvars.values():
                cvar.reset()

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
        print(f'changing "{cvar.name}" from "{cvar.string}" to "{new_value}"')
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
        """Persist only archived CVARs, in bare form:  <name> "<value>"."""
        if filename is None:
            filename = self._config_file
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                # Alphabetical output -> bottomcolor, name, topcolor, etc.
                for cvar in sorted(self._cvars.values(), key=lambda c: c.name.lower()):
                    if cvar.flags & CvarFlags.ARCHIVE:
                        # Escape backslashes and quotes
                        val = cvar.string.replace("\\", "\\\\").replace('"', '\\"')
                        f.write(f'{cvar.name} "{val}"\n')
            self._last_write_snapshot = {c.name: c.string for c in self._cvars.values()}
            return True
        except Exception as e:
            print(f"Error writing config: {e}")
            return False

    def load_config(self, filename: Optional[str] = None) -> bool:
        """Load config: accepts either `name "v"` or legacy `set/seta name "v"`."""
        if filename is None:
            filename = self._config_file
        if not os.path.exists(filename):
            return False
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith('//'):
                        continue
                    # Use shlex to respect quotes
                    try:
                        parts = shlex.split(line)
                    except Exception:
                        continue
                    if not parts:
                        continue
                    # Forms:
                    #   set name "v"
                    #   seta name "v"
                    #   name "v"
                    if parts[0] in ('set', 'seta'):
                        if len(parts) < 3:
                            continue
                        var_name = parts[1]
                        # Skip (and actively remove) retired variables so they don't get reintroduced.
                        if self._canon(var_name) in self._retired:
                            # If it exists from a previous run, drop it.
                            self.unregister_variable(var_name)
                            continue
                        var_value = ' '.join(parts[2:])
                    else:
                        if len(parts) < 2:
                            continue
                        var_name = parts[0]
                        # Skip (and actively remove) retired variables.
                        if self._canon(var_name) in self._retired:
                            self.unregister_variable(var_name)
                            continue
                        var_value = ' '.join(parts[1:])
                    # shlex already removed quotes, so var_value is raw
                    # Be tolerant: if the CVAR isn't registered yet, quietly create it (archived).
                    cv = self.find(var_name)
                    if cv:
                        self.set_quick(cv, var_value)
                    else:
                        # Prefer correct type for known booleans we expect in configs.
                        ctype = CvarType.BOOL if var_name.lower() in ('con_logcenterprint', 'cmd_warncmd') else CvarType.STRING
                        flags = CvarFlags.ARCHIVE | CvarFlags.USERDEFINED
                        created = self.create(var_name, var_value, flags=flags, cvar_type=ctype)
                        # If create failed (name collides with command), just ignore quietly.
            return True
        except Exception as e:
            print(f"Error loading config: {e}")
            return False

    def _format_float(self, value: float) -> str:
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
        self._stitch_last_at: float = 0.0     # last append time (monotonic)
        self._stitch_timeout: float = 0.35    # seconds; finalize if idle gap exceeds this

    def _finalize_line(self):
        """Emit a newline if a stitched line is open, store it in history, and reset."""
        if not self._line_open:
            return
        # Finish the terminal line
        sys.stdout.write("\n")
        sys.stdout.flush()
        # Remember it as a discrete console line
        self.lines.append(self._pending_line)
        # Reset state
        self._pending_line = ""
        self._line_open = False
        self._stitch_last_at = 0.0

    def newline(self):
        """Public alias: finalize any stitched line."""
        self._finalize_line()

    def append(self, s: str, prefix: str = ""):
        """
        Append a chunk to the current console line without forcing a newline.
        If no line is open, start one (optionally with a prefix, printed once).
        Idle gaps > _stitch_timeout auto-finalize the previous line first.
        """
        if s is None:
            return
        s = str(s)
        now = time.monotonic()
        if self._line_open and now - self._stitch_last_at > self._stitch_timeout:
            # Long gap → assume previous message completed
            self._finalize_line()
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
        self._stitch_last_at = now

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
        self._stitch_last_at = 0.0
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

# ==================== Command buffer (Cbuf_*) with 'wait' ====================
_CBUF: list[str] = []
_CBUF_BUSY = False

def Cbuf_AddText(text: str) -> None:
    """Append raw text (may include newlines/semicolons) to the command buffer."""
    if not text:
        return
    cmds = _split_queued_commands(text)
    _CBUF.extend(cmds)
    _kick_cbuf()

def Cbuf_InsertText(text: str) -> None:
    """Prepend raw text to the command buffer."""
    if not text:
        return
    cmds = _split_queued_commands(text)
    global _CBUF
    _CBUF = cmds + _CBUF
    _kick_cbuf()

def _kick_cbuf() -> None:
    """Start processing if not already running."""
    global _CBUF_BUSY
    if _CBUF_BUSY:
        return
    _CBUF_BUSY = True
    try:
        try:
            # If there's a running loop, schedule the pump task on it.
            loop = asyncio.get_running_loop()
            loop.create_task(_cbuf_pump())
            # ensure the idle reconnect loop is running on the same loop
            try:
                _ensure_idle_reconnect_task()
            except Exception:
                pass
        except RuntimeError:
            # No running loop → run synchronously so we don't get stuck busy.
            asyncio.run(_cbuf_pump())
    except Exception:
        _CBUF_BUSY = False
        raise

async def _wait_next_frame(n: int = 1) -> None:
    """Wait for the next client 'frame' (update). If offline, yield to loop."""
    n = max(1, int(n))
    for _ in range(n):
        if CURRENT_CLIENT and CURRENT_CLIENT._alive and not CURRENT_CLIENT.disconnected:
            try:
                await CURRENT_CLIENT.wait_for_update()
            except Exception:
                await asyncio.sleep(0)
        else:
            await asyncio.sleep(0)

async def _cbuf_pump() -> None:
    """Process commands in the buffer; 'wait' defers the remainder to next frame."""
    global _CBUF_BUSY
    try:
        while _CBUF:
            cmd = (_CBUF.pop(0) or "").strip()
            if not cmd:
                continue
            # handle 'wait' (and 'wait N')
            m = re.match(r'(?i)^wait(?:\s+(\d+))?$', cmd)
            if m:
                n = int(m.group(1) or "1")
                await _wait_next_frame(n)
                continue

            # local commands first
            if _handle_local_command(cmd):
                continue

            # expand aliases then send each; honor 'wait' inside alias bodies, too
            cmds = []
            if CURRENT_CLIENT:
                cmds = CURRENT_CLIENT._expand_alias(cmd) or [cmd]
            else:
                cmds = [cmd]
            for c in cmds:
                m2 = re.match(r'(?i)^wait(?:\s+(\d+))?$', c.strip())
                if m2:
                    await _wait_next_frame(int(m2.group(1) or "1"))
                    continue
                # Try to forward if we have a client and it's not explicitly disconnected.
                if CURRENT_CLIENT and not CURRENT_CLIENT.disconnected:
                    try:
                        await CURRENT_CLIENT.send_command(c)
                        continue
                    except Exception:
                        # fall through to local echo
                        pass
                # Offline (or race during connect): if this looks like plain chat,
                # render a friendly local echo with our name; otherwise keep the
                # old "(offline)" marker so scripts aren't silently dropped.
                if not _is_raw_server_command(c):
                    me = _sanitize_player_name(cvar_system.get_string("name", "player"))
                    CON.write(f"{me}: {c}")
                else:
                    CON.write(f"(offline) {c}")
    finally:
        _CBUF_BUSY = False

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
    # Try both chat forms:
    m  = _CHAT_RE.match(txt)              # "(TEAM) nick: msg" or "nick: msg"
    m2 = _CHAT_TEAM_ALT_RE.match(txt)     # ".(nick): msg" or "(nick): msg"

    # Alt team format: strip any leading '.' and render as "(nick): msg"
    if m2:
        nick = re.sub(r'^[.#]+', '', m2.group("nick"))
        if _is_known_player_name(nick):
            msg  = m2.group("msg")
            CON.newline()
            CON.write(f"({nick}): {msg}")
            return
        # else: fall through and treat as a normal server print (indented)

    # Primary format: render exactly like server does (no special 'You:' label)
    if m:
        team = m.group("t")
        nick = re.sub(r'^[.#]+', '', m.group("nick"))
        if _is_known_player_name(nick):
            msg  = m.group("msg")
            CON.newline()
            if team:
                CON.write(f"({team}) {nick}: {msg}")
            else:
                CON.write(f"{nick}: {msg}")
            return
        # else: not a known player → let it render as a normal server line
    else:
        # Non-chat server prints: stitch chunks into one visible line.
        # Handle any embedded newlines explicitly.
        parts = txt.split("\n")
        for i, seg in enumerate(parts):
            if seg:
                # Hide dev-only chatter unless developer=1
                try:
                    if (not _dev_enabled()) and _is_dev_only_logline(seg):
                        # Skip this segment entirely
                        pass
                    else:
                        # Indent once at the start of a new stitched line
                        CON.append(seg, prefix="  ")
                except Exception:
                    CON.append(seg, prefix="  ")
            if i < len(parts) - 1:
                # Explicit newline inside the payload: finalize the current line
                CON.newline()
        # Heuristic: if this chunk ends a sentence, finalize now so it doesn't
        # wait for the next incoming print to show up as a line.
        if re.search(r"[.!?]\s*$", txt):
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
    """
    Split alias bodies into individual commands by newlines/semicolons.
    This is intentionally simple (not quote-aware), good enough for Quake aliases.
    """
    parts: list[str] = []
    for line in s.splitlines():
        parts.extend([p.strip() for p in line.split(';') if p.strip()])
    return parts

# Built-in client commands that shouldn't be shadowed; used for conflict rename.
_RESERVED_CMDS = frozenset({
    "help","cmdlist","aliaslist","clear","quit","disconnect","connect","reconnect","demo","kill","status",
    "players","showscores","ping","impulse","name","color","say","say_team","tell","dm","pm","like",
    "condump","echo","exec","ip","extip","port","random",
    "reset","resetall","resetcfg","s","st","net_stats","unalias","unaliasall","wait",
    # say_random / say_rand slots (reserve so servers can't shadow)
    "version",
    "say_rand0","say_rand1","say_rand2","say_rand3","say_rand4",
    "say_rand5","say_rand6","say_rand7","say_rand8","say_rand9",
    "say_random0","say_random1","say_random2","say_random3","say_random4",
    "say_random5","say_random6","say_random7","say_random8","say_random9",
    # new local commands (reserve so server can't shadow them)
    "find","apropos","aliasedit",
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
    """
    Server/local aliases captured on CURRENT_CLIENT (exact keys as shown by aliaslist).
    """
    try:
        if CURRENT_CLIENT and getattr(CURRENT_CLIENT, "aliases", None):
            return sorted(list(CURRENT_CLIENT.aliases.keys()), key=str.lower)
    except Exception:
        pass
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

def _set_current_client(c: "AsyncClient|None") -> None:
    global CURRENT_CLIENT
    CURRENT_CLIENT = c
    try:
        _ensure_cl_idle_cvar()
        _ensure_timeout_cvars()
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
    s = _strip_quake_colors(s or "")
    s = re.sub(r"[\x00-\x1F]", "", s).strip()
    if len(s) > 15:
        s = s[:15]
    return s or "pyquake-m"

def _name_key(s: str) -> str:
    """Canonical key for merging/lookup: sanitized 15-char name, case-insensitive."""
    return _sanitize_player_name(s).casefold()

def _is_known_player_name(nick: str) -> bool:
    """Check if a nickname is a known player (or the local player)."""
    if not nick:
        return False

    # Check against local player name
    me_name = _sanitize_player_name(cvar_system.get_string("name", ""))
    if _name_key(nick) == _name_key(me_name):
        return True

    # Check against server-known players (if we have a client)
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

def _decode_bottom_from_colormap(val: int | None) -> int | None:
    """
    Classic player colormap packs colors as (top<<4 | bottom).
    Return bottomcolor (0..15) from colormap's low nibble.
    """
    if val is None:
        return None
    try:
        return int(val) & 15
    except Exception:
        return None

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
                    high_res_inputs=False, cmd_time=None):
    # Use server time if available, else a monotonic fallback
    if cmd_time is None:
        cmd_time = 0.0
    if not high_res_inputs:
        fmt = "<BfBBBhhhBB"
        encode_func = _encode_angle
    else:
        fmt = "<BfHHHhhhBB"
        encode_func = _encode_angle_16

    return struct.pack(fmt,
                       3, float(cmd_time),
                       encode_func(pitch), encode_func(yaw), encode_func(roll),
                       int(forward), int(side), int(up), int(buttons), int(impulse))


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
    print("  (chat)              - Just type and press Enter to chat")
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
    print("  connect H[:P]|last  - Connect to host[:port] (default port 26000) or last")
    print("  disconnect          - Disconnect but keep client running")
    print("  reconnect           - Disconnect, then reconnect to the last server")
    print("  like                - Say \"likes <last chat>\" (mirrors team/public)")
    print("  tell WHO MSG        - Private msg; WHO may be name, wildcard, or #N")
    print("  clear               - Clear the client console")
    print("  condump [file]      - Dump console text to file (default condump.txt)")
    print("  quit                - Disconnect from server")
    print("  demo                - Start demo recording")
    print("  kill                - Suicide")
    print("  status              - Show server status")
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
                cvar_type=CvarType.INT,
                minimum=0, maximum=60,
                description="When >0, auto-reconnect every N minutes while disconnected (clamped 1–60)."
            ))
    except Exception:
        pass

class _IdleReconnectManager:
    """
    Watches cl_idle and connection state; when disconnected and cl_idle>0,
    attempts an immediate 'reconnect' and then retries every N minutes
    (N clamped to 1..60). Messages go through CON and commands via Cbuf_AddText.
    """
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._last_attempt: float = 0.0
        self._saw_dc: bool = False

    def ensure_task(self):
        # start the loop once per process on the active asyncio loop
        if self._task and not self._task.done():
            return
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._loop(), name="idle-reconnect")

    def on_client_changed(self):
        # reset state so a fresh immediate attempt will happen after a dc
        self._last_attempt = 0.0
        self._saw_dc = False

    def reset_timer(self):
        # called when cl_idle changes to re-arm promptly
        self._last_attempt = 0.0

    async def _loop(self):
        while True:
            try:
                _ensure_cl_idle_cvar()
                minutes = int(cvar_system.get_value("cl_idle", 0))
            except Exception:
                minutes = 0

            if minutes <= 0:
                # disabled; idle a bit
                self._saw_dc = False
                self._last_attempt = 0.0
                await asyncio.sleep(5.0)
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
                if self._last_attempt == 0.0:
                    CON.write(f"attempting reconnect... will retry every {clamped} {'minute' if clamped==1 else 'minutes'}")
                    Cbuf_AddText("reconnect\n")
                    self._last_attempt = now
                elif (now - self._last_attempt) >= interval:
                    CON.write(f"idle reconnect triggered after {clamped} {'minute' if clamped==1 else 'minutes'}")
                    Cbuf_AddText("reconnect\n")
                    self._last_attempt = now
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

def _invoke_alias_if_any(raw: str) -> bool:
    """
    If `raw` is exactly the name of a known alias, queue its body into the
    command buffer and return True. Uses same conflict handling as unalias:
    exact match, then '<name>_a' for reserved collisions, then case-insensitive.
    """
    if not CURRENT_CLIENT:
        return False
    name = (raw or "").strip()
    if not name or (" " in name):
        return False  # only bare single tokens invoke aliases, Quake-style
    aliases = getattr(CURRENT_CLIENT, "aliases", None) or {}
    key = None
    if name in aliases:
        key = name
    elif name.lower() in _RESERVED_CMDS and (name + "_a") in aliases:
        key = name + "_a"
    else:
        # case-insensitive fallback
        for k in aliases.keys():
            if k.lower() == name.lower():
                key = k
                break
    if key is None:
        return False
    body = (aliases.get(key) or "").strip()
    if not body:
        return True  # alias exists but empty; still "handled"
    # Queue the alias body so ';' and 'wait' behave correctly
    Cbuf_InsertText(body)
    return True

def _handle_local_command(line: str) -> bool:
    """Return True if we handled it locally (and nothing should be sent to server)."""
    raw = (line or "").strip()
    if not raw:
        DBG("input: <empty>")
        return True
    DBG(f"input: {raw!r}")

    # --- single-token alias invocation (e.g., "last") ---
    # Do this early so aliases behave like in classic engines.
    if CURRENT_CLIENT and _invoke_alias_if_any(raw):
        return True

    low = raw.lower()
    # --- info extensions from server (and for manual testing) ---
    # Accept both forms: fullserverinfo/svi/fui/ui and //fullserverinfo//svi//fui//ui
    if re.match(r'^\s*(//)?(fullserverinfo|svi|fui|ui)\b', raw, flags=re.IGNORECASE):
        if CURRENT_CLIENT and CURRENT_CLIENT._handle_info_stufftext(raw):
            return True

    # --- semi-colon batching: run via command buffer so 'wait' works in one-liners ---
    if (";" in raw) or ("\n" in raw):
        Cbuf_AddText(raw)
        return True

    # --- cmd <args> : forward args to server (strip the "cmd ") ---
    # This makes aliases like  alias last "cmd last"  work reliably.
    m_cmd = re.match(r'(?i)^\s*cmd\s+(.+?)\s*$', raw)
    if m_cmd:
        args = m_cmd.group(1)
        if CURRENT_CLIENT and CURRENT_CLIENT._alive and not CURRENT_CLIENT.disconnected:
            try:
                asyncio.create_task(CURRENT_CLIENT.send_command(args))
            except Exception:
                pass
        else:
            CON.write(f"(offline) {args}")
        return True

    # --- wait [n]: mark to defer remainder (handled by cbuf, but keep for direct calls) ---
    m_wait = re.match(r'(?i)^\s*wait(?:\s+(\d+))?\s*$', raw)
    if m_wait:
        # Put a token in the buffer so sequences like "wait; say hi" still work
        Cbuf_AddText(raw)
        return True

    # --- unalias <name> : delete alias (johnfitz) ---
    m_unalias = re.match(r'(?i)^\s*unalias(?:\s+(\S+))?\s*$', raw)
    if m_unalias:
        name = (m_unalias.group(1) or "").strip()
        if not name:
            CON.write("unalias <name> : delete alias")
            return True
        # operate on the connected client's alias table
        if not CURRENT_CLIENT:
            CON.write(f"No alias named {name}")
            return True
        aliases = getattr(CURRENT_CLIENT, "aliases", None) or {}
        # exact match first
        if name in aliases:
            aliases.pop(name, None)
            return True
        # handle reserved-name remap (server alias stored as "<name>_a")
        if name.lower() in _RESERVED_CMDS:
            alt = f"{name}_a"
            if alt in aliases:
                aliases.pop(alt, None)
                return True
        # final: case-sensitive behavior like strcmp; not found
        CON.write(f"No alias named {name}")
        return True

    # --- unaliasall : delete all aliases (johnfitz) ---
    if re.match(r'(?i)^\s*unaliasall\s*$', raw):
        if CURRENT_CLIENT:
            try:
                CURRENT_CLIENT.aliases.clear()
            except Exception:
                pass
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
            asyncio.create_task(CURRENT_CLIENT.send_command(f"{cmd} {pick}"))
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
            asyncio.create_task(CURRENT_CLIENT.send_command(f"say {msg}"))
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
            asyncio.create_task(CURRENT_CLIENT.send_command(f"say_team {msg}"))
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
                    cv.reset()
            except Exception:
                pass
        return True

    # --- resetcfg: reset only archived cvars (QuakeSpasm) ---
    if re.match(r'(?i)^\s*resetcfg\s*$', raw):
        try:
            for cv in list(getattr(cvar_system, "_cvars", {}).values()):
                if cv.flags & CvarFlags.ARCHIVE:
                    cv.reset()
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
                asyncio.create_task(CURRENT_CLIENT.send_command(f"say randomize: {args_string}"))
                asyncio.create_task(CURRENT_CLIENT.send_command(f"say result: {picked}"))
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

    # --- __cfgmarker: end of config execution sentinel ---
    if low == "__cfgmarker":
        globals()["IN_CFG_EXEC"] = False
        return True

    # --- exec <filename>: execute a script file locally (no server forward) ---
    m_exec = re.match(r'(?i)^\s*exec\s+(.+?)\s*$', raw)
    if m_exec:
        path = m_exec.group(1)
        _exec_script_command(path)
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
        if not CURRENT_CLIENT:
            CON.write("⚠️  Not connected; no aliases loaded.")
            return True
        # Exact match, then conflict-renamed "<name>_a", then case-insensitive
        body = CURRENT_CLIENT.aliases.get(alias_name)
        key_used = alias_name
        if body is None:
            if alias_name + "_a" in CURRENT_CLIENT.aliases:
                key_used = alias_name + "_a"
                body = CURRENT_CLIENT.aliases[key_used]
            else:
                for k, v in CURRENT_CLIENT.aliases.items():
                    if k.lower() == alias_name.lower():
                        key_used, body = k, v
                        break
        if body is None:
            CON.write(f'\nno alias named "{alias_name}" found\n')
            return True
        # Clean trailing newline and escape quotes
        cleaned = body.rstrip("\n")
        cleaned = cleaned.replace('"', '\\"')
        final_cmd = f'alias "{alias_name}" "{cleaned}"'

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

    # Expand captured aliases (server-sent or local) *before* we decide what to forward.
    # Skip when the user is asking about aliases ("alias ...").
    if not low.startswith("alias ") and CURRENT_CLIENT:
        try:
            cmds = CURRENT_CLIENT._expand_alias(raw)
            if cmds:
                # Fire each expanded command; impulse lines will be handled specially in send_command().
                for c in cmds:
                    asyncio.create_task(CURRENT_CLIENT.send_command(c))
                return True
        except Exception:
            pass

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
    if m and CURRENT_CLIENT:
        args = (m.group(1) or "").strip()
        asyncio.create_task(CURRENT_CLIENT.send_rcon_nq(args))
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
            if CURRENT_CLIENT and CURRENT_CLIENT.aliases:
                aliases = sorted([k for k in CURRENT_CLIENT.aliases.keys() if needle in k.casefold()])
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

# -------------------- exec helpers --------------------
def _exec_script_command(path: str) -> None:
    """
    Implements: exec <filename>
      - prints 'execing <file>' when cmd_warncmd=1
      - on failure prints 'couldn't exec <file>' when cmd_warncmd=1
      - when file=='default.cfg' and not found, uses DEFAULT_CFG if set
      - executes commands in-order; unknown lines are sent as string commands (not chat)
      - sets IN_CFG_EXEC during processing; cleared by a trailing __cfgmarker
    """
    # Read file (expand ~, relative paths)
    fn = (path or "").strip()
    warn = bool(cvar_system.get_bool("cmd_warncmd", True))

    data: Optional[str] = None
    try:
        with open(os.path.expanduser(fn), "r", encoding="latin-1") as f:
            data = f.read()
    except Exception:
        # special default.cfg fallback
        if fn == "default.cfg" and isinstance(DEFAULT_CFG, str):
            data = DEFAULT_CFG

    if data is None:
        if warn:
            CON.write(f"couldn't exec {fn}")
        return

    if warn:
        CON.write(f"execing {fn}")

    # Ensure the cvar exists for future toggles (bool, archived)
    try:
        if cvar_system.find("cmd_warncmd") is None:
            cvar_system.register(Cvar("cmd_warncmd", "1", flags=CvarFlags.ARCHIVE, cvar_type=CvarType.BOOL,
                                      description="Warn/echo exec() actions"))
    except Exception:
        pass

    # Mark "in config exec"
    globals()["IN_CFG_EXEC"] = True

    # Insert a newline "just in case there was no trailing \\n." (Quake behavior)
    if data and not data.endswith("\n"):
        data = data + "\n"

    # Split into commands; treat semicolons/newlines as separators (naive like our alias splitter)
    cmds = _split_queued_commands(data)
    for cmd in cmds:
        s = cmd.strip()
        if not s:
            continue
        # Skip pure '//' comment lines
        if s.startswith("//"):
            continue
        # Try local handling first
        if _handle_local_command(s):
            continue
        # From configs, do NOT treat unknown tokens as chat; send as string command to server if connected.
        if CURRENT_CLIENT:
            try:
                asyncio.create_task(CURRENT_CLIENT.send_command(s))
            except Exception:
                pass
        # else: silently ignore unknowns when offline, matching common engine leniency

    # Queue a marker to clear IN_CFG_EXEC after the script batch
    # (execute immediately in our synchronous flow)
    _handle_local_command("__cfgmarker")

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

def _is_raw_server_command(line: str) -> bool:
    """
    Return True if the input should be forwarded as a raw server command
    instead of being treated as chat. (say_team is handled separately.)
    """
    l = line.strip()
    if not l:
        return False
    low = l.lower()
    if low in {"status", "players", "kill", "ping", "cmd"}:
        return True
    if re.match(r"^(impulse|name|color|say)\b", l, flags=re.IGNORECASE):
        return True
    if re.match(r"^(say_team)\b", l, flags=re.IGNORECASE):
        return True
    return False


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
        self.record_pending = self.recording_started = False
        self.recording_complete = True

    def add_message(self, angles, msg, has_server_info):
        if self.record_pending:
            if has_server_info:
                self.record_pending = False
                self.recording_started = True
        elif self.recording_started:
            if has_server_info:
                self.recording_started = False
                self.recording_complete = True
        elif self.recording_complete:
            raise Exception("add_message called when recording is complete")

        if self.recording_started:
            self._msgs.append(msg)
            self._angles.append(angles)

    def _parse_messages(self):
        for msg in self._msgs:
            parsed_msgs = []
            remaining_msg = msg
            while remaining_msg:
                parsed, remaining_msg = proto.ServerMessage.parse_message(
                        remaining_msg)
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
        t = (a - b) % (np.pi * 2.)
        return min(t, 2. * np.pi - t)

    def _get_movement_yaws(self, parsed_msg_lists):
        view_entity = None
        pos = np.zeros((3,))
        time = 0.
        move_time = None
        yaw = 0.

        for parsed_msgs in parsed_msg_lists:
            for parsed_msg in parsed_msgs:
                if parsed_msg.msg_type == proto.ServerMessageType.SETVIEW:
                    view_entity = parsed_msg.viewentity

                if parsed_msg.msg_type == proto.ServerMessageType.SPAWNBASELINE:
                    if view_entity is None:
                        raise ClientError("View entity not set but spawnbaseline received")
                    pos = parsed_msg.origin

                if parsed_msg.msg_type == proto.ServerMessageType.TIME:
                    time = parsed_msg.time

                if (parsed_msg.msg_type == proto.ServerMessageType.UPDATE and parsed_msg.entity_num == view_entity):
                    new_pos = np.array(_patch_vec(pos, parsed_msg.origin))
                    if move_time is not None:
                        dt = time - move_time
                        # Guard zero/negative dt to avoid division errors and noise.
                        if dt and dt > 1e-6:
                            vel = (new_pos - pos) / dt
                            if np.linalg.norm(vel) > 50:
                                yaw = np.arctan2(vel[1], vel[0])
                    move_time = time
                    pos = new_pos

            yield yaw


class AngleCalculatorSmoothed(_BaseAngleCalculator):
    def _angle_cost(self, movement_yaw, yaw):
        diff = self._angle_difference(movement_yaw, yaw)
        if diff < np.pi / 8:
            return 0
        return 0.4

    def calculate(self, parsed_msg_lists):
        paths = [([], 0.)]
        for j, movement_yaw in enumerate(self._get_movement_yaws(parsed_msg_lists)):
            new_paths = []
            for yaw in np.linspace(0., 2. * np.pi, 8, endpoint=False):
                c = self._angle_cost(movement_yaw, yaw)
                new_dists = [int(bool(prev_path) and prev_path[-1] != yaw) + c + prev_dist
                             for prev_path, prev_dist in paths]
                i = np.argmin(new_dists)
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
                yaw = np.pi * round((4 * movement_yaw / np.pi)) / 4
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
        self.angles = (0., 0., 0.)
        self.velocity = (0., 0., 0.)
        self.on_ground = None
        self.view_height = None
        self.models = None
        self.disconnected = False
        # --- Protocol extension tracking (server-advertised) ---
        # FTE/QSS-style pext comes as four 32-bit fields: pext[0..3].
        # REPLACEMENTDELTAS lives in pext[2] (aka "pext2" in many engines).
        self._pext_bits: list[int] = [0, 0, 0, 0]
        self._saw_cmd_pext: bool = False

        self._demos = []
        # Until we see any real inbound server message (serverinfo/signon/time),
        # we aggressively NAT-punch with small UNRELIABLE packets.
        self._signon_seen = False

        # --- Live scoreboard state (no parsing of 'status' needed) ---
        # idx -> {'active', 'name','frags','shirt','pants','ping','pl','t_last'}
        self._score_state: dict[int, dict] = {}
        # name -> latest parsed ping from 'ping' command
        self._latest_ping_by_name: dict[str, int] = {}

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

        self.aliases: dict[str, str] = {}  # server/local aliases live here
        # subscribe cvar updates to push userinfo on change
        self._cvar_subscribed = False

        # --- Centerprint logging control (default: suppressed) ---
        # Register cvar lazily to avoid boot-order issues.
        try:
            if cvar_system.find("con_logcenterprint") is None:
                cvar_system.register(Cvar(
                    "con_logcenterprint",
                    "0",
                    flags=CvarFlags.ARCHIVE,
                    cvar_type=CvarType.BOOL,
                    description="Log centerprints to console (deduped). 0=off, 1=on"
                ))
            # Friendly alias for the common typo ("pint" -> "print") without noise.
            cv = cvar_system.find("con_logcenterprint")
            if cv and cvar_system.find("con_logcenterpint") is None:
                cvar_system.register_alias(cv, "con_logcenterpint")
        except Exception:
            pass
        # Keep last logged centerprint to suppress identical spam bursts.
        self._last_centerprint_logged: str | None = None

        # --- ProQuake team/timer state ---
        self._pq_minutes: int | None = None
        self._pq_seconds: int | None = None
        self._pq_last_time_seen: float | None = None
        self._pq_paused: bool = False
        self._pq_team_frags: dict[int, int] = {}      # team(bottomcolor) -> frags
        self._pq_team_colors: dict[int, int] = {}     # team -> packed colors (16*shirt + team)
        self._pq_teamgame_hint: bool = False          # set true when we see any PQ team msg
        # --- Quake-style info caches (QSS-M Info_GetKey vibe) ---
        self._serverinfo_is: 'InfoString' = InfoString()
        self._myinfo_is: 'InfoString' = InfoString()
        self._userinfo_is: dict[int, 'InfoString'] = {}

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
        """
        Hook point for reacting to per-player userinfo changes (e.g., color/name cache).
        Currently quiet to avoid spam.
        """
        pass

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
                if payload:
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
                    islots = self._userinfo_is.get(slot) or InfoString()
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
        try:
            sent_unreliable = False
            if hasattr(self._conn, "send_unreliable"):
                maybe = self._conn.send_unreliable(payload)
                if inspect.isawaitable(maybe):
                    await maybe
                sent_unreliable = True
            elif hasattr(self._conn, "send"):
                # Some transports name it `send` but still frame it correctly.
                maybe = self._conn.send(payload)
                if inspect.isawaitable(maybe):
                    await maybe
            else:
                await self.send_reliable_raw(payload)  # last-resort, but framed
        finally:
            self.last_tx_at = time.monotonic()
            # Count packet + bytes
            try:
                self._net_stats["packets_sent"] += 1
                self._net_stats["bytes_sent"] += int(len(payload) or 0)
                if sent_unreliable:
                    self._net_stats["unreliable_messages_sent"] += 1
                # If we fell back to reliable, send_reliable_raw will increment reliable counters.
            except Exception:
                pass

    @property
    def player_entity(self):
        return self.entities[self.view_entity]

    def record_demo(self):
        d = Demo()
        self._demos.append(d)
        d.start_recording()
        return d

    def _mono_now(self):
        return time.monotonic() - self._mono0

    def _pq_read_byte_pq(self, b: bytes, i: int) -> tuple[int, int]:
        """ProQuake MSG_ReadBytePQ: next two bytes are high/low nibbles; returns value and new index."""
        if i + 1 >= len(b):
            return (0, i + 2)
        v = b[i] * 16 + b[i + 1] - 272
        return (int(v) & 0xFF, i + 2)

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

    def _handle_proquake_stufftext(self, s: str) -> bool:
        """
        Try to parse a ProQuake payload that rides inside svc_stufftext.
        Heuristic by total length (avoids needing cmd IDs):
          0: b[0] must be 0x01 (MOD_PROQUAKE marker)
          1: cmd (ignored here)
        Lengths:
          2  -> match_reset
          3  -> erase_team: [team]
          4  -> new_team:   [team][shirt]
          5  -> team_frags: [team][frags(PQ-short)]
          6  -> match_time: [minutes(PQ-byte)][seconds(PQ-byte)]
        Returns True if it *was* a PQ message (consumed).
        """
        if not isinstance(s, str) or not s:
            return False
        b = s.encode("latin-1", "ignore")
        if not b or b[0] != 0x01:
            return False

        # Skip 0x01 and cmd byte
        if len(b) < 2:
            return True
        cmd_byte = b[1]
        n = len(b)

        # match_reset (no args)
        if n == 2:
            self._pq_teamgame_hint = True
            self._pq_reset_teams()
            # timer isn't explicitly reset here in PQ; leave as-is
            return True

        # erase_team: [team]
        if n == 3:
            self._pq_teamgame_hint = True
            team = int(b[2]) - 16
            if 0 <= team <= 15:
                self._pq_team_colors.pop(team, None)
                self._pq_team_frags.pop(team, None)
            return True

        # new_team: [team][shirt]
        if n == 4:
            self._pq_teamgame_hint = True
            team = int(b[2]) - 16
            shirt = int(b[3]) - 16
            if 0 <= team <= 15 and 0 <= shirt <= 15:
                self._pq_team_colors[team] = 16 * shirt + team
                self._pq_team_frags.setdefault(team, 0)
            return True

        # team_frags: [team][frags(PQ-short)]
        if n == 5:
            self._pq_teamgame_hint = True
            team = int(b[2]) - 16
            if 0 <= team <= 15:
                frags, _ = self._pq_read_short_pq(b, 3)
                self._pq_team_frags[team] = int(frags)
            return True

        # match_time: [minutes(PQ-byte)][seconds(PQ-byte)]
        if n == 6:
            self._pq_teamgame_hint = True
            i = 2
            minutes, i = self._pq_read_byte_pq(b, i)
            seconds, i = self._pq_read_byte_pq(b, i)
            # '255' means 'not set' in many PQ engines; treat as unset
            if minutes == 255 and seconds == 255:
                self._pq_minutes = None
                self._pq_seconds = None
                self._pq_last_time_seen = None
            else:
                self._pq_minutes = int(minutes)
                self._pq_seconds = int(seconds)
                self._pq_last_time_seen = self.time if self.time is not None else time.monotonic() - self._mono0
                self._pq_paused = False
            return True

        # Unknown PQ payload (still consume so normal stufftext handlers don't choke)
        return True

    def _current_match_time(self) -> tuple[int, int] | None:
        """Return (mm, ss) counting down if we have PQ time, else None."""
        if self._pq_minutes is None or self._pq_seconds is None:
            return None
        mm = int(self._pq_minutes)
        ss = int(self._pq_seconds)
        if self._pq_paused:
            return (max(0, mm), max(0, min(59, ss)))
        # advance based on server TIME (preferred) or monotonic fallback
        if self._pq_last_time_seen is not None:
            now = self.time if self.time is not None else self._mono_now()
            dt = max(0.0, float(now) - float(self._pq_last_time_seen))
            # Tick down
            total = max(0, mm * 60 + ss - int(dt + 0.5))
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
        # Timer (or 0:00)
        t = self._current_match_time()
        if t is None:
            # NEW: when the PQ timer isn't running, use serverinfo matchtime
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
        payload = _make_move_body(
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
        payload = _make_move_body(
            p, y, r, 1, 0, 0, 0, 0,
            self.high_res_inputs,
            self.time if self.time is not None else self._mono_now()
        )
        await self.send_reliable_raw(payload)

    async def _read_messages(self):
        zero_baseline = Entity.make_zero_baseline()

        while True:
            remaining_msg = msg = await self._conn.read_message()
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
            next_entities = {}
            while remaining_msg:
                parsed, remaining_msg = proto.ServerMessage.parse_message(
                    remaining_msg, self.protocol
                )
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
                    if parsed.string.strip() == "cmd pext":
                        DBG("pext negotiation requested by server; replying with zeros")
                    # ProQuake piggy-back: short binary blobs inside STUFFTEXT
                    try:
                        if self._handle_proquake_stufftext(parsed.string):
                            # It was a PQ message — don't treat it like normal text commands
                            continue
                    except Exception:
                        # Never break the rx loop on malformed PQ blobs
                        pass

                    # Handle protocol extension negotiation
                    if parsed.string.strip() == "cmd pext":
                        # Server is negotiating FTE/QSS extensions.
                        self._saw_cmd_pext = True
                        # Full 4-field reply expected by FTE/QSS: pext <bits0> <bits1> <bits2> <bits3>
                        # Use plain decimals (some servers don't accept hex tokens).
                        await self.send_reliable_raw(_make_cmd_body("pext 0 0 0 0\n"))
                    else:
                        # Capture any server-advertised pext bitfields.
                        # Accept lines like:
                        #   "pext a b c d"
                        #   "cmd pext a b c d"
                        # Accept decimal or 0x... hex.
                        try:
                            for _line in parsed.string.splitlines():
                                s = _line.strip()
                                m = re.match(r"^(?:cmd\s+)?pext\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$",
                                             s, flags=re.IGNORECASE)
                                if not m:
                                    continue
                                vals = []
                                for t in m.groups():
                                    try:
                                        vals.append(int(t, 0))
                                    except Exception:
                                        vals.append(0)
                                if len(vals) == 4:
                                    self._pext_bits = vals
                                    DBG_ONCE("pext-captured", f"pext captured {self._pext_bits}")
                        except Exception:
                            pass
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
                            m_alias = re.match(r"^\s*alias\s+(\S+)\s+(.*)$", line, flags=re.IGNORECASE)
                            if not m_alias:
                                continue
                            name = m_alias.group(1)
                            body = _strip_quotes(m_alias.group(2).strip())
                            if not name:
                                continue
                            # Light conflict policy: if it shadows a reserved client command, append _a
                            final_name = name
                            if final_name.lower() in _RESERVED_CMDS:
                                final_name = f"{final_name}_a"
                            self.aliases[final_name] = body
                    except Exception:
                        # Bad alias lines should never kill the RX loop
                        pass

                # Human-visible server text (console lines, chat, kills, etc.)
                if parsed.msg_type == proto.ServerMessageType.PRINT:
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

                    # --- ProQuake textual timer + pause/unpause/over ---
                    if msg.startswith("Match ends in "):
                        # "Match ends in <N> minutes remaining"
                        try:
                            rem = msg[len("Match ends in "):]
                            mins = int(rem.split()[0])
                            self._pq_minutes = mins
                            self._pq_seconds = 0
                            self._pq_last_time_seen = self.time if self.time is not None else self._mono_now()
                            self._pq_paused = False
                            self._pq_teamgame_hint = True
                        except Exception:
                            pass
                    elif msg == "Match paused\n" or msg == "Match paused":
                        self._pq_paused = True
                    elif msg == "Match unpaused\n" or msg == "Match unpaused":
                        # shift timer forward by the paused duration; we don't know it exactly here,
                        # but resuming PQ binary time updates will re-sync quickly
                        self._pq_paused = False
                        if self._pq_last_time_seen is None:
                            self._pq_last_time_seen = self.time if self.time is not None else self._mono_now()
                    elif msg == "The match is over\n" or msg == "The match is over":
                        self._pq_paused = False
                        self._pq_last_time_seen = None
                        # freeze timer at zero
                        self._pq_minutes = 0
                        self._pq_seconds = 0

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
                    self.models = parsed.models
                    has_server_info = True

                    if parsed.protocol != self.protocol:
                        # Friendly banner using real PEXT2 flag when available
                        def _proto_number(p: "proto.Protocol") -> int | None:
                            v = getattr(p, "version", p)
                            try:
                                return int(getattr(v, "value", v))
                            except Exception:
                                return None
                        def _protocol_banner(p: "proto.Protocol") -> str:
                            n = _proto_number(p)
                            if n is None:
                                return "unknown"
                            pext2_mask = getattr(proto, "PEXT2_REPLACEMENTDELTAS", 0)
                            # Prefer a real field on the protocol object if it's ever plumbed:
                            pext2_val = getattr(p, "pext2", None)
                            if pext2_val is None:
                                # Fall back to server-advertised pext[2] captured from STUFFTEXT.
                                pext2_val = self._pext_bits[2]
                            has_rdelta = bool(int(pext2_val) & int(pext2_mask))
                            # Heuristic: if server asked for 'cmd pext' and we're on 666, prefer "fte666".
                            if not has_rdelta and n == 666 and self._saw_cmd_pext:
                                return f"fte{n}"
                            return f"fte{n}" if has_rdelta else f"{n}"
                        CON.write(f"Using protocol {_protocol_banner(parsed.protocol)}")
                    self.protocol = parsed.protocol

                    # New level/serverinfo → clear scoreboard state (prevents ghosts)
                    try:
                        self._score_state.clear()
                    except Exception:
                        pass

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
                            nm_short = _sanitize_player_name(nm_raw)  # classic 15-char display
                            if nm_short:
                                row["active"] = True
                                row["name"] = nm_short
                                # Only upgrade; never downgrade name_full
                                _maybe_upgrade_name_full(row, nm_short, nm_full)
                                # Seed bottomcolor from baseline colormap ONLY if it looks packed (>=16).
                                try:
                                    base = self.baselines.get(int(i) + 1)  # player entities are 1..maxclients
                                    baseline_cm = getattr(base, "colormap", None)
                                    if isinstance(baseline_cm, int) and baseline_cm >= 16 and "bottomcolor" not in row:
                                        bc = baseline_cm & 15
                                        row["bottomcolor"] = bc
                                        _debug_print_color_event("baseline/nameseen",
                                                                 int(i), nm_short,
                                                                 None, None, baseline_cm, bc, _pants_color_emoji(bc))
                                except Exception:
                                    pass
                            else:
                                # Blank/unused slot → mark inactive and drop the name
                                row["active"] = False
                                row.pop("name", None)
                                row.pop("name_full", None)
                            row["t_last"] = time.monotonic()
                except Exception:
                    pass

                try:
                    if hasattr(proto.ServerMessageType, "UPDATEFRAGS") and parsed.msg_type == proto.ServerMessageType.UPDATEFRAGS:
                        i = getattr(parsed, "client", getattr(parsed, "client_num", None))
                        fr = getattr(parsed, "frags", None)
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
                        self._spawned_fut.set_result(None)
                        self._spawned_fut = asyncio.Future()

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

                # Update entity positions
                if parsed.msg_type == proto.ServerMessageType.SPAWNBASELINE:
                    if self.view_entity is None:
                        raise ClientError("View entity not set but spawnbaseline received")
                    ent = Entity.from_baseline(parsed)
                    self.baselines[parsed.entity_num] = ent
                    # If this looks like a player entity, populate bottomcolor from colormap immediately.
                    try:
                        idx = int(parsed.entity_num) - 1
                        row = self._score_state.get(idx)
                        if row is not None:
                            baseline_cm = getattr(ent, "colormap", None)
                            # Only trust baseline colormap if it looks like packed colors (>=16).
                            if isinstance(baseline_cm, int):
                                row["colormap"] = baseline_cm
                                if baseline_cm >= 16:
                                    bc = baseline_cm & 15
                                    row["bottomcolor"] = bc
                                    _debug_print_color_event("baseline", idx, row.get("name"),
                                                             None, None, baseline_cm, bc, _pants_color_emoji(bc))
                                    row["t_last"] = time.monotonic()
                    except Exception:
                        pass
                if parsed.msg_type == proto.ServerMessageType.UPDATE:
                    ent_num = parsed.entity_num
                    baseline = self.baselines.get(ent_num, zero_baseline)
                    next_entities[ent_num] = baseline.update(parsed)
                    # If this UPDATE carries/changes colormap for a player, store it;
                    # only derive bottomcolor if cm looks packed (>=16).
                    try:
                        if 1 <= int(ent_num) <= 64:
                            ent_now = next_entities.get(ent_num)
                            if ent_now is not None:
                                cm = getattr(ent_now, "colormap", None)
                                if cm is not None:
                                    row = self._score_state.get(int(ent_num) - 1)
                                    if row is not None:
                                        row["colormap"] = _safe_int(cm)
                                        if isinstance(cm, int) and cm >= 16:
                                            bc = cm & 15
                                            row["bottomcolor"] = bc
                                            if _COLOR_DEBUG:
                                                _debug_print_color_event(
                                                    "update-colormap",
                                                    int(ent_num) - 1, row.get("name"),
                                                    bc, None, int(cm), row.get("bottomcolor"),
                                                    _pants_color_emoji(row.get("bottomcolor"))
                                                )
                    except Exception:
                        pass


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

                if parsed.msg_type == proto.ServerMessageType.INTERMISSION:
                    self.level_finished = True

                if parsed.msg_type == proto.ServerMessageType.DISCONNECT:
                    self.disconnected = True
                    self._alive = False
                    # Auto-save on server-initiated disconnect
                    try:
                        _autosave_config()
                        try: CON.write("💾 Auto-saved config (disconnect).")
                        except Exception: pass
                    except Exception: pass

            if has_client_update:
                self.entities = next_entities
                self._updated_fut.set_result(None)
                self._updated_fut = asyncio.Future()

            # Synthesize userinfo from scoreboard when real userinfo is missing
            try:
                self._synthesize_userinfo_from_scoreboard()
            except Exception:
                pass

            # Only add messages to demo if demo recording is enabled
            if hasattr(self, '_demos') and self._demos:
                self._demos = [d for d in self._demos if not d.recording_complete]
                for demo in self._demos:
                    if len(msg) > 0:
                        demo.add_message(self.angles, msg, has_server_info)

    async def wait_for_update(self):
        await self._updated_fut

    async def wait_until_spawn(self):
        await self._spawned_fut

    async def wait_for_center_print(self):
        return await self._center_print_fut

    @classmethod
    async def connect(cls, host, port, joequake_version=None):
        """Connect to the given host and port, and start listening for messages.

        At the point this coroutine returns, no messages have yet been read.

        """
        conn = await aiodgram.DatagramConnection.connect(host, port, joequake_version)

        # Always try to send a reliable NOP immediately after connect to flip ackSequence.
        try:
            maybe = conn.send_reliable(b"\x01")
            if inspect.isawaitable(maybe):
                await maybe
        except Exception:
            pass

        client = cls(conn)
        asyncio.create_task(client._read_messages()).add_done_callback(
                lambda fut: fut.result)
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
            _make_move_body(pitch, yaw, roll,
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
        self._cvar_subscribed = True

    async def _push_all_userinfo(self):
        """Send all USERINFO CVARs to the server."""
        try:
            for k, v in cvar_system.userinfo().items():
                await self.send_reliable_raw(_make_cmd_body(f'setinfo "{k}" "{v}"\n'))
        except Exception:
            pass

    async def disconnect(self):
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
            self.joequake_version is not None
            or self.protocol.version != proto.ProtocolVersion.NETQUAKE
        )

    @property
    def joequake_version(self):
        return self._conn.joequake_version

    # ---------------- Alias expansion ----------------
    def _expand_alias(self, line: str, depth: int = 0) -> list[str] | None:
        """
        If `line` begins with a known alias, expand it and return a list of
        concrete commands to send. Supports $0, $1..$9, and $*; recurses with cap.
        """
        if depth >= ALIAS_MAX_DEPTH:
            return None
        try:
            toks = shlex.split(line)
        except Exception:
            toks = line.strip().split()
        if not toks:
            return None
        name, *args = toks
        body = self.aliases.get(name)
        if body is None:
            return None

        # $-parameter substitution
        def _sub_param(m: re.Match) -> str:
            key = m.group(1)
            if key == '*':
                return ' '.join(args)
            if key == '0':
                return name
            if key.isdigit():
                idx = int(key) - 1
                return args[idx] if 0 <= idx < len(args) else ''
            return ''

        expanded = re.sub(r'\$(\*|[0-9])', _sub_param, body)
        # If no parameters used but args exist, append args
        if ('$' not in body) and args:
            expanded = (expanded + ' ' + ' '.join(args)).strip()

        cmds = _split_queued_commands(expanded)
        out: list[str] = []
        for c in cmds:
            nested = self._expand_alias(c, depth + 1)
            if nested:
                out.extend(nested)
            else:
                out.append(c)
        return out

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
        payload = _make_move_body(
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
        name/topcolor/bottomcolor inferred from scoreboard/colormap.
        """
        for idx, row in self._score_state.items():
            if not row.get("active"):
                continue
            if idx in self._userinfo_is:
                continue  # real userinfo already present

            nm = row.get("name") or row.get("name_full")
            bc = row.get("bottomcolor")
            tc = None
            cm = row.get("colormap")
            if isinstance(cm, int) and cm >= 16:
                tc = (cm >> 4) & 15

            isynth = InfoString()
            if nm: isynth["name"] = nm
            if tc is not None: isynth["topcolor"] = str(int(tc) & 15)
            if bc is not None: isynth["bottomcolor"] = str(int(bc) & 15)

            if isynth:
                self._userinfo_is[idx] = isynth

    # -------- showscores helpers: ping sweep & parsing ----------
    def _update_pings_from_lines(self, lines: list[str]) -> None:
        """
        Parse classic 'Client ping times' block and merge by player name.
        Populates both _latest_ping_by_name and per-slot pings when we can match.
        """
        try:
            in_block = False
            found_any = False
            for raw in lines:
                s = _strip_quake_colors(raw).strip()
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
            # 3) last resort: only derive from colormap if it looks packed (>=16)
            cm = r.get("colormap")
            try:
                if isinstance(cm, int) and cm >= 16:
                    return cm & 15
            except Exception:
                pass
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
            # Fallback: packed fields if present
            try:
                cm = _safe_int(client.myinfo_get("colormap", ""))
                if cm is not None and cm >= 16:
                    return cm & 15
            except Exception:
                pass
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


async def _aioclient():
    # Parse command line arguments
    host = "localhost"
    port = 26000
    enable_demo = False

    if len(sys.argv) >= 2:
        if sys.argv[1] == "--demo":
            enable_demo = True
            if len(sys.argv) >= 4:
                host, port = sys.argv[2], int(sys.argv[3])
        else:
            if len(sys.argv) >= 3:
                host, port = sys.argv[1], int(sys.argv[2])

        # ---------- Initialize CVAR system (must happen before config loading) ----------
    global cvar_system
    cvar_system = CvarSystem()
    # a couple of example/handy client cvars (feel free to add more)
    cvar_system.register(Cvar("cl_rate", "10000", CvarFlags.USERINFO, cvar_type=CvarType.INT,
                              minimum=1000, maximum=25000, description="client rate hint"))

    # Password CVAR for server authentication
    cvar_system.register(Cvar("password", "", CvarFlags.USERINFO,
                              cvar_type=CvarType.STRING, description="server password"))

    # RCON password CVAR
    cvar_system.register(Cvar("rcon_password", "", CvarFlags.NONE,
                              cvar_type=CvarType.STRING, description="Remote console password for RCON control packets"))

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

        if cvar_system.load_config("config.cfg"):
            print("execing config.cfg")
    except Exception:
        pass

    # Try some alternative servers if localhost fails
    alternative_servers = [
        ("la.quakeone.com", 26002),
        ("quake.shmack.net", 26000),
        ("quake.crmod.com", 26003),
        ("46.165.204.48", 26000),  # de.quakeone.com
        ("quake.se7en.org", 26000),  # Another known server
        ("quake.fpsclasico.de", 26000),  # German server
    ]

    if host == "localhost" and port == 26000:
        print("💡 localhost:26000 not responding. Trying alternative servers...")
        for alt_host, alt_port in alternative_servers:
            print(f"🔄 Trying {alt_host}:{alt_port}...")
            try:
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(1.0)
                sock.sendto(b'\x00\x00\x00\x00\x00', (alt_host, alt_port))
                sock.close()
                host, port = alt_host, alt_port
                print(f"✅ Found responsive server: {host}:{port}")
                break
            except:
                continue
        else:
            print("❌ No alternative servers found. Using localhost anyway.")

    print(f"🔗 Connecting to {host}:{port}...")

    # Test basic connectivity first
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        sock.sendto(b'\x00\x00\x00\x00\x00', (host, port))
        sock.close()
        # UDP connectivity test passed (silent on success)
    except Exception as e:
        print(f"UDP connectivity test failed: {e}")
        print("   This might indicate network/firewall issues")

    # --- Ensure userinfo CVARs exist before connecting (so we can push them) ---
    try:
        # Register CVARs (migrate 'name' to STRING if previously mis-typed)
        cv_name = cvar_system.find("name")
        if cv_name is None or cv_name.type is not CvarType.STRING:
            old_name_val = (cv_name.string if cv_name else "pyquake-m")
            try:
                # remove wrong-typed var so we can re-register correctly
                cvar_system.unregister_variable("name")
            except Exception:
                pass
            cvar_system.register(Cvar(
                "name",
                old_name_val,
                CvarFlags.USERINFO | CvarFlags.ARCHIVE,
                callback=_on_name_changed,
                cvar_type=CvarType.STRING
            ))
        if cvar_system.find("topcolor") is None:
            cvar_system.register(Cvar("topcolor", "0", CvarFlags.USERINFO | CvarFlags.ARCHIVE,
                                      callback=_on_color_changed))
        if cvar_system.find("bottomcolor") is None:
            cvar_system.register(Cvar("bottomcolor", "0", CvarFlags.USERINFO | CvarFlags.ARCHIVE,
                                      callback=_on_color_changed))
    except NameError:
        # CVAR system not available; continue without CVAR-backed userinfo.
        pass

    try:
        client = await AsyncClient.connect(host, port)
        client._bind_cvar_subscriptions()
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    # Allow callbacks to reach the live connection
    _set_current_client(client)

    # Enable demo recording if requested
    if enable_demo:
        demo = client.record_demo()
        print("Demo recording enabled")
    else:
        demo = None
    # Track current/last targets for 'connect last'
    last_connect_spec = f"{host}:{port}"

    try:
        # Start keepalive (clc_move @10Hz) ONCE; also gentle kickstart once
        hb_task = asyncio.create_task(_simple_keep_alive(client))
        chatinfo_task = asyncio.create_task(_watch_chatinfo(client))
        kick_task = asyncio.create_task(_kickstart_handshake(client))
        guard_task = asyncio.create_task(_connection_guard(client))

        try:
            await asyncio.wait_for(client.wait_until_spawn(), timeout=30.0)
        except asyncio.TimeoutError:
            print("Spawn timeout; continuing anyway. You can still send commands.")

        # (name/color already sent during signon 1)

        # Send one initial tiny move (some servers expect immediate activity)
        try:
            p, y, r = client.angles
        except Exception:
            p = y = r = 0.0
        client.move(p, y, r, 1, 0, 0, 0, 0)  # first move; subsequent keepalive uses framed micro-moves

        # Tasks already running (no duplicates)

        # Interactive command loop
        while True:
            try:
                # Get command from user input; mark console-as-chat while waiting
                loop = asyncio.get_event_loop()
                _INPUT_ACTIVE.set()
                try:
                    cmd = await loop.run_in_executor(None, input, "> ")
                finally:
                    _INPUT_ACTIVE.clear(); _mark_activity()

                line = cmd.strip()
                if _handle_local_command(line):
                    continue

                if line.lower() in ['quit', 'exit', 'q']:
                    break

                elif line.lower() in ('help', 'cmdlist'):
                    _print_cmdlist()
                    continue

                elif line.lower() == 'aliaslist':
                    if client.aliases:
                        print("\nCurrent aliases:")
                        for name, body in sorted(client.aliases.items()):
                            print(f"  {name} = {body}")
                        print("")
                    else:
                        print("No aliases defined.")
                    continue

                elif line.lower() == 'clear':
                    CON.clear()  # Con_Clear_f: wipe buffer + reset backscroll, no chatter
                    continue

                elif re.match(r"^\s*condump(\s+.+)?\s*$", line, flags=re.IGNORECASE):
                    # Syntax: condump [filename]; default is condump.txt
                    try:
                        m = re.match(r"^\s*condump(?:\s+(.+))?\s*$", line, flags=re.IGNORECASE)
                        fn = (m.group(1).strip() if m and m.group(1) else "condump.txt")
                    except Exception:
                        fn = "condump.txt"
                    ok = CON.dump(fn)
                    if not ok:
                        print("condump failed.")
                    # No server interaction; continue input loop
                    continue

                elif line.lower() == 'demo':
                    if demo is None:
                        demo = client.record_demo()
                        print("📹 Demo recording started")
                    else:
                        print("📹 Demo already recording")

                elif line.lower().strip() in ("showscores", "scoreboard"):
                    if client.disconnected or not client._alive:
                        CON.write('🔌 Not connected. Use: connect host[:port]')
                        continue

                    # Build rows keyed by canonical name to avoid duplicates (case/format diffs)
                    rows_by_key: dict[str, tuple[int,int,str,str,str]] = {}
                    # Ensure we have recent ping data
                    if not any(isinstance(r.get("ping"), int) for r in client._score_state.values()):
                        try:
                            await client.refresh_pings_via_ping(timeout=1.0)
                        except Exception:
                            pass

                    # 1) Start with active slots that have a resolved ping
                    for idx in sorted(client._score_state.keys()):
                        r = client._score_state[idx]
                        if not r.get("active"):
                            continue
                        nm = r.get("name")
                        pg = r.get("ping")
                        if not (nm and isinstance(pg, int)):
                            continue
                        fr = int(r.get("frags", 0))
                        st = _status_from_names(nm, r.get("name_full", ""))
                        _merge_score_row(rows_by_key, fr, int(pg), st, nm, _name_key(nm))

                    # 2) Merge in any pings we learned from the silent sweep for names
                    #    that aren't present in _score_state yet (covers "us" edge case).
                    for nm, pg in client._latest_ping_by_name.items():
                        fr = 0
                        st = ""
                        nm_k = _name_key(nm)
                        # if we already know frags/status via a slot with same canonical name, use that
                        for r in client._score_state.values():
                            if r.get("active") and _name_key(r.get("name","")) == nm_k:
                                fr = int(r.get("frags", 0))
                                st = _status_from_names(r.get("name", ""), r.get("name_full", ""))
                                break
                        _merge_score_row(rows_by_key, fr, int(pg), st, nm, nm_k)

                    rows = list(rows_by_key.values())

                    if not rows:
                        STATUS_W = 15  # status tail can use the full 15 chars
                        hdr = f'{"ping":>5}  {"frags":>5}  {"name":<15}  {"status":<{STATUS_W}}'
                        width = len(hdr)
                        print("\n" + _rule_with_title("Scoreboard", width))
                        print("  (no players with ping yet)")
                        print("=" * width + "\n")
                        continue

                    # Sort & render
                    rows.sort(key=lambda t: (t[0], -t[1]), reverse=True)  # frags desc, then lower ping
                    me = _sanitize_player_name(cvar_system.get_string("name", "pyquake-m"))
                    me_k = _name_key(me)
                    STATUS_W = 15
                    hdr = f'{"ping":>5}      {"frags":>5}  {"name":<15}  {"status":<{STATUS_W}}'
                    width = len(hdr)
                    # Title/rule spans the *exact* header width
                    print("\n" + _rule_with_title("Scoreboard", width))
                    print(hdr)
                    for fr, pg, st, nm, nm_k in rows:
                        # Resolve pants color and emoji
                        bottom_val = _bottom_for_name_key(client._score_state, nm_k)
                        # Fallback to our own CVAR color for our row if still unknown
                        if bottom_val is None:
                            bottom_val = _maybe_self_bottom(client, nm_k)
                        pants_emoji = _pants_color_emoji(bottom_val)
                        # Put the arrow right after the name (inside the name column),
                        # and leave TWO spaces between emoji and name.
                        name_disp = nm + (" ◀" if nm_k == me_k else "")
                        print(
                            f"{pg:>5}   {fr:>5} {pants_emoji}  "
                            f"{name_disp:<15}  {st[:STATUS_W]:<{STATUS_W}}"
                        )
                    # Add ProQuake timer/team footer
                    CON.write("")  # blank line before the footer
                    try:
                        line = client.format_showscores_footer(width)
                        if line:
                            CON.write(line)
                    except Exception:
                        pass
                    print("=" * width + "\n")
                    continue

                elif line:
                    # Optional leading slash support; not required anymore
                    raw = line.lstrip()
                    if raw.startswith('/'):
                        raw = raw[1:].lstrip()

                    # -------------- CVAR commands --------------
                    # set/seta/unset/toggle
                    m_cvarcmd = re.match(r"^\s*(seta?|unset|toggle|cvarlist|writeconfig|execconfig)\b(.*)$", raw, flags=re.IGNORECASE)
                    if m_cvarcmd:
                        cmdw = m_cvarcmd.group(1).lower()
                        rest = (m_cvarcmd.group(2) or "").strip()
                        # cvarlist [pattern]
                        if cmdw == "cvarlist":
                            pat = rest
                            rows = cvar_system.list_cvars(pat)
                            if not rows:
                                print("(no cvars)")
                            else:
                                for mark, nm, val in rows:
                                    print(f"{mark} {nm} : {val}")
                            continue
                        # writeconfig [file]
                        if cmdw == "writeconfig":
                            fn = rest if rest else "config.cfg"
                            ok = cvar_system.write_variables(fn)
                            print(f"wrote {fn}" if ok else f"failed writing {fn}")
                            continue
                        # execconfig [file]
                        if cmdw == "execconfig":
                            fn = rest if rest else "config.cfg"
                            ok = cvar_system.load_config(fn)
                            print(f"execed {fn}" if ok else f"failed exec {fn}")
                            continue
                        # parse: <var> [value]
                        try:
                            parts = shlex.split(rest)
                        except Exception:
                            parts = rest.split()
                        if not parts:
                            print("usage: set[a]/unset/toggle <var> [value]")
                            continue
                        var, *val_parts = parts
                        if cmdw in ("set","seta"):
                            val = " ".join(val_parts) if val_parts else ""
                            cv = cvar_system.find(var)
                            if not cv:
                                flags = CvarFlags.USERDEFINED
                                if cmdw == "seta":
                                    flags |= CvarFlags.ARCHIVE | CvarFlags.SETA
                                # default to STRING for user-defined vars
                                cv = cvar_system.create(var, val, flags=flags, cvar_type=CvarType.STRING)
                                if not cv:
                                    print(f"couldn't create {var}")
                                    continue
                            else:
                                if cmdw == "seta":
                                    cv.flags |= (CvarFlags.ARCHIVE | CvarFlags.SETA)
                            cvar_system.set(cv.name, val)
                            continue
                        if cmdw == "unset":
                            cv = cvar_system.find(var)
                            if not cv:
                                print(f"{var}: not found")
                            else:
                                cvar_system.reset(cv.name)
                            continue
                        if cmdw == "toggle":
                            cv = cvar_system.find(var)
                            if not cv:
                                # create boolean if absent
                                cv = cvar_system.create(var, "0", flags=CvarFlags.USERDEFINED, cvar_type=CvarType.BOOL)
                            newv = "0" if (cv.as_bool()) else "1"
                            cvar_system.set(cv.name, newv)
                            continue

                    # -------- name / color commands -> go through CVARs --------
                    # `name` with/without argument
                    if re.match(r"^\s*name\s*$", raw, flags=re.IGNORECASE):
                        try:
                            cvar_system.command(["name"])  # prints current value
                        except Exception:
                            # fallback: print from current state
                            print(f'"name" is "{_sanitize_player_name(cvar_system.get_string("name","pyquake-m"))}"')
                        continue
                    m_setname = re.match(r'^\s*name\s+(.+?)\s*$', raw, flags=re.IGNORECASE)
                    if m_setname:
                        newn = _strip_quotes(m_setname.group(1).strip())
                        try:
                            cvar_system.set("name", newn)
                        except Exception:
                            # If CVARs missing, fall back to raw command
                            await client.send_command(f'name "{_sanitize_player_name(newn)}"')
                        continue

                    # `color` with/without args
                    if re.match(r"^\s*color\s*$", raw, flags=re.IGNORECASE):
                        try:
                            top = int(cvar_system.get_value("topcolor", 0))
                            bot = int(cvar_system.get_value("bottomcolor", 0))
                        except Exception:
                            top = bot = 0
                        print(f'"color" is "{top} {bot}"')
                        continue
                    m_setcolor = re.match(r"^\s*color\s+(\d+)\s+(\d+)\s*$", raw, flags=re.IGNORECASE)
                    if m_setcolor:
                        t = _clamp_color(int(m_setcolor.group(1)))
                        b = _clamp_color(int(m_setcolor.group(2)))
                        try:
                            cvar_system.set("topcolor", str(t))
                            cvar_system.set("bottomcolor", str(b))
                        except Exception:
                            await client.send_command(f"color {t} {b}")
                        continue

                    # If the line starts with a known CVAR name, treat as CVAR command:
                    try:
                        toks = shlex.split(raw)
                    except Exception:
                        toks = raw.split()
                    if toks:
                        maybe_cvar = cvar_system.find(toks[0])
                        if maybe_cvar:
                            if not cvar_system.command(toks):
                                print("cvar command error")
                            continue

                    # -------- like (say/say_team "likes <last chat>") --------
                    if raw.lower().strip() == "like":
                        if client.disconnected or not client._alive:
                            CON.write('🔌 Not connected. Use: connect host[:port]')
                            continue
                        now = time.monotonic()
                        if (now - client._last_like_at) < 1.0:
                            # simple anti-spam (silent, like engine)
                            continue
                        # nothing to like?
                        if not client.last_chat_msg:
                            CON.write("nothing to like")
                            client._last_like_at = now
                            continue
                        # avoid liking a like
                        if re.search(r"\blikes\b", client.last_chat_msg, flags=re.IGNORECASE):
                            client._last_like_at = now
                            continue
                        # sanitize and send
                        msg_txt = client.last_chat_msg.replace("\r", " ").replace("\n", " ").strip()
                        if not msg_txt:
                            client._last_like_at = now
                            continue
                        cmd_out = ("say_team " if client.last_chat_is_team else "say ") + f"likes {msg_txt}"
                        await client.send_command(cmd_out)
                        CON.write(f"👍 Liked: {msg_txt}")
                        client._last_like_at = now
                        continue

                    # -------- tell / dm / pm (private message) --------
                    # Syntax: tell WHO MSG
                    #  - WHO may be a name (supports server-side wildcards), or '#N' / 'N' for scoreboard index
                    #  - We forward as: tell <WHO> <MSG>
                    m_tell = re.match(
                        r"^\s*(tell|dm|pm)\s+(\"[^\"]+\"|'[^']+'|\S+)\s+(.+)$",
                        raw, flags=re.IGNORECASE
                    )
                    if m_tell:
                        if client.disconnected or not client._alive:
                            CON.write('🔌 Not connected. Use: connect host[:port]')
                            continue
                        who = m_tell.group(2)
                        msg = m_tell.group(3).strip()
                        who = _strip_quotes(who)
                        # Support numeric indices: "7" -> "#7", keep "#7" as-is
                        if re.fullmatch(r"#?\d+", who):
                            try:
                                idx = int(who.lstrip("#"))
                                who = f"#{idx}"
                            except Exception:
                                pass
                        # Guard against empty message after trimming
                        if not msg:
                            CON.write("Usage: tell WHO MESSAGE")
                            continue
                        # Send to server
                        await client.send_command(f"tell {who} {msg}")
                        # Local echo
                        # Avoid leaking IPs or control chars; msg is user-provided
                        echo_who = who if len(who) < 64 else who[:61] + "..."
                        echo_msg = msg if len(msg) < 400 else msg[:397] + "..."
                        CON.write(f"📩 You → {echo_who}: {echo_msg}")
                        continue

                    # -------- disconnect (keep app running) --------
                    if raw.lower() == "disconnect":
                        if 'hb_task' in locals() and hb_task:
                            hb_task.cancel()
                            try: await hb_task
                            except: pass
                            hb_task = None
                        if 'kick_task' in locals() and kick_task:
                            kick_task.cancel()
                            try: await kick_task
                            except: pass
                            kick_task = None
                        if 'guard_task' in locals() and guard_task:
                            guard_task.cancel()
                            try: await guard_task
                            except: pass
                            guard_task = None
                        # Auto-save before tearing down the connection
                        try:
                            # Always autosave, but only *announce* it when developer=1
                            # (keep the config write quiet so we fully control the message here)
                            _autosave_config(silent=True)
                            if _dev_enabled():
                                try: CON.write("💾 Auto-saved config (manual disconnect).")
                                except Exception: pass
                        except Exception: pass
                        _set_current_client(None)
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        print("🔌 Disconnected. Use: connect host[:port]  (or  connect last)")
                        continue

                    # -------- reconnect (reuse last_connect_spec) --------
                    if raw.lower().strip() == "reconnect":
                        # Need something to reconnect *to*
                        target = _parse_hostport_spec(last_connect_spec) if 'last_connect_spec' in locals() and last_connect_spec else None
                        if not target:
                            print("ℹ️  No previous server recorded. Use: connect host[:port]")
                            continue
                        new_host, new_port = target
                        # Tear down existing connection (if any)
                        if 'hb_task' in locals() and hb_task:
                            hb_task.cancel()
                            try:
                                await hb_task
                            except:
                                pass
                            hb_task = None
                        if 'kick_task' in locals() and kick_task:
                            kick_task.cancel()
                            try:
                                await kick_task
                            except:
                                pass
                            kick_task = None
                        if 'guard_task' in locals() and guard_task:
                            guard_task.cancel()
                            try:
                                await guard_task
                            except:
                                pass
                            guard_task = None
                        if client and not client.disconnected:
                            # Auto-save before reconnect teardown (counts as a disconnect)
                            try:
                                _autosave_config()
                                try: CON.write("💾 Auto-saved config (reconnect).")
                                except Exception: pass
                            except Exception: pass
                            try:
                                await client.disconnect()
                            except Exception:
                                pass
                        # Reconnect to last target
                        print(f"🔁 Reconnecting to {new_host}:{new_port}...")
                        try:
                            client = await AsyncClient.connect(new_host, new_port)
                            # restart background tasks
                            hb_task = asyncio.create_task(_simple_keep_alive(client))
                            chatinfo_task = asyncio.create_task(_watch_chatinfo(client))
                            kick_task = asyncio.create_task(_kickstart_handshake(client))
                            # keep last_connect_spec unchanged (still points to this target)
                            try:
                                await asyncio.wait_for(client.wait_until_spawn(), timeout=20.0)
                                print("✅ Reconnected & spawned.")
                            except asyncio.TimeoutError:
                                print("✅ Reconnected (spawn pending).")
                        except Exception as e:
                            print(f"❌ Reconnect failed: {e}")
                        continue

                    # -------- connect host[:port] / connect last --------
                    if raw.lower().startswith("connect"):
                        parts = raw.split(maxsplit=1)
                        if len(parts) == 1 or not parts[1].strip():
                            print("Usage: connect host[:port]   or   connect last")
                            continue
                        spec = parts[1].strip()
                        if spec.lower() == "last":
                            target = _parse_hostport_spec(last_connect_spec) if last_connect_spec else None
                        else:
                            target = _parse_hostport_spec(spec)
                        if not target:
                            print("❌ address is not a valid ip/domain or port")
                            continue
                        new_host, new_port = target
                        # Clean up any existing connection
                        if client and not client.disconnected:
                            if 'hb_task' in locals() and hb_task: hb_task.cancel();
                            if 'kick_task' in locals() and kick_task: kick_task.cancel();
                            if 'guard_task' in locals() and guard_task: guard_task.cancel();
                            try: await client.disconnect()
                            except Exception: pass
                        print(f"🔗 Connecting to {new_host}:{new_port}...")
                        try:
                            client = await AsyncClient.connect(new_host, new_port)
                            # restart background tasks
                            hb_task = asyncio.create_task(_simple_keep_alive(client))
                            chatinfo_task = asyncio.create_task(_watch_chatinfo(client))
                            kick_task = asyncio.create_task(_kickstart_handshake(client))
                            guard_task = asyncio.create_task(_connection_guard(client))
                            last_connect_spec = f"{new_host}:{new_port}"
                            try:
                                await asyncio.wait_for(client.wait_until_spawn(), timeout=20.0)
                                print("✅ Connected & spawned.")
                            except asyncio.TimeoutError:
                                print("✅ Connected (spawn pending).")
                        except Exception as e:
                            print(f"❌ Connect failed: {e}")
                        continue

                    # ---- Cmd_ForwardToServer-style handling for 'cmd ...' ----
                    m_cmd = re.match(r"^\s*cmd\b(.*)$", raw, flags=re.IGNORECASE)
                    if m_cmd:
                        args = (m_cmd.group(1) or "").strip()
                        if client.disconnected or not client._alive:
                            CON.write('Can\'t "cmd", not connected')
                            continue

                        # Block changing setinfo observer (mirrors the C guard)
                        if re.match(r"^\s*setinfo\s+observer\b", args, flags=re.IGNORECASE):
                            CON.write("changing observer key is not allowed")
                            continue

                        # Special replies for capability queries (sent WITHOUT 'cmd ' prefix)
                        if args.lower() == "protocols":
                            # Order by preference; include common extended + classic NQ
                            proto_line = "protocols 999 666 10002 15"
                            await client.send_command(proto_line)
                            continue
                        if args.lower().startswith("pext"):
                            await client.send_command("pext 0 0 0 0")
                            continue

                        # Default: forward just the args (no 'cmd ' prefix)
                        if args:
                            await client.send_command(args)
                        continue

                    # Local 'alias' command (teach the client):
                    #   alias                   -> list all with count
                    #   alias <name>            -> show exact; else substring search in names/values (case-insensitive)
                    #   alias <name> <value...> -> set/replace (rename to <name>_a on command conflict; or CVAR conflict if cl_aliasoverlap=0)
                    m_local_alias = re.match(r"^\s*alias\b(.*)$", raw, flags=re.IGNORECASE)
                    if m_local_alias:
                        rest = m_local_alias.group(1).strip()

                        # 1) No args → list all
                        if not rest:
                            count = 0
                            for n, b in sorted(client.aliases.items(), key=lambda kv: kv[0].lower()):
                                print(f"   {n}: {b}")
                                count += 1
                            if count:
                                print(f"{count} alias command(s)")
                            else:
                                print("no alias commands found")
                            continue

                        # Parse tokens (name [value...])
                        try:
                            parts = shlex.split(rest)
                        except Exception:
                            parts = rest.split()

                        # 2) One token → exact show, else substring search
                        if len(parts) == 1:
                            key = parts[0]
                            val = client.aliases.get(key)
                            if val is not None:
                                print(f"   {key}: {val}")
                                continue
                            # substring search across names and values (case-insensitive)
                            q = key.lower()
                            matches = []
                            for n, b in client.aliases.items():
                                if q in n.lower() or q in b.lower():
                                    matches.append((n, b))
                            if matches:
                                for n, b in sorted(matches, key=lambda kv: kv[0].lower()):
                                    print(f"   {n}: {b}")
                                print(f"\nexact alias not found. however, {len(matches)} instance(s) of the search string were found")
                            else:
                                print("\nno alias or instances of the search string found")
                            continue

                        # 3) name + value... → set/replace with conflict handling
                        name, body = parts[0], " ".join(parts[1:])
                        if not name:
                            print("Alias name is too long or empty")
                            continue

                        # Conflict policy:
                        #  - commands ALWAYS take precedence → rename to *_a
                        #  - cvars: rename to *_a unless cl_aliasoverlap==1
                        want_overlap = bool(cvar_system.get_bool("cl_aliasoverlap", False))
                        cmd_conflict = (name.lower() in _RESERVED_CMDS)
                        cvar_conflict = (not want_overlap) and (cvar_system.find(name) is not None)
                        final_name = name
                        if cmd_conflict or cvar_conflict:
                            final_name = f"{name}_a"
                            print(f"alias {name} renamed to {final_name} due to {'command' if cmd_conflict else 'cvar'} conflict")

                        # Reuse existing alias object if present (by final_name)
                        existed = final_name in client.aliases
                        client.aliases[final_name] = (body + ("\n" if not body.endswith("\n") else ""))
                        if existed:
                            print(f"updated alias {final_name}")
                        else:
                            print(f"alias {name!r} set ({final_name!r})")
                        continue

                    # Try alias expansion first: use loaded (server/local) aliases.
                    if client and not client.disconnected and client._alive:
                        expanded = client._expand_alias(raw)
                    else:
                        expanded = None
                        # If user tries to issue server commands while disconnected, nudge them.
                        if _is_raw_server_command(raw):
                            print("🔌 Not connected. Use: connect host[:port]")
                            continue
                    if expanded:
                        for cmd_out in expanded:
                            await client.send_command(cmd_out)
                        continue

                    # If it looks like a raw server command, send it verbatim
                    if _is_raw_server_command(raw):
                        # Special-case say: keep the nicer "You:" echo
                        if re.match(r"^say\s+(.+)$", raw, flags=re.IGNORECASE):
                            await client.send_command(raw)
                            try:
                                said = re.sub(r"^say\s+", "", raw, flags=re.IGNORECASE)
                            except Exception:
                                said = raw
                            #CON.write(f"💬 You: {said}")
                        elif re.match(r"^say_team\s+(.+)$", raw, flags=re.IGNORECASE):
                            await client.send_command(raw)
                            try:
                                said = re.sub(r"^say_team\s+", "", raw, flags=re.IGNORECASE)
                            except Exception:
                                said = raw
                            #CON.write(f"👪 TEAM • You: {said}")
                        else:
                            await client.send_command(raw)
                    else:
                        # Only send to server when actually connected.
                        if CURRENT_CLIENT and CURRENT_CLIENT._alive and not CURRENT_CLIENT.disconnected:
                            # If the line looks like a real server command, forward it verbatim;
                            # otherwise treat it as chat.
                            if _is_raw_server_command(line):
                                await client.send_command(line)
                            else:
                                await client.send_command(f"say {line}")
                        else:
                            # Disconnected: never try to send; just echo locally so the console
                            # doesn't break on unknown input while offline.
                            if line.strip():
                                CON.write(f"(offline) {line}")
                            # (nothing else to do in offline mode)

            except KeyboardInterrupt:
                print("\nDisconnecting...")
                break
            except Exception as e:
                print(f"Error: {e}")
                continue

    finally:
        if 'hb_task' in locals() and hb_task:
            hb_task.cancel()
            try:
                await hb_task
            except:
                pass
        if 'kick_task' in locals() and kick_task:
            kick_task.cancel()
            try:
                await kick_task
            except:
                pass
        if 'guard_task' in locals() and guard_task:
            guard_task.cancel()
            try:
                await guard_task
            except:
                pass
        if 'chatinfo_task' in locals() and chatinfo_task:
            chatinfo_task.cancel()
            try: await chatinfo_task
            except: pass
        _INPUT_ACTIVE.clear()
        _set_current_client(None)
        # Always auto-save config on clean shutdown (if CVAR system present)
        try:
            _autosave_config(silent=True)  # Silent save on quit
        except Exception:
            pass
        try:
            await client.disconnect()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        if demo is not None:
            demo.stop_recording()
            with open('pyquake.dem', 'wb') as f:
                demo.dump(f)
            print("Demo saved to pyquake.dem")
        # else: demo recording disabled - silent cleanup


def aioclient_main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    # --- Quiet unreliable message skips (safest, fastest) ---
    class _SilenceUnreliableSkips(logging.Filter):
        _pat = re.compile(r"^Skipped -?\d+ unreliable messages$")
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                if record.name not in ("aiodgram", "proto", "netchan"):
                    return True
                msg = record.getMessage()
                return not (isinstance(msg, str) and self._pat.match(msg))
            except Exception:
                return True

    logging.getLogger("aiodgram").addFilter(_SilenceUnreliableSkips())
    logging.getLogger("proto").addFilter(_SilenceUnreliableSkips())
    # Optional: turn the whole module down
    logging.getLogger("aiodgram").setLevel(logging.WARNING)

    # Ensure retired CVARs are purged
    try:
        cvar_system.unregister_variable("cl_chat_notify")
    except Exception:
        pass
    try:
        asyncio.run(_aioclient())
    except asyncio.CancelledError:
        pass


async def test_connection(host, port):
    """Simple test to check server connectivity without full spawn process"""
    print(f"🧪 Testing connection to {host}:{port}...")

    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        sock.sendto(b'\x00\x00\x00\x00\x00', (host, port))
        sock.close()
        print("✅ UDP connectivity: OK")
    except Exception as e:
        print(f"❌ UDP connectivity: FAILED ({e})")
        return

    try:
        from aiodgram import DatagramConnection
        conn = await DatagramConnection.connect(host, port)
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
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Run simple connectivity test
        host = sys.argv[2] if len(sys.argv) > 2 else "la.quakeone.com"
        port = int(sys.argv[3]) if len(sys.argv) > 3 else 26002
        asyncio.run(test_connection(host, port))
    elif len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] in ["-h", "--help"]):
        print("PyQuake Client")
        print("Usage:")
        print("  python client.py [host] [port]     - Connect to server (no demo)")
        print("  python client.py --demo [host] [port] - Connect with demo recording")
        print("  python client.py test [host] [port]   - Test connectivity only")
        print("  python client.py --help              - Show this help")
        print("")
        print("Examples:")
        print("  python client.py la.quakeone.com 26002")
        print("  python client.py --demo localhost 26000")
    else:
        aioclient_main()