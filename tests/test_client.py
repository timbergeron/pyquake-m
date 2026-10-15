import asyncio
import io
import math
import struct
import unittest
from unittest import mock

from pyquake import client, proto
from test_proto import client_data, server_info


class FakeConnection:
    joequake_version = None

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def read_message(self):
        return await self.incoming.get()

    async def send_reliable(self, data):
        self.sent.append(data)

    def send(self, data):
        self.sent.append(data)

    def disconnect(self):
        self.closed = True

    async def wait_until_disconnected(self):
        pass


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cvars = client.CvarSystem()
        for name, value in (("cvar_system", self.cvars), ("CURRENT_CLIENT", None),
                            ("CON", mock.Mock()), ("_autosave_config", mock.Mock())):
            patcher = mock.patch.object(client, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn = FakeConnection()
        self.client = client.AsyncClient(self.conn)
        self.client._read_task = asyncio.create_task(self.client._read_messages())
        self.client._read_task.add_done_callback(self.client._reader_finished)

    async def asyncTearDown(self):
        await self.client.disconnect()

    async def test_centerprint_wait_timeout_does_not_cancel_shared_future(self):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.client.wait_for_center_print(), 0.001)
        self.assertFalse(self.client._center_print_fut.cancelled())
        waiting = asyncio.create_task(self.client.wait_for_center_print())
        await asyncio.sleep(0)
        self.conn.incoming.put_nowait(b"\x1ahello\0")
        self.assertEqual(await asyncio.wait_for(waiting, 1), "hello")
        self.assertFalse(self.client._read_task.done())

    async def test_spawn_wait_survives_timeout_and_remembers_spawn(self):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.client.wait_until_spawn(), 0.001)
        self.conn.incoming.put_nowait(b"\x19\x03")
        await asyncio.wait_for(self.client.wait_until_spawn(), 1)
        await asyncio.wait_for(self.client.wait_until_spawn(), 0.1)
        self.assertIn(b"\x04begin\0", self.conn.sent)
        self.assertFalse(self.client._read_task.done())

    async def test_mid_map_demo_replays_signon_and_current_state(self):
        baseline = b"\x16" + struct.pack("<HBBBBhBhBhB", 1, 0, 0, 0, 0, 8, 0, 0, 0, 0, 0)
        await self.receive(b"\x08QSS-M server\n\0" + server_info(666) + b"\x05\x01\0\x19\x01")
        await self.receive(baseline + b"\x19\x02")
        await self.receive(b"\x0d\x00old\0\x19\x03")
        await self.receive(b"\x0d\x00current\0\x0e\0\x05\0\x0c\0abc\0"
                           + b"\x07" + struct.pack("<f", 120))
        demo = self.client.record_demo()
        self.assertTrue(demo.recording_started)
        await self.receive(b"\x08still recording\n\0" + client_data() + b"\x82\x01\x10\0")
        await self.client.disconnect()
        self.assertTrue(demo.recording_complete)
        output = io.BytesIO()
        demo.dump(output)
        output.seek(0)
        messages = [entry[2] for entry in proto.read_demo_file(output)]
        kinds = [message.msg_type for message in messages]
        self.assertEqual(kinds[:2], [proto.ServerMessageType.PRINT, proto.ServerMessageType.SERVERINFO])
        self.assertIn(proto.ServerMessageType.SPAWNBASELINE, kinds)
        self.assertEqual([m.num for m in messages if m.msg_type == proto.ServerMessageType.SIGNONNUM], [1, 2, 3])
        self.assertEqual([m.name for m in messages if m.msg_type == proto.ServerMessageType.UPDATENAME][-1], "current")
        self.assertEqual([m.time for m in messages if m.msg_type == proto.ServerMessageType.TIME][-1], 120)
        self.assertIn("still recording\n", [m.string for m in messages
                                           if m.msg_type == proto.ServerMessageType.PRINT])
        self.assertEqual(kinds[-1], proto.ServerMessageType.DISCONNECT)

    async def test_demo_cache_resets_on_map_change_and_active_demo_continues(self):
        await self.receive(server_info(level=b"old") + b"\x19\x03")
        first = self.client.record_demo()
        await self.receive(server_info(level=b"new") + b"\x19\x03")
        second = self.client.record_demo()
        for demo, levels in ((first, ["old", "new"]), (second, ["new"])):
            demo.stop_recording()
            output = io.BytesIO()
            demo.dump(output)
            output.seek(0)
            self.assertEqual([m.level_name for _, _, m in proto.read_demo_file(output)
                              if m.msg_type == proto.ServerMessageType.SERVERINFO], levels)

    async def test_demo_started_before_signon_records_and_stops_once(self):
        demo = self.client.record_demo()
        await self.receive(b"\x08negotiating\0")
        self.assertFalse(demo._msgs)
        await self.receive(server_info() + b"\x19\x03")
        demo.stop_recording()
        demo.stop_recording()
        self.assertEqual(demo._msgs[-1], b"\x02")
        self.assertEqual(demo._msgs.count(b"\x02"), 1)
        await self.receive(b"\x01")
        self.assertFalse(self.client._read_task.done())

    async def test_demo_preserves_final_server_packet_before_disconnect(self):
        demo = self.client.record_demo()
        await self.receive(server_info() + b"\x19\x03")
        self.conn.incoming.put_nowait(b"\x08final match statistics\n\0\x02")
        await self.client._read_task
        await asyncio.sleep(0)
        output = io.BytesIO()
        demo.dump(output)
        output.seek(0)
        messages = [m for _, _, m in proto.read_demo_file(output)]
        self.assertTrue(demo.recording_complete)
        self.assertEqual(messages[-2].string, "final match statistics\n")
        self.assertEqual(messages[-1].msg_type, proto.ServerMessageType.DISCONNECT)
        self.assertEqual(sum(m.msg_type == proto.ServerMessageType.DISCONNECT for m in messages), 1)

    async def test_demo_cache_does_not_retain_gameplay_history(self):
        await self.receive(server_info() + b"\x19\x03")
        signon = list(self.client._demo_signon)
        for frame in range(100):
            await self.receive(b"\x07" + struct.pack("<f", frame))
        self.assertEqual(self.client._demo_signon, signon)
        self.assertEqual(len(self.client._demo_state), 1)
        demo = self.client.record_demo()
        demo.stop_recording()
        output = io.BytesIO()
        demo.dump(output)
        output.seek(0)
        self.assertEqual([m.time for _, _, m in proto.read_demo_file(output)
                          if m.msg_type == proto.ServerMessageType.TIME], [99])

    async def test_update_wait_survives_timeout(self):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.client.wait_for_update(), 0.001)
        waiting = asyncio.create_task(self.client.wait_for_update())
        await asyncio.sleep(0)
        self.conn.incoming.put_nowait(client_data())
        await asyncio.wait_for(waiting, 1)
        self.assertFalse(self.client._read_task.done())

    async def test_disconnect_stops_reader_and_wakes_waiters(self):
        waiting = asyncio.create_task(self.client.wait_for_update())
        await asyncio.sleep(0)
        await self.client.disconnect()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertTrue(self.conn.closed)
        self.assertTrue(self.client.disconnected)
        self.assertFalse(self.client._alive)
        self.assertTrue(self.client._read_task.done())

    async def test_reader_errors_close_connection_and_report_failure(self):
        with self.assertLogs(level="WARNING") as logs:
            self.conn.incoming.put_nowait(b"\x07")
            await asyncio.gather(self.client._read_task, return_exceptions=True)
            await asyncio.sleep(0)
        self.assertTrue(self.client.disconnected)
        self.assertTrue(self.conn.closed)
        self.assertTrue(any("Truncated server message" in line for line in logs.output))

    async def test_serverinfo_clears_old_map_state(self):
        self.client.baselines[1] = client.Entity.make_zero_baseline()
        self.client.entities[1] = client.Entity.make_zero_baseline()
        self.client._latest_ping_by_name["old"] = 25
        self.client._userinfo_is[1] = client.InfoString({"name": "old"})
        self.client._score_state[1] = {"name": "old"}
        waiting = asyncio.create_task(self.client.wait_for_center_print())
        await asyncio.sleep(0)
        self.conn.incoming.put_nowait(server_info() + b"\x1aready\0")
        await asyncio.wait_for(waiting, 1)
        for state in (self.client.baselines, self.client.entities, self.client._latest_ping_by_name,
                      self.client._userinfo_is, self.client._score_state):
            self.assertFalse(state)

    async def test_extended_baseline_is_used_by_entity_updates(self):
        baseline = (b"\x2a" + struct.pack("<HBBBBB", 1, 0, 3, 4, 5, 6)
                    + struct.pack("<hBhBhB", 8, 0, 16, 0, 24, 0))
        waiting = asyncio.create_task(self.client.wait_for_update())
        await asyncio.sleep(0)
        self.conn.incoming.put_nowait(server_info(666) + baseline + b"\x80\x01" + client_data())
        await asyncio.wait_for(waiting, 1)
        self.assertEqual(self.client.entities[1].origin, (1, 2, 3))
        self.assertEqual(self.client.entities[1].model_num, 3)

    async def receive(self, packet):
        waiting = asyncio.create_task(self.client.wait_for_center_print())
        await asyncio.sleep(0)
        self.conn.incoming.put_nowait(packet + b"\x1acheckpoint\0")
        await asyncio.wait_for(waiting, 1)

    async def test_live_frags_reach_scoreboard_and_team_totals(self):
        await self.receive(b"\x0d\x02player\0\x11\x02\x04"
                           + b"\x0e" + struct.pack("<Bh", 2, -5))
        self.assertEqual(self.client._score_state[2]["frags"], -5)
        self.assertEqual(self.client._derive_team_frags_from_players(), {4: -5})
        await self.receive(b"\x0e" + struct.pack("<Bh", 2, 12))
        self.assertEqual(self.client._score_state[2]["frags"], 12)

    async def test_departed_player_does_not_survive_in_ping_or_userinfo_caches(self):
        await self.receive(b"\x0d\x02old\0\x11\x02\x04")
        self.client._score_state[2].update(ping=42, frags=8)
        self.client._latest_ping_by_name["old"] = 42
        await self.receive(b"\x0d\x02\0")
        self.assertFalse(self.client._score_state[2]["active"])
        self.assertNotIn("old", self.client._latest_ping_by_name)
        self.assertNotIn(2, self.client._userinfo_is)
        await self.receive(b"\x0d\x02new\0")
        self.assertNotIn("ping", self.client._score_state[2])
        self.assertNotIn("frags", self.client._score_state[2])
        self.assertEqual(self.client.userinfo_get(2, "name"), "new")

    async def test_received_info_extensions_update_caches(self):
        commands = ('//fullserverinfo "\\hostname\\Test server\\timelimit\\10"\n'
                    'svi "matchtime" "3:42"\n'
                    'cmd fui 2 "\\name\\player\\bottomcolor\\4"\n'
                    '//ui 2 "bottomcolor" "13"\n')
        await self.receive(b"\x09" + commands.encode("latin-1") + b"\0")
        self.assertEqual(self.client.serverinfo_get("hostname"), "Test server")
        self.assertEqual(self.client.serverinfo_get("matchtime"), "3:42")
        self.assertEqual(self.client.userinfo_get(2, "name"), "player")
        self.assertEqual(self.client.userinfo_get(2, "bottomcolor"), "13")
        self.assertEqual(self.client._score_state[2]["bottomcolor"], 13)
        await self.receive(b'\x09fullserverinfo ""\n\0')
        self.assertFalse(self.client._serverinfo_is)

    async def test_synthesized_userinfo_tracks_changes_and_yields_to_real_info(self):
        await self.receive(b"\x0d\x02old\0\x11\x02\x34")
        self.assertEqual(self.client.userinfo_get(2, "name"), "old")
        self.assertEqual(self.client.userinfo_get(2, "topcolor"), "3")
        await self.receive(b"\x0d\x02new\0\x11\x02\x5d")
        self.assertEqual(self.client.userinfo_get(2, "name"), "new")
        self.assertEqual(self.client.userinfo_get(2, "topcolor"), "5")
        self.assertEqual(self.client.userinfo_get(2, "bottomcolor"), "13")
        await self.receive(b'\x09ui 2 "name" "server name"\n\0')
        await self.receive(b"\x0d\x02changed\0")
        self.assertEqual(self.client.userinfo_get(2, "name"), "server name")

    async def test_entity_colormap_does_not_override_player_colors(self):
        baseline = (b"\x16" + struct.pack("<HBBBB", 20, 0, 0, 20, 0)
                    + struct.pack("<hBhBhB", 0, 0, 0, 0, 0, 0))
        # Both a baseline before the name and one after UPDATECOLORS must be safe.
        await self.receive(baseline + b"\x0d\x13player\0")
        self.assertNotIn("bottomcolor", self.client._score_state[19])
        await self.receive(b"\x11\x13\x5d" + baseline + b"\x81\x08\x14\x14")
        self.assertEqual(self.client._score_state[19]["bottomcolor"], 13)
        self.assertEqual(self.client.userinfo_get(19, "topcolor"), "5")
        self.assertEqual(self.client.userinfo_get(19, "bottomcolor"), "13")

    async def test_standard_control_messages_do_not_stop_reader(self):
        await self.receive(b"\x04" + struct.pack("<I", 666)
                           + b"\x10" + struct.pack("<H", (10 << 3) | 2) + b"\x21")
        self.assertEqual(self.client.protocol.version, proto.ProtocolVersion.FITZQUAKE)
        self.assertFalse(self.client._read_task.done())

    async def test_unreliable_stats_count_successful_send(self):
        await self.client._send_unreliable(b"\x01")
        self.assertEqual(self.client._net_stats["unreliable_messages_sent"], 1)
        self.assertEqual(self.client._net_stats["packets_sent"], 1)
        self.assertEqual(self.client._net_stats["bytes_sent"], 1)

    async def test_reliable_fallback_stats_are_not_counted_twice(self):
        transport = mock.Mock(spec=["send_reliable"])
        transport.send_reliable = mock.AsyncMock()
        with mock.patch.object(self.client, "_conn", transport):
            await self.client._send_unreliable(b"\x01")
        self.assertEqual(self.client._net_stats["reliable_messages_sent"], 1)
        self.assertEqual(self.client._net_stats["packets_sent"], 1)
        self.assertEqual(self.client._net_stats["bytes_sent"], 1)

    async def test_failed_send_does_not_update_stats_or_last_tx(self):
        last_tx = self.client.last_tx_at
        with mock.patch.object(self.conn, "send", side_effect=OSError("closed")):
            with self.assertRaises(OSError):
                await self.client._send_unreliable(b"\x01")
        self.assertEqual(self.client.last_tx_at, last_tx)
        self.assertEqual(self.client._net_stats["packets_sent"], 0)

    async def test_ping_results_are_independent_of_print_chunk_boundaries(self):
        self.client._score_state[2] = {"active": True, "name": "player"}
        for name in ("player", client.ANSI_RED + "player" + client.ANSI_RESET):
            stream = f"Client ping times:\n  42 {name}\n  70 other\n"
            for size in (1, 7, len(stream)):
                with self.subTest(name=name, size=size):
                    self.client._latest_ping_by_name.clear()
                    chunks = [stream[i:i + size] for i in range(0, len(stream), size)]
                    self.client._update_pings_from_lines(chunks)
                    self.assertEqual(self.client._latest_ping_by_name, {"player": 42, "other": 70})
                    self.assertEqual(self.client._score_state[2]["ping"], 42)

    async def test_userinfo_changes_and_resets_reach_server_and_unsubscribe(self):
        cv = client.Cvar("team", "red", client.CvarFlags.USERINFO, cvar_type=client.CvarType.STRING)
        self.cvars.register(cv)
        self.client._bind_cvar_subscriptions()
        self.cvars.set("team", "blue")
        await asyncio.sleep(0)
        self.assertIn(b'\x04setinfo "team" "blue"\n\0', self.conn.sent)
        self.assertEqual(self.client.myinfo_get("team"), "blue")
        self.cvars.reset("team")
        await asyncio.sleep(0)
        self.assertIn(b'\x04setinfo "team" "red"\n\0', self.conn.sent)
        await self.client.disconnect()
        self.assertFalse(self.cvars._listeners)

    async def test_recursive_alias_reports_error_without_requeuing(self):
        self.client.aliases.update(a="b", b="a")
        client.CURRENT_CLIENT = self.client
        with mock.patch.object(client, "Cbuf_InsertText") as queue:
            await client._get_console_session().dispatcher.execute("a")
            queue.assert_not_called()
        self.assertIn("Alias expansion exceeded", client.CON.write.call_args.args[0])

    async def test_quoted_semicolon_is_not_requeued_forever(self):
        with mock.patch.object(client, "Cbuf_AddText") as queue:
            await client._get_console_session().dispatcher.execute('echo "hello; world"')
            queue.assert_not_called()
        client.CON.write.assert_called_with("hello; world")

    async def test_server_aliases_share_persistent_table_and_keep_commands_reserved(self):
        waiting = asyncio.create_task(self.client.wait_for_center_print())
        await asyncio.sleep(0)
        self.conn.incoming.put_nowait(b'\x09alias "quit" "ready"\nalias watch "cmd observe"\nquit\n\0\x1aloaded\0')
        await asyncio.wait_for(waiting, 1)
        self.assertEqual(self.cvars.command_aliases["quit_a"], "ready")
        self.assertEqual(self.cvars.command_aliases["watch"], "cmd observe")
        self.assertFalse(self.client._read_task.done())

    async def test_programmatic_client_captures_aliases_without_console_initialization(self):
        del client.cvar_system
        standalone = client.AsyncClient(FakeConnection())
        standalone._read_task = asyncio.create_task(standalone._read_messages())
        standalone._read_task.add_done_callback(standalone._reader_finished)
        try:
            waiting = asyncio.create_task(standalone.wait_for_center_print())
            await asyncio.sleep(0)
            standalone._conn.incoming.put_nowait(b'\x09alias watch "cmd observe"\n\0\x1aloaded\0')
            await asyncio.wait_for(waiting, 1)
            self.assertEqual(standalone.aliases["watch"], "cmd observe")
        finally:
            client.cvar_system = self.cvars
            await standalone.disconnect()

    async def test_received_server_prints_preserve_blank_lines_and_colon_labels(self):
        output = io.StringIO()
        console = client._ConsoleBuffer()
        waiting = asyncio.create_task(self.client.wait_for_center_print())
        await asyncio.sleep(0)
        chunks = ("\n", "match statistics\n", "\n", "map      \b:\b ", "q1q3hektik\n",
                  "\n", "\befficiency\b\n", "\n")
        packet = b"".join(b"\x08" + chunk.encode("latin-1") + b"\0" for chunk in chunks)
        with mock.patch.object(client, "CON", console), mock.patch("sys.stdout", output):
            self.conn.incoming.put_nowait(packet + b"\x1afinished\0")
            await asyncio.wait_for(waiting, 1)
        self.assertEqual(output.getvalue(), "\n  match statistics\n\n  map      : q1q3hektik\n\n  efficiency\n\n")

    async def test_console_reconnect_uses_new_client_and_cleans_up_tasks(self):
        connections = []
        background_tasks = []

        async def connect(host, port):
            connection = FakeConnection()
            active = client.AsyncClient(connection)
            active._spawned_fut.set_result(None)
            active._read_task = asyncio.create_task(active._read_messages())
            active._read_task.add_done_callback(active._reader_finished)
            connections.append((host, port, active))
            return active

        async def background(active):
            background_tasks.append(asyncio.current_task())
            await asyncio.Event().wait()

        async def input_without_thread(executor, function, prompt):
            await asyncio.sleep(0)
            return function(prompt)

        with mock.patch.object(client.AsyncClient, "connect", side_effect=connect), \
                mock.patch.object(client.CvarSystem, "load_config", return_value=False), \
                mock.patch.object(client, "_simple_keep_alive", background), \
                mock.patch.object(client, "_watch_chatinfo", background), \
                mock.patch.object(client, "_kickstart_handshake", background), \
                mock.patch.object(client, "_connection_guard", background), \
                mock.patch.object(asyncio.get_running_loop(), "run_in_executor", input_without_thread), \
                mock.patch("builtins.print"), \
                mock.patch("builtins.input", side_effect=["connect localhost:26001", "s second",
                           "reconnect", "s third", "disconnect", EOFError()]):
            await asyncio.wait_for(client._aioclient(client._parse_cli_args(["localhost"])), 2)

        self.assertEqual([(host, port) for host, port, _ in connections],
                         [("localhost", 26000), ("localhost", 26001), ("localhost", 26001)])
        self.assertIn(b"\x04say second\0", connections[1][2]._conn.sent)
        self.assertIn(b"\x04say third\0", connections[2][2]._conn.sent)
        self.assertTrue(all(active._conn.closed for _, _, active in connections))
        self.assertEqual(len(background_tasks), 12)
        self.assertTrue(all(task.done() for task in background_tasks))
        self.assertFalse(client.cvar_system._listeners)


