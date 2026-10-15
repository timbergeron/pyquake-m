import asyncio
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from pyquake import client
from pyquake.commands import quote_arg
from pyquake.iplog import IpLog
from test_client import FakeConnection
from test_proto import server_info


def status(name="player", address="10.20.30.xxx", slot=1):
    return (f"host: Test\nversion: QSS-M\nplayers: 1 active (16 max)\n\n"
            f"#{slot} {name:<16}   5   0:00:10\n   {address}\n")


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory(prefix="pyquake-identity-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.cvars = client.CvarSystem()
        self.cvars._config_file = str(self.directory / "config.cfg")
        self.console = mock.Mock()
        for name, value in (("cvar_system", self.cvars), ("CURRENT_CLIENT", None),
                            ("_CONSOLE_SESSION", None), ("CON", self.console),
                            ("_autosave_config", mock.Mock())):
            patch = mock.patch.object(client, name, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)
        self.conn = FakeConnection()
        self.active = client.AsyncClient(self.conn)
        self.active._read_task = asyncio.create_task(self.active._read_messages())
        self.active._read_task.add_done_callback(self.active._reader_finished)
        client.CURRENT_CLIENT = self.active
        self.session = client._get_console_session()

    async def asyncTearDown(self):
        await self.session.close()

    def output(self):
        return [call.args[0] for call in self.console.write.call_args_list]

    async def execute(self, command):
        await self.session.dispatcher.execute(command, source="console")

    async def receive(self, data):
        waiting = asyncio.create_task(self.active.wait_for_center_print())
        await asyncio.sleep(0)
        self.conn.incoming.put_nowait(data + b"\x1aprocessed\0")
        await asyncio.wait_for(waiting, 1)

    async def printed(self, text):
        await self.receive(b"\x08" + text.encode("latin-1") + b"\0")

    async def test_status_records_persist_and_identity_commands_stay_local(self):
        await self.receive(b"\x0d\0player\0")
        text = status()
        chunks = (text[:12], text[12:45], text[45:-4], text[-4:])
        await self.receive(b"".join(b"\x08" + chunk.encode("latin-1") + b"\0" for chunk in chunks))
        await self.execute("identify 1; identify #1; identify PLAYER; identify 10.20.30.99")
        self.assertFalse(self.conn.sent)
        self.assertEqual(self.output().count("player"), 4)
        restored = IpLog(self.directory / "iplog.dat")
        restored.load()
        self.assertEqual(restored.aliases(0x0a141e), ["player"])

    async def test_identify_refreshes_status_and_defaults_to_latest_connected_player(self):
        await self.receive(b"\x0d\x02first\0\x0d\0last connected\0")
        original = self.conn.send_reliable

        async def send(data):
            await original(data)
            if data == b"\x04status\0":
                self.conn.incoming.put_nowait(b"\x08" + status("last connected").encode() + b"\0")

        with mock.patch.object(self.conn, "send_reliable", side_effect=send):
            await self.execute("identify")
        self.assertEqual(self.conn.sent, [b"\x04status\0"])
        self.assertIn("known aliases for last connected:", self.output())
        self.assertIn("1 entry found", self.output())

    async def test_reused_slots_and_map_changes_do_not_retain_old_addresses(self):
        await self.receive(b"\x0d\0old\0")
        await self.printed(status("old"))
        await self.receive(b"\x0d\0\0\x0d\0new\0")
        self.assertNotIn(0, self.active._status_players)
        await self.printed(status("new", "192.0.2.xxx"))
        await self.execute("identify 1")
        self.assertIn("known aliases for new:", self.output())
        self.assertEqual(self.session.iplog.aliases(0x0a141e), ["old"])
        self.assertEqual(self.session.iplog.aliases(0xc00002), ["new"])
        await self.receive(server_info())
        self.assertFalse(self.active._status_players)
        self.assertIsNone(self.active._last_connected_slot)
        self.assertEqual(self.session.iplog.aliases(0x0a141e), ["old"])

    async def test_name_changes_require_fresh_status_before_reusing_address(self):
        await self.receive(b"\x0d\0old\0")
        await self.printed(status("old"))
        await self.receive(b"\x0d\0new\0")
        self.assertNotIn(0, self.active._status_players)
        await self.printed(status("new"))
        await self.execute("identify new")
        self.assertEqual(self.session.iplog.aliases(0x0a141e), ["old", "new"])
        self.assertIn("2 entries found", self.output())

    async def test_late_status_cannot_assign_a_previous_players_address_to_reused_slot(self):
        await self.receive(b"\x0d\0new\0")
        await self.printed(status("old", "10.20.30.xxx"))
        self.assertNotIn(0, self.active._status_players)
        self.assertEqual(self.session.iplog.aliases(0x0a141e), ["old"])
        await self.printed(status("new", "192.0.2.xxx"))
        await self.execute("identify 1")
        self.assertEqual(self.session.iplog.aliases(0xc00002), ["new"])

    async def test_ipmerge_ipdump_ipnames_and_offline_prefix_lookup(self):
        source = self.directory / "existing qssm.dat"
        source.write_bytes(struct.pack("<I16s", 0x0a141e, b"old name")
                           + struct.pack("<I16s", 0x0a141e, b"new name")
                           + struct.pack("<I16s", 0xc00002, b"new name"))
        await self.execute(f"ipmerge {quote_arg(source)}; ipnames; ipdump")
        dump = (self.directory / "iplog.txt").read_text()
        self.assertIn("10.20.30.xxx", dump)
        self.assertIn("old name", dump)
        self.assertIn("2 entries found", self.output())  # Names are unique across prefixes.
        await self.session.disconnect()
        await self.execute("identify 10.20.30.xxx")
        self.assertIn("known aliases for 10.20.30.xxx:", self.output())
        self.assertFalse(self.conn.sent)

    async def test_numeric_name_prefixes_fun_names_and_ambiguous_matches(self):
        await self.receive(b"\x0d\0one player\0\x0d\x01other player\0\x0d\x02\xf7\xb0\xb0\xe4\xf3\0")
        await self.printed("host: Test\nplayers: 3 active (16 max)\n"
                           "#1 one player       0 0:00:01\n   10.20.30.xxx\n"
                           "#2 other player     0 0:00:01\n   192.0.2.xxx\n"
                           "#3 w00ds            0 0:00:01\n   198.51.100.xxx\n")
        await self.execute("identify player; identify woods")
        self.assertIn("No unique player match for that name", self.output())
        self.assertIn("known aliases for w00ds:", self.output())
        await self.receive(b"\x0d\0\0\x0d\0" + b"2 cool names" + b"\0")
        await self.printed(status("2 cool names"))
        await self.execute("identify 2 cool names")
        self.assertIn("known aliases for 2 cool names:", self.output())

    async def test_marked_chat_cannot_create_identity_history(self):
        await self.printed("\x01" + status("spoofed"))
        self.assertFalse(self.active._status_players)
        self.assertFalse(self.session.iplog.entries())
        self.assertFalse((self.directory / "iplog.dat").exists())

    async def test_status_wait_timeout_and_disconnect_do_not_cancel_reader(self):
        self.assertFalse(await self.active.refresh_status(timeout=0.001))
        await self.printed(status())
        self.assertFalse(self.active._read_task.done())
        waiting = asyncio.create_task(self.active.refresh_status())
        await asyncio.sleep(0)
        await self.active.disconnect()
        self.assertFalse(await asyncio.wait_for(waiting, 1))

    async def test_persistence_errors_do_not_disconnect_and_import_errors_are_local(self):
        with mock.patch.object(self.session.iplog, "save", side_effect=OSError("disk full")), \
                self.assertLogs(client.logger, level="WARNING"):
            await self.printed(status())
        self.assertFalse(self.active._read_task.done())
        await self.execute("ipmerge /definitely/missing/iplog.dat; ipmerge; ipnames extra")
        self.assertTrue(any(line.startswith("ipmerge:") for line in self.output()))
        self.assertIn("usage: ipmerge <filename>", self.output())
        self.assertIn("usage: ipnames", self.output())

    async def test_history_survives_reconnect_and_cannot_be_shadowed_by_aliases(self):
        await self.printed(status())
        replacement = client.AsyncClient(FakeConnection())
        replacement._spawned_fut.set_result(None)

        async def background(active):
            await asyncio.Event().wait()

        with mock.patch.object(client.AsyncClient, "connect", return_value=replacement), \
                mock.patch.object(client, "_simple_keep_alive", background), \
                mock.patch.object(client, "_watch_chatinfo", background), \
                mock.patch.object(client, "_kickstart_handshake", background), \
                mock.patch.object(client, "_connection_guard", background):
            await self.session.connect("localhost")
        self.assertIs(replacement.iplog, self.session.iplog)
        await self.execute('alias identify "say shadowed"; identify 10.20.30')
        self.assertIn("identify_a", self.session.aliases)
        self.assertFalse(replacement._conn.sent)
        self.assertIn("known aliases for 10.20.30.xxx:", self.output())
