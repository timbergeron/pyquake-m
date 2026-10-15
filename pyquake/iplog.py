"""Local ProQuake identity history and incremental parsing of status replies."""

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import os
import re
import struct
import tempfile
import time


_RECORD = struct.Struct("<I16s")
_ADDRESS = re.compile(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:\.(\d{1,3}|xxx|\*))?(?::(\d{1,5}))?",
                      re.IGNORECASE)


def address_prefix(text: str) -> int | None:
    """Accept IPv4 addresses, masked addresses, or three-octet prefixes."""
    match = _ADDRESS.fullmatch(text.strip())
    if not match:
        return None
    a, b, c = (int(value) for value in match.groups()[:3])
    last, port = match.groups()[3:]
    if max(a, b, c) > 255 or (last and last.isdigit() and int(last) > 255):
        return None
    if port is not None and not 1 <= int(port) <= 65535:
        return None
    return (a << 16) | (b << 8) | c


def format_address(address: int) -> str:
    return f"{address >> 16}.{(address >> 8) & 255}.{address & 255}.xxx"


class IpLog:
    """Bounded, deduplicated history using QSS-M's 20-byte iplog.dat records."""

    def __init__(self, path=None, *, limit=65536, per_address=128):
        if limit < 1 or per_address < 1:
            raise ValueError("IP history limits must be positive")
        self.path = Path(path) if path is not None else None
        self.limit = limit
        self.per_address = per_address
        self._entries = OrderedDict()
        self._aliases = {}
        self.dirty = False

    def add(self, address: int, name: str) -> bool:
        if not 0 <= address <= 0xffffff:
            raise ValueError("IP log address must contain three IPv4 octets")
        name = name.encode("latin-1", "replace").split(b"\0", 1)[0][:15].decode("latin-1").rstrip()
        if not name or (address, name) in self._entries:
            return False
        aliases = self._aliases.get(address, [])
        if len(aliases) >= self.per_address:
            self._remove(address, aliases[0])
        if len(self._entries) >= self.limit:
            self._remove(*next(iter(self._entries)))
        self._entries[address, name] = None
        self._aliases.setdefault(address, []).append(name)
        self.dirty = True
        return True

    def _remove(self, address, name):
        del self._entries[address, name]
        aliases = self._aliases[address]
        aliases.remove(name)
        if not aliases:
            del self._aliases[address]

    def aliases(self, address: int) -> list[str]:
        return list(self._aliases.get(address, ()))

    def entries(self):
        return list(self._entries)

    def names(self) -> list[str]:
        return sorted({name for _, name in self._entries}, key=lambda name: (name.casefold(), name))

    def merge(self, path) -> int:
        # Parse into a separate bounded store first, so a malformed file cannot
        # partly change the live history. Latin-1 preserves Quake font bytes.
        imported = IpLog(limit=self.limit, per_address=self.per_address)
        with Path(path).open("rb") as source:
            while record := source.read(_RECORD.size):
                if len(record) != _RECORD.size:
                    raise ValueError("truncated iplog.dat record (expected 20 bytes)")
                address, raw_name = _RECORD.unpack(record)
                imported.add(address, raw_name.split(b"\0", 1)[0].decode("latin-1"))
        return sum(self.add(address, name) for address, name in imported.entries())

    def load(self) -> None:
        if self.path is not None and self.path.exists():
            self.merge(self.path)
        self.dirty = False

    def save(self) -> None:
        if self.path is None or not self.dirty:
            return
        combined = IpLog(limit=self.limit, per_address=self.per_address)
        if self.path.exists():
            combined.merge(self.path)
        # Apply this session's observations last so old disk entries cannot
        # displace newly observed aliases when the history reaches its limit.
        for address, name in self._entries:
            combined.add(address, name)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".iplog-", delete=False) as output:
                temporary = Path(output.name)
                for address, name in combined._entries:
                    output.write(_RECORD.pack(address, name.encode("latin-1")))
            os.replace(temporary, self.path)
            self._entries, self._aliases = combined._entries, combined._aliases
            self.dirty = False
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def dump(self, path, printable) -> None:
        with Path(path).open("w", encoding="utf-8") as output:
            for address, name in sorted(self._entries):
                output.write(f"{format_address(address):<16}  {printable(name)}\n")


@dataclass(frozen=True)
class StatusPlayer:
    slot: int  # Zero based, like svc_updatename.
    name: str
    address: int | None


class StatusParser:
    """Parse vanilla/QSS-M status blocks across arbitrary svc_print chunks."""

    _ROW = re.compile(r"\s*#?(\d{1,4})\s+(.+?)\s+-?\d+\s+\d+:\d{2}(?::\d{2})?\s*")

    def __init__(self):
        self.reset()

    def reset(self):
        self._buffer = ""
        self._header = False
        self._remaining = 0
        self._pending = None
        self._players = {}
        self._deadline = 0

    def feed(self, text: str, *, now=None) -> list[dict[int, StatusPlayer]]:
        now = time.monotonic() if now is None else now
        if self._deadline and now > self._deadline:
            self.reset()
        self._buffer += text
        completed = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if re.match(r"^host:\s", line):
                self._header = True
                self._remaining = 0
                self._pending = None
                self._players = {}
                self._deadline = now + 5
                continue
            if self._header:
                match = re.match(r"^players:\s+(\d{1,4})\s+active\b", line)
                if match:
                    self._remaining = int(match.group(1))
                    self._header = False
                    if not 0 <= self._remaining <= 4096:
                        self._remaining = 0
                        self._deadline = 0
                    elif self._remaining == 0:
                        completed.append({})
                        self._deadline = 0
                continue
            if not self._remaining or not line.strip():
                continue
            if self._pending is None:
                match = self._ROW.fullmatch(line)
                if match and 1 <= int(match.group(1)) <= 4096:
                    self._pending = (int(match.group(1)) - 1, match.group(2).strip())
                else:
                    self._remaining = 0
                continue
            # An address must be the indented line immediately after its row.
            # IPv6 and bots complete a row but have no legacy IPv4 history key.
            if not line[:1].isspace():
                self._remaining = 0
                self._pending = None
                continue
            slot, name = self._pending
            self._players[slot] = StatusPlayer(slot, name, address_prefix(line))
            self._pending = None
            self._remaining -= 1
            if not self._remaining:
                completed.append(self._players)
                self._players = {}
                self._deadline = 0
        if len(self._buffer) > 8192:
            self.reset()
        return completed
