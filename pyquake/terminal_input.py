"""A small ANSI console editor; ordinary input() remains the non-TTY fallback."""

import codecs
import os
import re
import select
import sys
import threading
import unicodedata


RESET = "\x1b[0m"
BACKGROUND = "\x1b[48;2;30;30;30m"
HIGHLIGHT = "\x1b[48;2;42;42;42m"
WHITE = "\x1b[97m"
BORDER = "\x1b[38;2;78;78;78m"


def supported():
    """Use the editor only on UTF-8 POSIX terminals with ANSI cursor support."""
    return (os.name == "posix" and os.environ.get("TERM", "") not in ("", "dumb")
            and sys.stdin.isatty() and sys.stdout.isatty()
            and "utf" in (sys.stdout.encoding or "").lower())


def cell_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def format_completions(candidates, width, colmax=0, direction=0):
    """Lay out QSS-M-style columns, reading down each column by default."""
    if not candidates:
        return ""
    lengths = [sum(cell_width(char) for char in name) for name in candidates]
    stride = (max(8, max(lengths) + 3) + 3) // 4 * 4
    columns = max(1, width // stride)
    if colmax >= 1:
        columns = min(columns, int(colmax))
    if len(candidates) < 6:
        columns = 1
    columns = min(columns, len(candidates))
    rows = (len(candidates) + columns - 1) // columns
    lines = []
    for row in range(rows):
        cells = []
        for column in range(columns):
            index = row * columns + column if direction == 1 else column * rows + row
            if index < len(candidates):
                cells.append("  " + candidates[index] + " " * (stride - 2 - lengths[index]))
        lines.append("".join(cells).rstrip())
    return "\n".join(lines) + "\n"


def layout(text, cursor, width):
    """Wrap inside the highlighted area, returning rows and a cell cursor."""
    capacity = max(2, width - 6)  # two left spaces, chevron, gap, two right spaces
    rows = [""]
    column = 0
    position = (0, 0)
    for index, char in enumerate(text):
        if index == cursor:
            position = (len(rows) - 1, column)
        size = cell_width(char)
        if char == "\n" or column + size > capacity:
            rows.append("")
            column = 0
            if index == cursor and char != "\n":
                position = (len(rows) - 1, 0)
        if char != "\n":
            rows[-1] += char
            column += size
        if column == capacity:
            rows.append("")
            column = 0
    if cursor == len(text):
        position = (len(rows) - 1, column)
    return rows, position


class _Output:
    """Collect fragmented prints, then insert complete lines above the editor."""

    def __init__(self, editor, stream):
        self.editor = editor
        self.stream = stream
        self.pending = ""

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, text):
        with self.editor.lock:
            if not self.editor.active:
                return self.stream.write(text)
            self.pending += text
            end = self.pending.rfind("\n") + 1
            if end:
                complete, self.pending = self.pending[:end], self.pending[end:]
                self.editor._erase()
                self.editor.output.write(RESET + complete)
                self.editor._draw()
            return len(text)

    def flush(self):
        self.stream.flush()


