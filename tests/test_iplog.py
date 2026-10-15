from pathlib import Path
import struct
import tempfile
import unittest

from pyquake.iplog import IpLog, StatusParser, address_prefix, format_address


STATUS = ("host:    Test server\nversion: QSS-M\ntcp/ip: 192.0.2.1:26000\n"
          "players: 2 active (16 max)\n\n"
          "#3  two cool names      -5  1:02:03\n   10.20.30.xxx\n"
          "#7  other player        8   0:00:10\n   192.0.2.44:26001\n")


class IpLogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="pyquake-iplog-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_address_formats_and_validation(self):
        for text in ("10.20.30", "10.20.30.1", "10.20.30.xxx", "10.20.30.*:26000"):
            with self.subTest(text=text):
                self.assertEqual(address_prefix(text), 0x0a141e)
        for text in ("256.1.2", "1.2.3.999", "1.2.3.4:0", "1.2.3.4:65536",
                     "1.2", "1.2.3.trailing", "[2001:db8::1]:26000", "botclient"):
            with self.subTest(text=text):
                self.assertIsNone(address_prefix(text))
        self.assertEqual(format_address(0x0a141e), "10.20.30.xxx")

    def test_qssm_binary_import_save_and_reload(self):
        source = self.directory / "qssm.dat"
        records = [(0x0a141e, b"player"), (0x0a141e, b"other name"),
                   (0xc00002, b"\xf7\xef\xef\xe4\xf3")]
        data = b"".join(struct.pack("<I16s", address, name) for address, name in records)
        source.write_bytes(data)
        history = IpLog(self.directory / "iplog.dat")
        self.assertEqual(history.merge(source), 3)
        self.assertEqual(history.merge(source), 0)
        history.save()
        self.assertEqual(history.path.read_bytes(), data)
        restored = IpLog(history.path)
        restored.load()
        self.assertEqual(restored.entries(), history.entries())
        self.assertFalse(restored.dirty)

    def test_names_deduplicate_and_records_keep_classic_name_length(self):
        history = IpLog()
        self.assertTrue(history.add(1, "a very long player name"))
        self.assertFalse(history.add(1, "a very long player name"))
        history.add(2, "a very long player name")
        history.add(1, "  ")
        self.assertEqual(history.names(), ["a very long pla"])

    def test_global_and_per_address_limits_evict_oldest_aliases(self):
        history = IpLog(limit=3, per_address=2)
        for name in ("one", "two", "three"):
            history.add(1, name)
        self.assertEqual(history.aliases(1), ["two", "three"])
        history.add(2, "four")
        history.add(3, "five")
        self.assertEqual(history.entries(), [(1, "three"), (2, "four"), (3, "five")])

    def test_saving_a_full_history_does_not_resurrect_evicted_disk_entries(self):
        history = IpLog(self.directory / "iplog.dat", limit=3)
        for name in ("one", "two", "three"):
            history.add(1, name)
        history.save()
        history.add(1, "four")
        history.save()
        restored = IpLog(history.path, limit=3)
        restored.load()
        self.assertEqual(restored.aliases(1), ["two", "three", "four"])

    def test_save_preserves_observations_from_another_session(self):
        first = IpLog(self.directory / "iplog.dat")
        second = IpLog(first.path)
        first.add(1, "one")
        second.add(2, "two")
        first.save()
        second.save()
        first.add(3, "three")
        first.save()
        self.assertEqual(set(first.names()), {"one", "two", "three"})

    def test_bad_import_does_not_partially_modify_history_or_overwrite_disk(self):
        history = IpLog(self.directory / "iplog.dat")
        history.add(1, "original")
        for data in (struct.pack("<I16s", 2, b"new") + b"short",
                     struct.pack("<I16s", 0xffffffff, b"invalid")):
            history.path.write_bytes(data)
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    history.merge(history.path)
                self.assertEqual(history.entries(), [(1, "original")])
                with self.assertRaises(ValueError):
                    history.save()
                self.assertEqual(history.path.read_bytes(), data)


class StatusParserTests(unittest.TestCase):
    def test_status_is_independent_of_print_chunk_boundaries(self):
        expected = StatusParser().feed(STATUS)[0]
        self.assertEqual(expected[2].name, "two cool names")
        self.assertEqual(expected[2].address, 0x0a141e)
        self.assertEqual(expected[6].address, 0xc00002)
        for split in range(len(STATUS) + 1):
            with self.subTest(split=split):
                parser = StatusParser()
                self.assertEqual(parser.feed(STATUS[:split]) + parser.feed(STATUS[split:]), [expected])
        parser = StatusParser()
        results = [result for char in STATUS for result in parser.feed(char)]
        self.assertEqual(results, [expected])

    def test_bot_and_ipv6_rows_do_not_generate_ipv4_keys(self):
        parser = StatusParser()
        text = STATUS.replace("   10.20.30.xxx", "   botclient").replace(
            "   192.0.2.44:26001", "   [2001:db8::1]:26000")
        players = parser.feed(text)[0]
        self.assertTrue(all(player.address is None for player in players.values()))

    def test_requires_complete_status_header_and_expires_partial_replies(self):
        parser = StatusParser()
        self.assertFalse(parser.feed(STATUS.split("players:", 1)[1]))
        prefix, rows = STATUS.split("#3", 1)
        self.assertFalse(parser.feed(prefix, now=1))
        self.assertFalse(parser.feed("#3" + rows, now=7))
        self.assertEqual(len(parser.feed(STATUS, now=8)), 1)

    def test_empty_status_replies_and_unprefixed_player_rows(self):
        self.assertEqual(StatusParser().feed("host: Test\nplayers: 0 active (16 max)\n"), [{}])
        players = StatusParser().feed(STATUS.replace("#3", " 3").replace("#7", " 7"))[0]
        self.assertEqual(set(players), {2, 6})

    def test_malformed_address_and_interrupted_rows_do_not_reuse_old_state(self):
        parser = StatusParser()
        bad = STATUS.replace("   10.20.30.xxx", "   999.20.30.xxx")
        self.assertIsNone(parser.feed(bad)[0][2].address)
        interrupted = STATUS.replace("   10.20.30.xxx\n", "unrelated text\n")
        self.assertFalse(parser.feed(interrupted))
        self.assertEqual(len(parser.feed(STATUS)), 1)
