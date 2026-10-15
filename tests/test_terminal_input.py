import io
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time
import unittest
from unittest import mock

from pyquake.terminal_input import TerminalInput, format_completions, layout, supported


class CompletionLayoutTests(unittest.TestCase):
    def test_columns_fill_down_and_fit_terminal_width(self):
        names = [f"a{i}" for i in range(1, 8)]
        self.assertEqual(format_completions(names, 24),
                         "  a1      a4      a7\n  a2      a5\n  a3      a6\n")
        self.assertEqual(format_completions(names, 8),
                         "".join(f"  {name}\n" for name in names))

    def test_column_limit_and_horizontal_direction(self):
        names = [f"a{i}" for i in range(1, 8)]
        self.assertEqual(format_completions(names, 80, 2),
                         "  a1      a5\n  a2      a6\n  a3      a7\n  a4\n")
        self.assertEqual(format_completions(names, 80, 1),
                         "".join(f"  {name}\n" for name in names))
        self.assertEqual(format_completions(names, 80, 2, 1),
                         "  a1      a2\n  a3      a4\n  a5      a6\n  a7\n")

    def test_small_lists_and_long_names_use_one_column(self):
        self.assertEqual(format_completions(["alpha", "beta"], 80), "  alpha\n  beta\n")
        names = ["long_server_address" + str(i) for i in range(6)]
        self.assertEqual(format_completions(names, 8),
                         "".join(f"  {name}\n" for name in names))
        self.assertEqual(format_completions([], 80), "")

    def test_unicode_alignment_uses_cell_width(self):
        names = ["界", "a", "e\u0301", "b", "c", "d"]
        self.assertEqual(format_completions(names, 16),
                         "  界      b\n  a       c\n  e\u0301       d\n")


class LayoutTests(unittest.TestCase):
    def test_wrap_reserves_a_cursor_cell_at_the_right_edge(self):
        self.assertEqual(layout("abcdef", 6, 12), (["abcdef", ""], (1, 0)))
        self.assertEqual(layout("abcdefg", 3, 12), (["abcdef", "g"], (0, 3)))

    def test_newlines_and_unicode_use_terminal_cells(self):
        self.assertEqual(layout("界界a\ne\u0301", 6, 12), (["界界a", "e\u0301"], (1, 1)))

    def test_render_has_full_width_borders_and_highlight(self):
        editor = TerminalInput(complete=lambda *_: [])
        editor.output = io.StringIO()
        editor._size = lambda: (32, 10)
        editor._draw()
        output = editor.output.getvalue()
        self.assertEqual(output.count("─" * 32), 2)
        self.assertIn("\x1b[48;2;30;30;30m", output)
        self.assertIn("\x1b[48;2;42;42;42m", output)
        self.assertIn("❯", output)
        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
        self.assertEqual(plain.split("\r\n")[1], "  ❯" + " " * 29)
        self.assertEqual(editor.drawn, 3)
        editor.text, editor.cursor = "x" * 1000, 1000
        editor._erase()
        editor._draw()
        self.assertEqual(editor.drawn, 10)
        self.assertLess(editor.screen_cursor, 9)

    def test_redirected_input_uses_standard_prompt(self):
        with mock.patch("sys.stdin", io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(supported())


@unittest.skipUnless(os.name == "posix", "POSIX terminal editor")
class TerminalTests(unittest.TestCase):
    def start_editor(self, *, count=1, server=False, candidates=None):
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 32, 0, 0))
        script = f'''
import json, sys, termios, threading
from pyquake.terminal_input import TerminalInput
saved = termios.tcgetattr(0)
typed = threading.Event()
editor = TerminalInput(complete=lambda context, prefix: (
                           {candidates!r} if {candidates is not None!r}
                           else ["status"] if prefix == "sta" else []),
                       activity=typed.set)
def server():
    typed.wait(3)
    sys.stdout.write("woods: ")
    sys.stdout.flush()
    sys.stdout.write("hello\\n")
    sys.stdout.flush()
if {server!r}:
    threading.Thread(target=server, daemon=True).start()
try:
    for _ in range({count}):
        result = editor.read()
        print("RESULT:" + json.dumps(result), flush=True)
except (KeyboardInterrupt, EOFError) as exc:
    print("STOP:" + type(exc).__name__, flush=True)
finally:
    print("RESTORED:" + str(saved == termios.tcgetattr(0)), flush=True)
'''
        process = subprocess.Popen([sys.executable, "-u", "-c", script],
                                   cwd=Path(__file__).resolve().parents[1],
                                   stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, "TERM": "xterm-256color"})
        self.addCleanup(self.stop_process, process)
        self.master, self.process, self.transcript = master, process, ""
        self.wait_for("\x1b[?25h")
        return slave

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)

    def wait_for(self, marker):
        deadline = time.monotonic() + 5
        while marker not in self.transcript:
            if time.monotonic() >= deadline:
                self.fail(f"Timed out waiting for {marker!r}: {self.transcript!r}")
            if select.select([self.master], [], [], 0.1)[0]:
                self.transcript += os.read(self.master, 65536).decode("utf-8", "replace")

    def send(self, text):
        os.write(self.master, text.encode())

    def finish(self):
        self.wait_for("RESTORED:True")
        self.assertEqual(self.process.wait(timeout=3), 0)
        # Strip ANSI before reading the application's result markers.
        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", self.transcript)
        return [json.loads(line) for line in re.findall(r"RESULT:([^\r\n]+)", plain)]

    def test_editing_completion_history_and_typeahead(self):
        self.start_editor(count=3)
        self.send("sta\t\r\x1b[A\x01/\x05\x7f\rnext\r")
        self.assertEqual(self.finish(), ["status ", "/status", "next"])

    def test_ambiguous_completion_prints_vertical_list_and_keeps_editing(self):
        self.start_editor(candidates=["stats", "status"])
        self.send("sta\t")
        self.wait_for("  stats\r\n  status\r\n")
        self.send("us\r")
        self.assertEqual(self.finish(), ["status"])

    def test_multiline_paste_does_not_execute_and_filters_controls(self):
        self.start_editor()
        self.send("\x1b[20")
        self.send("0~say 界\nhello\x1b[31m\x03\x1b[20")
        self.send("1~")
        self.wait_for("hello[31m")
        self.assertNotIn("RESULT:", self.transcript)
        self.send("\r")
        self.assertEqual(self.finish(), ["say 界\nhello[31m"])

    def test_server_output_and_resize_preserve_the_input(self):
        import fcntl
        import struct
        import termios

        slave = self.start_editor(server=True)
        self.send("say " + "x" * 70)
        self.wait_for("woods: hello")
        self.assertIn("─" * 32, self.transcript)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 20, 0, 0))
        self.wait_for("─" * 20)
        self.send("\r")
        self.assertEqual(self.finish(), ["say " + "x" * 70])

    def test_interrupt_and_eof_restore_terminal(self):
        for key, exception in (("\x03", "KeyboardInterrupt"), ("\x04", "EOFError")):
            with self.subTest(exception=exception):
                self.start_editor()
                self.send(key)
                self.assertEqual(self.finish(), [])
                self.assertIn("STOP:" + exception, self.transcript)