class TerminalInput:
    def __init__(self, *, complete, history=None, activity=lambda: None,
                 completion_options=lambda: (0, 0)):
        self.complete = complete
        self.completion_options = completion_options
        self.history = history if history is not None else []
        self.activity = activity
        self.lock = threading.RLock()
        self.active = False
        self.text = ""
        self.cursor = 0
        self.drawn = 0
        self.screen_cursor = 0
        self.size = (80, 24)
        self.history_index = len(self.history)
        self.draft = ""
        self.paste = None
        self.keys = ""
        self.decoder = None

    def _size(self):
        size = os.get_terminal_size(self.output.fileno())
        return max(8, size.columns), max(3, size.lines)

    def _erase(self):
        if not self.drawn:
            return
        self.output.write("\r" + (f"\x1b[{self.screen_cursor}A" if self.screen_cursor else ""))
        for row in range(self.drawn):
            self.output.write("\x1b[2K")
            if row < self.drawn - 1:
                self.output.write("\x1b[1B\r")
        if self.drawn > 1:
            self.output.write(f"\x1b[{self.drawn - 1}A")
        self.output.write("\r")
        self.drawn = 0

    def _draw(self):
        self.size = width, height = self._size()
        rows, (cursor_row, cursor_col) = layout(self.text, self.cursor, width)
        # Keep both borders on screen even for input longer than the terminal.
        first = max(0, cursor_row - (height - 3))
        visible = rows[first:first + height - 2]
        rule = BACKGROUND + BORDER + "─" * width + RESET
        lines = [rule]
        for index, row in enumerate(visible, first):
            content = row
            padding = width - 4 - sum(cell_width(c) for c in content)
            lead = "  " + WHITE + "❯" if index == 0 else "   "
            lines.append(BACKGROUND + lead + HIGHLIGHT + " " + WHITE + content
                         + " " * max(0, padding - 2) + BACKGROUND + "  " + RESET)
        lines.append(rule)
        self.output.write("\x1b[?25l" + "\r\n".join(lines))
        self.drawn = len(lines)
        self.screen_cursor = 1 + cursor_row - first
        up = self.drawn - 1 - self.screen_cursor
        self.output.write((f"\x1b[{up}A" if up else "") + "\r"
                          + f"\x1b[{cursor_col + 4}C\x1b[?25h")
        self.output.flush()

    def _insert(self, text):
        # Never allow pasted terminal controls to become output escape sequences.
        text = "".join(c for c in text.replace("\r\n", "\n").replace("\r", "\n")
                       .replace("\t", "    ") if c == "\n" or c.isprintable())
        self.text = self.text[:self.cursor] + text + self.text[self.cursor:]
        self.cursor += len(text)

    def _recall(self, step):
        if self.history_index == len(self.history):
            self.draft = self.text
        self.history_index = min(len(self.history), max(0, self.history_index + step))
        self.text = (self.history[self.history_index] if self.history_index < len(self.history)
                     else self.draft)
        self.cursor = len(self.text)

    def _complete(self):
        before = self.text[:self.cursor]
        start = len(before)
        while start and before[start - 1] not in " \t\n;":
            start -= 1
        prefix = before[start:]
        candidates = self.complete(before[:start], prefix)
        if not candidates:
            return
        replacement = os.path.commonprefix(candidates)
        if len(candidates) == 1:
            replacement += " "
        else:
            # Show choices through the same output path used by server messages.
            sys.stdout.write(format_completions(candidates, self._size()[0],
                                                *self.completion_options()))
        self.text = self.text[:start] + replacement + self.text[self.cursor:]
        self.cursor = start + len(replacement)

    def _key(self, key):
        if key in ("\r", "\n"):
            return True
        if key == "\x03":
            raise KeyboardInterrupt
        if key == "\x04":
            if not self.text:
                raise EOFError
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        elif key in ("\x7f", "\b"):
            if self.cursor:
                self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
                self.cursor -= 1
        elif key in ("\x1b[D", "\x1bOD", "\x02"):
            self.cursor = max(0, self.cursor - 1)
        elif key in ("\x1b[C", "\x1bOC", "\x06"):
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key in ("\x1b[H", "\x1bOH", "\x1b[1~", "\x01"):
            self.cursor = 0
        elif key in ("\x1b[F", "\x1bOF", "\x1b[4~", "\x05"):
            self.cursor = len(self.text)
        elif key == "\x1b[3~":
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        elif key in ("\x1b[A", "\x1bOA", "\x10"):
            self._recall(-1)
        elif key in ("\x1b[B", "\x1bOB", "\x0e"):
            self._recall(1)
        elif key == "\x15":
            self.text, self.cursor = self.text[self.cursor:], 0
        elif key == "\x0b":
            self.text = self.text[:self.cursor]
        elif key == "\x17":
            start = len(self.text[:self.cursor].rstrip())
            while start and not self.text[start - 1].isspace():
                start -= 1
            self.text = self.text[:start] + self.text[self.cursor:]
            self.cursor = start
        elif key == "\t":
            self._complete()
        elif key in ("\x1b\r", "\x1b\n"):
            self._insert("\n")
        elif len(key) == 1 and key.isprintable():
            self._insert(key)
        return False

    def _feed(self, chunk):
        self.keys += chunk
        while self.keys:
            if self.paste is not None:
                self.paste += self.keys
                self.keys = ""
                if "\x1b[201~" not in self.paste:
                    return False
                pasted, self.keys = self.paste.split("\x1b[201~", 1)
                self.paste = None
                self._insert(pasted)
                continue
            if self.keys.startswith("\x1b[200~"):
                self.keys, self.paste = self.keys[6:], ""
                continue
            if self.keys[0] == "\x1b":
                match = re.match(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|O.|[^\[O])", self.keys)
                if not match:
                    return False
                key = match.group()
            else:
                key = self.keys[0]
            self.keys = self.keys[len(key):]
            if self._key(key):
                return True
        return False

    def read(self, initial=""):
        import termios

        self.output = sys.stdout
        original_error = sys.stderr
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        settings = termios.tcgetattr(fd)
        settings[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
        settings[0] &= ~termios.IXON
        settings[6][termios.VMIN], settings[6][termios.VTIME] = 1, 0
        if self.decoder is None:
            self.decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")("replace")
        self.text, self.cursor = "", 0
        self._insert(initial)
        self.history_index = len(self.history)
        self.paste = None
        output = _Output(self, self.output)
        error = _Output(self, original_error)
        accepted = False
        try:
            termios.tcsetattr(fd, termios.TCSANOW, settings)
            with self.lock:
                self.active = True
                sys.stdout = output
                if original_error.isatty():
                    sys.stderr = error
                self.output.write("\x1b[?2004h")
                # Retain text typed after Enter for the next command.
                accepted = self._feed("")
                self._draw()
            while not accepted:
                ready, _, _ = select.select([fd], [], [], 0.1)
                with self.lock:
                    if self.size != self._size():
                        self._erase()
                        self._draw()
                    if ready:
                        data = os.read(fd, 4096)
                        if not data:
                            raise EOFError
                        self.activity()
                        accepted = self._feed(self.decoder.decode(data))
                        self._erase()
                        self._draw()
                        if accepted:
                            break
                    elif self.keys == "\x1b":
                        self.keys = ""
            if self.text and (not self.history or self.history[-1] != self.text):
                self.history.append(self.text)
            return self.text
        finally:
            try:
                with self.lock:
                    self.active = False
                    sys.stdout, sys.stderr = self.output, original_error
                    self._erase()
                    self.output.write(RESET + "\x1b[?2004l\x1b[?25h")
                    if accepted and self.text:
                        self.output.write("  " + WHITE + "❯ " + RESET + self.text + "\n")
                    for proxy in (output, error):
                        if proxy.pending:
                            self.output.write(proxy.pending + "\n")
                    self.output.flush()
            finally:
                termios.tcsetattr(fd, termios.TCSANOW, saved)
