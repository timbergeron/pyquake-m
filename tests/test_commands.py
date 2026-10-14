import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pyquake import client
from pyquake.commands import quote_arg, split_commands
from pyquake.console import ConsoleSession
from test_client import FakeConnection


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cvars = client.CvarSystem()
        self.console = mock.Mock()
        for name, value in (("cvar_system", self.cvars), ("CURRENT_CLIENT", None),
                            ("_CONSOLE_SESSION", None), ("CON", self.console),
                            ("_autosave_config", mock.Mock())):
            patch = mock.patch.object(client, name, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="pyquake-commands-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.conn = FakeConnection()
        self.active = client.AsyncClient(self.conn)
        self.active._spawned_fut.set_result(None)
        client.CURRENT_CLIENT = self.active
        self.session = client._get_console_session()
        self.dispatcher = self.session.dispatcher

    async def asyncTearDown(self):
        await self.session.close()

    def messages(self):
        return [message[1:-1].decode("ascii") for message in self.conn.sent if message[:1] == b"\x04"]

    def output(self):
        return [call.args[0] for call in self.console.write.call_args_list]

    async def execute(self, text, *, source="console"):
        await self.dispatcher.execute(text, source=source)

    async def test_default_mode_forwards_mod_commands_without_chat(self):
        await self.execute("ready; observe; custom_mod_command arg")
        self.assertEqual(self.messages(), ["ready", "observe", "custom_mod_command arg"])
        self.assertEqual(self.cvars.get_value("cl_chatmode"), 0)

    async def test_auto_chat_keeps_commands_cvars_and_aliases(self):
        await self.execute('cl_chatmode 1; seta greeting hello; alias go "ready"')
        await self.execute("hello everyone; this is chat")
        await self.execute("ready")
        await self.execute("ready;status")
        await self.execute("greeting goodbye")
        await self.execute("go")
        await self.execute("/custom_mod_command arg")
        self.assertEqual(self.messages(), ['say "hello everyone; this is chat"', "ready", "ready", "status", "ready", "custom_mod_command arg"])
        self.assertEqual(self.cvars.get_string("greeting"), "goodbye")

    async def test_chat_modes_two_and_three_preserve_leading_space(self):
        await self.execute("cl_chatmode 2")
        await self.execute("hello")
        await self.execute("ready")
        await self.execute(" ready")
        await self.execute("cl_chatmode 3")
        await self.execute("custom_mod_command")
        await self.execute(" hello")
        await self.execute(" ready")
        self.assertEqual(self.messages(), ['say "hello"', "ready", 'say "ready"', "custom_mod_command", 'say "hello"', 'say "ready"'])

    async def test_leading_space_forces_chat_in_all_enabled_modes(self):
        await self.execute('alias go ready; seta greeting hello')
        for mode in range(4):
            with self.subTest(mode=mode):
                self.conn.sent.clear()
                self.console.reset_mock()
                self.cvars.set("cl_chatmode", str(mode))
                self.cvars.set("greeting", "hello")
                for line in (" ready", " echo hello", " greeting goodbye", " go"):
                    await self.execute(line)
                if mode:
                    self.assertEqual(self.messages(), [
                        'say "ready"', 'say "echo hello"',
                        'say "greeting goodbye"', 'say "go"'])
                    self.assertEqual(self.cvars.get_string("greeting"), "hello")
                    self.assertEqual(self.output(), [])
                else:
                    self.assertEqual(self.messages(), ["ready", "ready"])
                    self.assertEqual(self.cvars.get_string("greeting"), "goodbye")
                    self.assertIn("hello", self.output())

    async def test_disconnected_input_stays_commands_in_all_chat_modes(self):
        for mode in (1, 2, 3):
            for active, alive, disconnected in ((None, True, False),
                                                (self.active, True, True),
                                                (self.active, False, False)):
                with self.subTest(mode=mode, client=active, alive=alive, disconnected=disconnected), \
                        mock.patch.object(client, "CURRENT_CLIENT", active), \
                        mock.patch.object(self.active, "_alive", alive), \
                        mock.patch.object(self.active, "disconnected", disconnected):
                    self.console.reset_mock()
                    self.cvars.set("cl_chatmode", str(mode))
                    await self.execute(" echo offline")
                    await self.execute(" custom_mod_command arg")
                    self.assertEqual(self.output(), ["offline", "(offline) custom_mod_command arg"])
        self.assertEqual(self.messages(), [])

    async def test_chat_modes_recognize_cmd_and_rcon(self):
        for mode in (1, 2):
            with self.subTest(mode=mode), \
                    mock.patch.object(self.active, "send_rcon_nq", new_callable=mock.AsyncMock) as rcon:
                self.conn.sent.clear()
                self.cvars.set("cl_chatmode", str(mode))
                await self.execute("cmd custom_mod_command arg")
                await self.execute("rcon status")
                self.assertEqual(self.messages(), ["custom_mod_command arg"])
                rcon.assert_awaited_once_with("status")

    async def test_slash_forces_commands_after_chat_prefill(self):
        for mode in (1, 2, 3):
            with self.subTest(mode=mode):
                self.conn.sent.clear()
                self.console.reset_mock()
                self.cvars.set("cl_chatmode", str(mode))
                await self.execute(" /custom_mod_command arg")
                await self.execute(" /echo local")
                self.assertEqual(self.messages(), ["custom_mod_command arg"])
                self.assertIn("local", self.output())

    async def test_scripts_and_aliases_never_inherit_automatic_chat(self):
        script = self.directory / "commands.cfg"
        script.write_text('alias go "custom_mod_command fromalias"\ngo\ncustom_mod_command fromconfig\n')
        await self.execute("cl_chatmode 1")
        await self.execute(f"exec {quote_arg(script)}")
        client.Cbuf_AddText("custom_mod_command fromqueue")
        await self.dispatcher.flush()
        self.assertEqual(self.messages(), ["custom_mod_command fromalias", "custom_mod_command fromconfig", "custom_mod_command fromqueue"])

    async def test_queued_commands_execute_local_cvar_and_alias_handlers(self):
        client.Cbuf_AddText('seta mood calm; alias go "seta mood loud; ready"; go; echo done')
        await self.dispatcher.flush()
        self.assertEqual(self.cvars.get_string("mood"), "loud")
        self.assertEqual(self.messages(), ["ready"])
        self.assertIn("done", self.output())

    async def test_commands_wait_for_network_send_in_order(self):
        started = asyncio.Event()
        release = asyncio.Event()
        original = self.active.send_command

        async def send(command):
            if command == "ready":
                started.set()
                await release.wait()
            await original(command)

        with mock.patch.object(self.active, "send_command", send):
            executing = asyncio.create_task(self.execute("ready; seta after yes; status"))
            await started.wait()
            self.assertIsNone(self.cvars.find("after"))
            release.set()
            await asyncio.wait_for(executing, 1)
        self.assertEqual(self.messages(), ["ready", "status"])
        self.assertEqual(self.cvars.get_string("after"), "yes")

    async def test_nested_exec_and_alias_wait_insert_before_remainder(self):
        outer = self.directory / "outer.cfg"
        inner = self.directory / "inner.cfg"
        outer.write_text('echo before\nexec inner.cfg\necho after\n')
        inner.write_text('seta custom yes\nalias go "echo inside; wait 2; ready"\ngo\n')
        waiting = asyncio.Event()
        release = asyncio.Event()
        frames = []

        async def wait_frame(count):
            frames.append(count)
            waiting.set()
            await release.wait()

        self.dispatcher.wait_frame = wait_frame
        executing = asyncio.create_task(self.execute(f'exec {quote_arg(outer)}; echo done'))
        await asyncio.wait_for(waiting.wait(), 1)
        self.assertEqual(frames, [2])
        self.assertNotIn("after", self.output())
        self.assertNotIn("done", self.output())
        release.set()
        await asyncio.wait_for(executing, 1)
        self.assertEqual([text for text in self.output() if text in ("before", "inside", "after", "done")],
                         ["before", "inside", "after", "done"])
        self.assertEqual(self.cvars.get_string("custom"), "yes")
        self.assertEqual(self.messages(), ["ready"])
        self.assertFalse(client.IN_CFG_EXEC)

    async def test_recursive_exec_and_aliases_are_bounded(self):
        script = self.directory / "loop.cfg"
        script.write_text('exec loop.cfg\necho survived\n')
        await self.execute(f'exec {quote_arg(script)}; alias a b; alias b a; a; ready')
        self.assertTrue(any("Recursive exec rejected" in text for text in self.output()))
        self.assertTrue(any("Alias expansion exceeded" in text for text in self.output()))
        self.assertIn("survived", self.output())
        self.assertEqual(self.messages(), ["ready"])

    async def test_comments_and_quoted_semicolons_are_data(self):
        await self.execute('echo "hello; world" // comment; quit\n// quit; ready\necho https://example.test/a; ready')
        self.assertFalse(self.dispatcher.stopped)
        self.assertIn("hello; world", self.output())
        self.assertIn("https://example.test/a", self.output())
        self.assertEqual(self.messages(), ["ready"])

    async def test_cancelled_command_waiter_does_not_cancel_queue(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def wait_frame(count):
            started.set()
            await release.wait()

        self.dispatcher.wait_frame = wait_frame
        executing = asyncio.create_task(self.execute("wait; ready"))
        await started.wait()
        executing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await executing
        release.set()
        await asyncio.wait_for(self.dispatcher.flush(), 1)
        self.assertEqual(self.messages(), ["ready"])

    async def test_aliases_can_be_edited_and_run_offline(self):
        await self.session.disconnect()
        await self.execute('alias local "seta value yes; echo offline"; local')
        self.assertEqual(self.cvars.get_string("value"), "yes")
        self.assertIn("offline", self.output())
        self.assertIn("local", client._tabcomp__all_alias_names())
        await self.execute("unalias LOCAL")
        self.assertNotIn("local", self.session.aliases)

    async def test_aliases_and_user_defined_variables_round_trip(self):
        self.dispatcher.wait_frame = mock.AsyncMock()
        path = self.directory / "settings with spaces.cfg"
        text = 'a;b "quoted" \\path'
        body = f"echo {quote_arg(text)}; wait 1; ready"
        await self.execute(f"seta custom {quote_arg(text)}; alias go {quote_arg(body)}")
        await self.execute(f"writeconfig {quote_arg(path)}")
        self.assertIn("seta custom", path.read_text())
        await self.session.close()
        self.cvars = client.CvarSystem()
        client.cvar_system = self.cvars
        self.session = client._get_console_session()
        self.dispatcher = self.session.dispatcher
        self.dispatcher.wait_frame = mock.AsyncMock()
        self.conn = FakeConnection()
        self.active = client.AsyncClient(self.conn)
        client.CURRENT_CLIENT = self.active
        await self.cvars.load_config(path)
        self.assertEqual(self.cvars.get_string("custom"), text)
        self.assertEqual(self.session.aliases["go"], body)
        await self.execute("go")
        self.assertIn(text, self.output())
        self.assertEqual(self.messages(), ["ready"])

    async def test_alias_saving_can_be_disabled(self):
        path = self.directory / "settings.cfg"
        await self.execute('alias go ready; cfg_save_aliases 0')
        self.cvars.write_variables(path)
        self.assertNotIn("alias ", path.read_text())
        self.assertIn('cfg_save_aliases "0"', path.read_text())

    async def test_builtin_alias_conflicts_are_renamed(self):
        await self.execute('alias quit "echo kept running"; quit_a')
        self.assertFalse(self.dispatcher.stopped)
        self.assertIn("kept running", self.output())
        await self.execute("unalias quit")
        self.assertNotIn("quit_a", self.session.aliases)

    async def test_alias_arguments_execute_local_commands(self):
        await self.execute('alias change "seta value $1; ready"; change "two words"')
        self.assertEqual(self.cvars.get_string("value"), "two words")
        self.assertEqual(self.messages(), ["ready"])

    async def test_alias_parameters_preserve_quoted_separators(self):
        value = 'two; words "quoted" \\path'
        await self.execute(f'alias change "seta value $1; ready"; change {quote_arg(value)}')
        self.assertEqual(self.cvars.get_string("value"), value)
        body = 'echo "$*"'
        await self.execute(f'alias print {quote_arg(body)}; print {quote_arg(value)}')
        self.assertIn(value, self.output())
        self.assertEqual(self.messages(), ["ready"])

    async def test_registered_command_takes_precedence_over_existing_alias(self):
        await self.execute('alias custom "echo alias"; cl_chatmode 1')
        handler = mock.AsyncMock()
        self.cvars.register_command("custom", handler)
        await self.execute("custom arg")
        handler.assert_awaited_once_with(["arg"])
        self.assertNotIn("alias", self.output())

    async def test_toggle_cycles_user_defined_string_cvars(self):
        await self.execute("seta enabled 0; toggle enabled; toggle enabled")
        self.assertEqual(self.cvars.get_string("enabled"), "0")
        await self.execute("toggle enabled red blue; toggle enabled red blue")
        self.assertEqual(self.cvars.get_string("enabled"), "blue")

    async def test_disconnect_releases_wait_for_frame(self):
        executing = asyncio.create_task(self.execute("wait; echo resumed"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await self.session.disconnect()
        await asyncio.wait_for(executing, 1)
        self.assertIn("resumed", self.output())

    async def test_invalid_command_does_not_drop_remainder(self):
        await self.execute('echo "unterminated\nready')
        self.assertTrue(any("No closing quotation" in text for text in self.output()))
        self.assertEqual(self.messages(), ["ready"])

    async def test_quit_in_queue_stops_remainder(self):
        await self.execute("ready; quit; status")
        self.assertTrue(self.dispatcher.stopped)
        self.assertEqual(self.messages(), ["ready"])

    async def test_mode_prefill_preserves_alias_edit_hook(self):
        await self.execute("cl_chatmode 3")
        readline = mock.Mock()
        with mock.patch.object(client, "_readline", readline), mock.patch("builtins.input", return_value=" ready"):
            self.assertEqual(self.session.read_line("> "), " ready")
        self.assertEqual(readline.set_startup_hook.call_count, 2)
        readline.reset_mock()
        with mock.patch.object(client, "_readline", readline), mock.patch.object(client, "_EDIT_PREFILL", True), \
                mock.patch("builtins.input", return_value="alias go ready"):
            self.session.read_line("> ")
        readline.set_startup_hook.assert_not_called()

    async def test_queued_reconnect_switches_clients_and_preserves_aliases(self):
        self.session.last_connect_spec = "localhost:26001"
        await self.execute('alias go "ready"')
        replacement = client.AsyncClient(FakeConnection())
        replacement._spawned_fut.set_result(None)

        async def background(active):
            await asyncio.Event().wait()

        with mock.patch.object(client.AsyncClient, "connect", return_value=replacement), \
                mock.patch.object(client, "_simple_keep_alive", background), \
                mock.patch.object(client, "_watch_chatinfo", background), \
                mock.patch.object(client, "_kickstart_handshake", background), \
                mock.patch.object(client, "_connection_guard", background):
            client.Cbuf_AddText("reconnect; go")
            await asyncio.wait_for(self.dispatcher.flush(), 1)
        self.assertTrue(self.conn.closed)
        self.assertIs(client.CURRENT_CLIENT, replacement)
        self.assertIs(replacement.aliases, self.session.aliases)
        self.assertIn(b"\x04ready\0", replacement._conn.sent)

    async def test_idle_reconnect_does_not_queue_during_connection_attempt(self):
        self.cvars.create("cl_idle", "1", cvar_type=client.CvarType.INT)
        self.session.connecting = True
        manager = client._IdleReconnectManager()
        with mock.patch.object(client, "Cbuf_AddText") as queue:
            running = asyncio.create_task(manager._loop())
            await asyncio.sleep(0)
            queue.assert_not_called()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        self.session.connecting = False

    async def test_failed_reconnect_preserves_idle_retry_timer(self):
        self.cvars.create("cl_idle", "1", cvar_type=client.CvarType.INT)
        last_attempt = client.time.monotonic()
        client._IDLE_RECONNECT._last_attempt = last_attempt
        client._IDLE_RECONNECT._saw_dc = True
        self.session.last_connect_spec = "localhost:26001"
        with mock.patch.object(client.AsyncClient, "connect", side_effect=OSError("unreachable")):
            await self.execute("reconnect")
        self.assertEqual(client._IDLE_RECONNECT._last_attempt, last_attempt)
        self.assertTrue(client._IDLE_RECONNECT._saw_dc)
        self.assertIsNone(client.CURRENT_CLIENT)

    async def test_startup_config_uses_shared_dispatcher_before_connection(self):
        config = self.directory / "startup.cfg"
        saved_settings = {
            "bottomcolor": "0", "cl_afk_idle": "25", "cl_aliasoverlap": "0",
            "cl_idle": "2", "con_logcenterprint": "1", "developer": "0",
            "name": "pyquake-m", "net_connecttimeout": "17",
            "net_hostport": "26001", "net_messagetimeout": "450", "topcolor": "0",
            "cl_chatmode": "1",
        }
        config.write_text('seta custom yes\nalias warmup "ready"\n' + ''.join(
            f'{name} "{value}"\n' for name, value in saved_settings.items()))
        startup_cvars = client.CvarSystem()
        startup_cvars._config_file = str(config)
        client.CURRENT_CLIENT = None
        settings_at_connect = {}
        replacement = None

        async def connect(host, port):
            nonlocal replacement
            settings_at_connect.update({name: startup_cvars.get_string(name)
                                        for name in saved_settings})
            replacement = client.AsyncClient(FakeConnection())
            replacement._spawned_fut.set_result(None)
            return replacement

        async def input_without_thread(executor, function, prompt):
            await asyncio.sleep(0)
            return function(prompt)

        async def background(active):
            await asyncio.Event().wait()

        with mock.patch.object(client, "CvarSystem", return_value=startup_cvars), \
                mock.patch.object(client.platform, "system", return_value="Darwin"), \
                mock.patch.object(client.AsyncClient, "connect", side_effect=connect), \
                mock.patch.object(client, "_simple_keep_alive", background), \
                mock.patch.object(client, "_watch_chatinfo", background), \
                mock.patch.object(client, "_kickstart_handshake", background), \
                mock.patch.object(client, "_connection_guard", background), \
                mock.patch.object(asyncio.get_running_loop(), "run_in_executor", input_without_thread), \
                mock.patch("builtins.input", side_effect=["warmup", "quit"]), \
                mock.patch("builtins.print") as output:
            await asyncio.wait_for(client._aioclient(client._parse_cli_args(["localhost"])), 1)
        self.assertEqual(settings_at_connect, saved_settings)
        output.assert_called_once_with("\n========= PyQuake-M Initialized =========\n")
        self.assertEqual(self.output(), ["Connecting to localhost:26000..."])
        self.assertEqual(startup_cvars.get_string("custom"), "yes")
        self.assertIn("warmup", startup_cvars.command_aliases)
        self.assertIs(startup_cvars.find("con_logcenterpint"), startup_cvars.find("con_logcenterprint"))
        self.assertIn(b"\x04ready\0", replacement._conn.sent)
        self.assertTrue(replacement._conn.closed)
        await self.active.disconnect()


class TokenizerTests(unittest.TestCase):
    def test_comment_semicolons_do_not_execute_commands(self):
        self.assertEqual(split_commands('echo a // ignore; quit\necho "//;"; ready'),
                         ["echo a", 'echo "//;"', "ready"])