def pq_byte(value):
    return bytes((16 + ((value >> 4) & 15), 16 + (value & 15)))


def pq_message(command, args=b"", suffix=b""):
    return b"\x09\x01" + bytes((command,)) + args + suffix + b"\0"


class ProQuakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cvars = client.CvarSystem()
        for name, value in (("cvar_system", self.cvars), ("CURRENT_CLIENT", None),
                            ("CON", mock.Mock())):
            patcher = mock.patch.object(client, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn = FakeConnection()
        self.client = client.AsyncClient(self.conn)
        self.client._read_task = asyncio.create_task(self.client._read_messages())
        self.client._read_task.add_done_callback(self.client._reader_finished)

    async def asyncTearDown(self):
        await self.client.disconnect()

    async def receive(self, message):
        waiting = asyncio.create_task(self.client.wait_for_center_print())
        await asyncio.sleep(0)
        self.conn.incoming.put_nowait(message + b"\x1aprocessed\0")
        await asyncio.wait_for(waiting, 1)

    async def print_message(self, text):
        await self.receive(b"\x08" + text.encode("latin-1") + b"\0")

    async def timer(self, minutes, seconds):
        await self.receive(pq_message(5, pq_byte(minutes) + pq_byte(seconds)))

    async def test_team_scores_signed_frags_and_lifecycle(self):
        await self.receive(pq_message(2, b"\x14\x14") + pq_message(2, b"\x1d\x1d"))
        for score in (42, -3, -32768, 32767):
            with self.subTest(score=score):
                await self.receive(pq_message(4, b"\x14" + pq_byte(score >> 8) + pq_byte(score)))
                self.assertEqual(self.client._pq_team_frags[4], score)
        await self.receive(pq_message(1))
        self.assertEqual(self.client._pq_team_frags, {4: 32767, 13: 0})
        await self.receive(pq_message(2, b"\x14\x13"))
        self.assertEqual(self.client._pq_team_frags[4], 0)
        self.assertEqual(self.client._pq_team_colors[4], 16 * 3 + 4)
        await self.receive(pq_message(3, b"\x14"))
        self.assertEqual(self.client._pq_team_frags, {13: 0})
        self.assertNotIn(4, self.client._pq_team_colors)
        await self.receive(pq_message(6))
        self.assertFalse(self.client._pq_team_frags)
        self.assertFalse(self.client._pq_team_colors)

    async def test_nop_does_not_enable_teamgame(self):
        await self.receive(pq_message(1))
        self.assertFalse(self.client._pq_teamgame_hint)

    async def test_binary_prefix_preserves_following_text_and_message_alignment(self):
        await self.receive(pq_message(2, b"\x14\x14", b"cmd ping\n")
                           + pq_message(4, b"\x14" + pq_byte(0) + pq_byte(42),
                                        b'alias watch "cmd observe"\n'))
        self.assertIn(b"\x04ping\0", self.conn.sent)
        self.assertEqual(self.client.aliases["watch"], "cmd observe")
        self.assertEqual(self.client._pq_team_frags[4], 42)

    async def test_malformed_and_unknown_payloads_do_not_change_state_or_execute_text(self):
        self.client.time = 100
        await self.timer(10, 0)
        await self.receive(pq_message(2, b"\x14\x14"))
        malformed = (b"\x09\x01\0", pq_message(2), pq_message(3),
                     pq_message(4, b"\x14\x10\x10"),
                     pq_message(4, b"\x14\x10\x10\x10\x20"),
                     pq_message(5, b"\x10\x10\x10"),
                     pq_message(5, b"\x10\x10\x10\x20"),
                     pq_message(2, b"\x1e\x14"), pq_message(3, b"\x1f"),
                     pq_message(2, b"\x14\x20"),
                     pq_message(99, b"\x14\x14", b"cmd ping\n"))
        with self.assertLogs(client.logger, level="WARNING"):
            await self.receive(b"".join(malformed))
        self.assertEqual(self.client._pq_team_frags, {4: 0})
        self.assertEqual(self.client._pq_team_colors, {4: 68})
        self.assertEqual(self.client._current_match_time(), (10, 0))
        self.assertNotIn(b"\x04ping\0", self.conn.sent)
        self.assertFalse(self.client._read_task.done())

    async def test_pause_freezes_current_time_and_resume_excludes_pause_duration(self):
        self.client.time = 100
        await self.timer(10, 0)
        self.client.time = 110
        await self.print_message("Match paused\n")
        self.assertEqual(self.client._current_match_time(), (9, 50))
        self.client.time = 120
        await self.print_message("Match paused\n")  # Repeated notification is harmless.
        self.assertEqual(self.client._current_match_time(), (9, 50))
        self.client.time = 130
        await self.print_message("Match unpaused\n")
        self.assertEqual(self.client._current_match_time(), (9, 50))
        self.client.time = 131
        await self.print_message("Match unpaused\n")
        self.assertEqual(self.client._current_match_time(), (9, 49))

    async def test_timer_sample_while_paused_remains_frozen(self):
        self.client.time = 100
        await self.timer(10, 0)
        self.client.time = 110
        await self.print_message("Match paused\n")
        self.client.time = 120
        await self.timer(9, 50)
        self.client.time = 140
        self.assertTrue(self.client._pq_paused)
        self.assertEqual(self.client._current_match_time(), (9, 50))
        await self.print_message("Match unpaused\n")
        self.client.time = 141
        self.assertEqual(self.client._current_match_time(), (9, 49))

    async def test_fractional_seconds_do_not_tick_early_and_clock_clamps_at_zero(self):
        self.client.time = 100
        await self.timer(0, 1)
        self.client.time = 100.9
        self.assertEqual(self.client._current_match_time(), (0, 1))
        self.client.time = 101
        self.assertEqual(self.client._current_match_time(), (0, 0))
        self.client.time = 200
        self.assertEqual(self.client._current_match_time(), (0, 0))

    async def test_monotonic_sample_does_not_switch_clock_source_during_signon(self):
        with mock.patch.object(self.client, "_mono_now", return_value=10) as now:
            await self.timer(10, 0)
            self.client.time = 1000
            now.return_value = 11
            self.assertEqual(self.client._current_match_time(), (9, 59))
            await self.timer(9, 59)
            self.client.time = 1001
            self.assertEqual(self.client._current_match_time(), (9, 58))

    async def test_printed_minutes_support_both_forms_and_ignore_chat_or_seconds(self):
        self.client.time = 100
        for text, minutes in (("5 minutes remaining\n", 5),
                              ("Match ends in 7 minutes remaining\n", 7),
                              ("Match ends in 3 minutes\n", 3), ("1 minute remaining\n", 1)):
            with self.subTest(text=text):
                await self.print_message(text)
                self.assertEqual(self.client._current_match_time(), (minutes, 0))
        await self.print_message("player: 5 minutes remaining\n")
        await self.print_message("Match ends in 4 seconds remaining\n")
        self.assertEqual(self.client._current_match_time(), (1, 0))

    async def test_timer_sentinels_negative_countdown_and_hidden_fallback(self):
        self.client.time = 100
        self.client._serverinfo_is["matchtime"] = "10"
        self.assertIn("10:00", self.client.format_showscores_footer())
        for minutes, seconds, display in ((254, 0, "SD"), (0, 128, "-0:00"),
                                          (0, 133, "-0:05"), (0, 187, "-0:59")):
            with self.subTest(minutes=minutes, seconds=seconds):
                await self.timer(minutes, seconds)
                self.assertIn(display, self.client.format_showscores_footer())
        for seconds in (0, 255):
            await self.timer(255, seconds)
            self.assertIsNone(self.client._current_match_time())
            self.assertEqual(self.client.format_showscores_footer().strip(), "No Teams")

    async def test_match_end_and_map_change_clear_paused_clock(self):
        self.client.time = 100
        await self.timer(10, 0)
        await self.print_message("Match paused\n")
        await self.print_message("The match is over\n")
        self.assertFalse(self.client._pq_paused)
        self.assertIsNone(self.client._pq_pause_time)
        self.assertIsNone(self.client._current_match_time())
        await self.print_message("Match paused\n")
        await self.receive(server_info())
        self.assertFalse(self.client._pq_paused)
        self.assertIsNone(self.client._pq_pause_time)
        self.assertIsNone(self.client._pq_minutes)
        self.assertFalse(self.client._pq_uses_server_time)

    async def test_move_angles_follow_negotiation_or_extended_protocol(self):
        self.client.time = 10
        for negotiated, version, fmt, angles in (
                (False, 15, "<BfBBBhhhBB", (0, 64, 192)),
                (True, 15, "<BfHHHhhhBB", (64, 16384, 49152)),
                (False, 666, "<BfHHHhhhBB", (64, 16384, 49152))):
            with self.subTest(negotiated=negotiated, version=version):
                self.conn.proquake_angle_hack = negotiated
                self.client.protocol = proto.Protocol(proto.ProtocolVersion(version), proto.ProtocolFlags(0))
                self.conn.sent.clear()
                self.client.move(math.pi / 512, math.pi / 2, -math.pi / 2, 1, -2, 3, 4, 5)
                await asyncio.sleep(0)
                self.assertEqual(struct.unpack(fmt, self.conn.sent[-1]),
                                 (3, 10, *angles, 1, -2, 3, 4, 5))
                await self.client._small_move_unreliable()
                await self.client._small_move_reliable()
                await self.client._send_heartbeat(impulse=39)
                for move in self.conn.sent[1:]:
                    self.assertEqual(struct.unpack(fmt, move)[2:5], angles)

    async def test_client_passes_cvar_or_explicit_password_before_reading_messages(self):
        self.cvars.register(client.Cvar("password", "configured", cvar_type=client.CvarType.STRING))
        for supplied, expected in ((None, "configured"), ("override", "override"), ("", "")):
            with self.subTest(supplied=supplied):
                connection = FakeConnection()
                with mock.patch.object(client.aiodgram.DatagramConnection, "connect",
                                       new=mock.AsyncMock(return_value=connection)) as connect:
                    active = await client.AsyncClient.connect("localhost", 26000, password=supplied)
                try:
                    connect.assert_awaited_once_with("localhost", 26000, None, password=expected)
                finally:
                    await active.disconnect()


class HelpersTests(unittest.TestCase):
    def test_masked_player_names_have_plain_canonical_keys(self):
        masked = "".join(chr(ord(char) | 0x80) for char in "player")
        self.assertEqual(client._sanitize_player_name(masked), "player")
        self.assertEqual(client._name_key(masked), client._name_key("player"))
        colored = client.ANSI_RED + "player" + client.ANSI_RESET
        self.assertEqual(client._sanitize_player_name(colored), "player")

    def test_commands_preserve_quotes_and_escaped_quotes(self):
        self.assertEqual(client._split_queued_commands('echo "a;b"; wait\necho done'),
                         ['echo "a;b"', 'wait', 'echo done'])
        self.assertEqual(client._split_queued_commands('alias a "say \\"a;b\\"; wait"; echo done'),
                         ['alias a "say \\"a;b\\"; wait"', 'echo done'])

    def test_cli_accepts_host_without_port_and_validates_ports(self):
        args = client._parse_cli_args(["example.test"])
        self.assertEqual((args.host, args.port), ("example.test", 26000))
        args = client._parse_cli_args(["--demo", "localhost", "26001"])
        self.assertTrue(args.demo)
        self.assertEqual(args.port, 26001)
        self.assertTrue(client._parse_cli_args(["test", "localhost"]).test)
        for port in ("0", "65536", "bad"):
            with self.subTest(port=port), mock.patch("sys.stderr", io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                client._parse_cli_args(["localhost", port])
            self.assertEqual(error.exception.code, 2)

    def test_non_finite_numeric_cvars_are_safe(self):
        cvars = client.CvarSystem()
        cv = client.Cvar("rate", "nan", cvar_type=client.CvarType.FLOAT, minimum=1, maximum=100)
        cvars.register(cv)
        self.assertEqual(cv.as_int(), 0)
        for value in ("nan", "inf", "-inf", "1e999"):
            self.assertTrue(cvars.set("rate", value))
            self.assertEqual(cv.as_float(), 1)
        self.assertTrue(cvars.set_value("rate", float("nan")))
        self.assertEqual(cv.as_float(), 1)

    def test_demo_angle_calculators_parse_protocol_and_ignore_other_baselines(self):
        demo = client.Demo()
        demo.start_recording()
        view = b"\x05\x01\x00"
        def baseline(entity, x):
            return b"\x16" + struct.pack("<HBBBBhBhBhB", entity, 0, 0, 0, 0, x, 0, 0, 0, 0, 0)
        demo.add_message((0, 0, 0), server_info() + view + baseline(1, 0) + baseline(2, 800), True)
        demo.add_message((0, 0, 0), b"\x07" + struct.pack("<f", 1) + b"\x82\x01\0\0", False)
        demo.add_message((0, 0, 0), b"\x07" + struct.pack("<f", 2) + b"\x84\x01\x20\x03", False)
        for frame in range(3, 7):
            demo.add_message((0, 0, 0), b"\x07" + struct.pack("<f", frame)
                             + b"\x84\x01" + struct.pack("<h", 800 * (frame - 1)), False)
        demo.stop_recording()
        for calculator in (client.AngleCalculatorHysteresis(math.pi / 8), client.AngleCalculatorSmoothed()):
            with self.subTest(calculator=type(calculator).__name__):
                output = io.BytesIO()
                demo.dump(output, angle_calculator=calculator)
                output.seek(0)
                parsed = list(proto.read_demo_file(output))
                self.assertAlmostEqual(parsed[-1][1][1], 90)
