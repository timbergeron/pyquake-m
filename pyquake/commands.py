"""Ordered console, alias, and config command execution."""

import asyncio
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import re
import shlex


def split_commands(text):
    """Split commands, retaining quoted separators and discarding // comments."""
    commands = []
    current = []
    quoted = escaped = comment = False
    i = 0
    while i < len(text):
        char = text[i]
        if char in "\r\n" or (char == ";" and not quoted and not comment):
            command = "".join(current).strip()
            if command:
                commands.append(command)
            current = []
            quoted = escaped = comment = False
        elif comment:
            pass
        elif escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quoted:
            current.append(char)
            escaped = True
        elif char == '"':
            current.append(char)
            quoted = not quoted
        elif (not quoted and text[i:i + 2] == "//"
              and (not current or current[-1].isspace())):
            comment = True
            i += 1
        else:
            current.append(char)
        i += 1
    command = "".join(current).strip()
    if command:
        commands.append(command)
    return commands


def quote_arg(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def alias_key(aliases, name):
    return next((key for key in aliases if key.casefold() == name.casefold()), None)


def define_alias(aliases, name, body, *, reserved, is_cvar, allow_overlap=False):
    if not name or any(char.isspace() for char in name) or any(char in name for char in ';"\\'):
        raise ValueError("Invalid alias name")
    if reserved(name.lower()) or (is_cvar(name) and not allow_overlap):
        name += "_a"
    key = alias_key(aliases, name) or name
    aliases[key] = body
    return key


def substitute_alias(body, name, args):
    def substitute(match):
        key = match.group(1)
        if key == "*":
            values = args
        elif key == "0":
            values = [name]
        else:
            index = int(key) - 1
            values = [args[index] if 0 <= index < len(args) else ""]
        quoted = escaped = False
        for char in body[:match.start()]:
            if escaped:
                escaped = False
            elif char == "\\" and quoted:
                escaped = True
            elif char == '"':
                quoted = not quoted
        if quoted:
            return " ".join(values).replace("\\", "\\\\").replace('"', '\\"')
        return " ".join(quote_arg(value) for value in values)
    result = re.sub(r"\$(\*|[0-9])", substitute, body)
    if "$" not in body and args:
        result = result.rstrip() + " " + " ".join(quote_arg(arg) for arg in args)
    return result


@dataclass
class _Command:
    text: str
    source: str = "command"
    alias_depth: int = 0
    files: tuple = ()


class CommandDispatcher:
    """One serial queue; expansion inserts commands ahead of the remainder."""

    def __init__(self, *, aliases, handle, forward, is_command, chat_mode,
                 wait_frame, output, reserved=(), max_depth=16):
        self.aliases = aliases
        self.handle = handle
        self.forward = forward
        self.is_command = is_command
        self.chat_mode = chat_mode
        self.wait_frame = wait_frame
        self.output = output
        self.reserved = reserved if callable(reserved) else lambda name: name in reserved
        self.max_depth = max_depth
        self._queue = deque()
        self._task = None
        self.current = None
        self.stopped = False

    def add_text(self, text, *, source="command", insert=False, context=None):
        if self.stopped:
            return
        parent = context or _Command("", source)
        entries = [_Command(command, source, parent.alias_depth, parent.files)
                   for command in split_commands(text)]
        if insert:
            self._queue.extendleft(reversed(entries))
        else:
            self._queue.extend(entries)
        if self._queue and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(self._pump())

    async def execute(self, text, *, source="command"):
        if source == "console":
            text = self._console_command(text)
        self.add_text(text, source=source)
        await self.flush()

    def _console_command(self, text):
        stripped = text.lstrip()
        if stripped.startswith("/") and not stripped.startswith("//"):
            return stripped[1:]
        if not stripped or stripped.startswith("//"):
            return text
        mode = self.chat_mode()
        if ((mode in (1, 2) and (text[:1].isspace() or not self.is_command(text)))
                or (mode == 3 and text.startswith(" "))):
            return "say " + quote_arg(text.strip())
        return text

    async def flush(self):
        # Handlers insert nested work; only callers outside the pump wait for it.
        if self._task is not None and self._task is not asyncio.current_task():
            await asyncio.shield(self._task)

    def exec_file(self, filename, *, fallback=None):
        path = Path(filename).expanduser()
        parent = self.current or _Command("")
        if not path.is_absolute() and not path.exists() and parent.files:
            path = Path(parent.files[-1]).parent / path
        path = path.resolve()
        if str(path) in parent.files or len(parent.files) >= 16:
            self.output(f"Recursive exec rejected: {filename}")
            return False
        try:
            data = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            data = path.read_text(encoding="latin-1")
        except OSError:
            if fallback is None:
                self.output(f"couldn't exec {filename}")
                return False
            data = fallback
        context = _Command("", "config", parent.alias_depth, parent.files + (str(path),))
        self.add_text(data, source="config", insert=True, context=context)
        return True

    async def _pump(self):
        try:
            while self._queue and not self.stopped:
                self.current = entry = self._queue.popleft()
                try:
                    tokens = shlex.split(entry.text)
                    if not tokens:
                        continue
                    name = tokens[0].lower()
                    if name == "wait":
                        if len(tokens) > 2 or (len(tokens) == 2 and not tokens[1].isdigit()):
                            self.output("usage: wait [frames]")
                        else:
                            await self.wait_frame(max(1, int(tokens[1]) if len(tokens) == 2 else 1))
                        continue
                    key = alias_key(self.aliases, tokens[0])
                    if key is not None and not self.reserved(name):
                        if entry.alias_depth >= self.max_depth:
                            self.output(f"Alias expansion exceeded {self.max_depth} levels: {key}")
                            continue
                        context = _Command("", "alias", entry.alias_depth + 1, entry.files)
                        body = substitute_alias(self.aliases[key], key, tokens[1:])
                        self.add_text(body, source="alias", insert=True, context=context)
                    elif not await self.handle(entry.text):
                        await self.forward(entry.text)
                    # Let cvar callbacks enqueue their network updates before the next command.
                    await asyncio.sleep(0)
                except Exception as exc:
                    self.output(f"Error: {exc}")
        finally:
            self.current = None

    def stop(self):
        self.stopped = True
        self._queue.clear()

    async def close(self):
        self.stop()
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
