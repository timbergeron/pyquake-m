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


class HelpersTests(unittest.TestCase):
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
