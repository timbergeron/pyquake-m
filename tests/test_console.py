import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from pyquake import client


class ServerPrintTests(unittest.TestCase):
    def setUp(self):
        self.stdout = io.StringIO()
        self.console = client._ConsoleBuffer()
        cvars = client.CvarSystem()
        cvars.register(client.Cvar("name", "pyquake-m", cvar_type=client.CvarType.STRING))
        self.active = SimpleNamespace(_suppress_prints=False, _captured_ping_lines=[],
                                      _players={0: SimpleNamespace(name="woods")})
        for name, value in (("cvar_system", cvars), ("CURRENT_CLIENT", self.active), ("CON", self.console)):
            patcher = mock.patch.object(client, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("sys.stdout", self.stdout)
        patcher.start()
        self.addCleanup(patcher.stop)

    def render(self, *chunks):
        for chunk in chunks:
            client._display_server_print(chunk)
        return client.strip_ansi(self.stdout.getvalue())

    def test_standalone_newlines_are_not_discarded(self):
        self.assertEqual(self.render("\n", "\n", "efficiency\n", "\n"), "\n\n  efficiency\n\n")
        self.assertEqual(self.console.lines, ["", "", "  efficiency", ""])

    def test_embedded_newlines_preserve_category_gaps(self):
        self.assertEqual(self.render("kills\n\nbad stuff\n\n\nitems taken stats\n"),
                         "  kills\n\n  bad stuff\n\n\n  items taken stats\n")

    def test_crmod_stats_header_and_category_spacing(self):
        # Print calls and separator placement from CRMod7/src/stats.qc:
        # stats_autodump, stats_show_efficiency, and the items-taken sections.
        chunks = ("\n", "\n", "match statistics\n", "\n",
                  "map      \b:\b ", "q1q3hektik", "\n",
                  "date     \b:\b ", "10-14-2026 16:00", "\n",
                  "server   \b:\b ", "Denver", "\n",
                  "mode     \b:\b ", "standard", "\n",
                  "teamplay \b:\b ", "off", "\n", "\n",
                  "\n", "\befficiency\b\n", "\n",
                  "kill |weapn|     name\n", "-----+-----+---------------\n",
                  " N/A | N/A |woods\n", "\n",
                  "\n", "\bitems taken stats\n", "\n",
                  "hlth|mega|suit| GA| YA| RA|bkpk| name\n", "\n",
                  "\n", "\bweapon items taken stats\n\n",
                  "\b     weapons taken        ammo taken\n")
        expected = ("\n\n  match statistics\n\n"
                    "  map      : q1q3hektik\n"
                    "  date     : 10-14-2026 16:00\n"
                    "  server   : Denver\n"
                    "  mode     : standard\n"
                    "  teamplay : off\n\n\n"
                    "  efficiency\n\n"
                    "  kill |weapn|     name\n"
                    "  -----+-----+---------------\n"
                    "   N/A | N/A |woods\n\n\n"
                    "  items taken stats\n\n"
                    "  hlth|mega|suit| GA| YA| RA|bkpk| name\n\n\n"
                    "  weapon items taken stats\n\n"
                    "       weapons taken        ammo taken\n")
        self.assertEqual(self.render(*chunks), expected)

    def test_unknown_colon_labels_remain_visible(self):
        self.assertEqual(self.render("host: Denver\n", "tcp/ip: 127.0.0.1\n",
                                     "  QPER reference provisional: 0/300 observed player-minutes\n\n"),
                         "  host: Denver\n  tcp/ip: 127.0.0.1\n"
                         "    QPER reference provisional: 0/300 observed player-minutes\n\n")

    def test_fragmented_row_uses_server_newlines(self):
        self.assertEqual(self.render("NextMap is ", "q1q3hektik.", "\n", "\n"),
                         "  NextMap is q1q3hektik.\n\n")

    def test_fragmented_row_is_not_split_after_a_delay(self):
        with mock.patch.object(client, "time", SimpleNamespace(monotonic=mock.Mock(side_effect=[0, 5]))):
            self.assertEqual(self.render("  0 |", "  0 |woods\n"), "    0 |  0 |woods\n")

    def test_table_spacing_is_independent_of_print_chunk_boundaries(self):
        stream = "map      : q1q3hektik\n\nefficiency\n\nkill |weapn|     name\n N/A | N/A |woods\n\n"
        expected = "  map      : q1q3hektik\n\n  efficiency\n\n  kill |weapn|     name\n   N/A | N/A |woods\n\n"
        for size in (1, 2, 7, len(stream)):
            with self.subTest(chunk_size=size):
                self.stdout.seek(0)
                self.stdout.truncate()
                client.CON = self.console = client._ConsoleBuffer()
                self.assertEqual(self.render(*(stream[i:i + size] for i in range(0, len(stream), size))), expected)

    def test_chat_uses_the_same_explicit_newline_rules(self):
        self.assertEqual(self.render("woods: hel", "lo\n", "\n", ".(woods): team\n"),
                         "woods: hello\n\n(woods): team\n")

    def test_multiline_print_with_chat_keeps_all_text(self):
        self.assertEqual(self.render("woods: hello\n\nefficiency\n\n"),
                         "woods: hello\n\n  efficiency\n\n")

    def test_live_scoreboard_names_are_recognized_as_chat(self):
        self.active._players = {}
        self.active._score_state = {1: {"active": True, "name": "woods"}}
        self.assertEqual(self.render("woods: hello\n\n"), "woods: hello\n\n")

    def test_masked_color_keeps_blank_lines_and_resets_color(self):
        colored = "".join(chr(ord(char) | 0x80) for char in "kills")
        self.assertEqual(self.render(colored + "\n\n"), "  kills\n\n")
        self.assertIn(client.ANSI_RED, self.stdout.getvalue())
        self.assertIn(client.ANSI_RESET, self.stdout.getvalue())

    def test_ping_capture_preserves_newlines_without_displaying_them(self):
        self.active._suppress_prints = True
        self.assertEqual(self.render("Client ping times:\n", "\n", "70 woods\n"), "")
        self.assertEqual(self.active._captured_ping_lines, ["Client ping times:\n", "\n", "70 woods\n"])
        self.assertEqual(self.console.lines, [])

    def test_console_dump_preserves_interior_blank_lines(self):
        self.render("\n", "efficiency\n", "\n", "kills\n", "\n")
        with tempfile.TemporaryDirectory(prefix="pyquake-console-", dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / "console.txt"
            self.assertTrue(self.console.dump(str(path)))
            self.assertEqual(path.read_text(), "  efficiency\n\n  kills\n\n")

    def test_hidden_diagnostic_does_not_add_a_blank_line(self):
        self.assertEqual(self.render("cfg: private diagnostic\n", "kills\n\n"), "  kills\n\n")
