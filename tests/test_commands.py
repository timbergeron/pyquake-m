import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from pyquake import client, proto
from pyquake.commands import quote_arg, split_commands
from pyquake.console import ConsoleSession
from test_client import FakeConnection
from test_proto import server_info


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cvars = client.CvarSystem()
        self.console = mock.Mock()
        for name, value in (("cvar_system", self.cvars), ("CURRENT_CLIENT", None),
                            ("_CONSOLE_SESSION", None), ("CON", self.console),
                            ("_IDLE_RECONNECT", client._IdleReconnectManager()),
                            ("_autosave_config", mock.Mock())):
            patch = mock.patch.object(client, name, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="pyquake-commands-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.cvars._config_file = str(self.directory / "config.cfg")
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

    async def test_demo_commands_save_named_file_and_can_record_again(self):
        path = self.directory / "match recording"
        await self.execute("cl_chatmode 1")
        await self.execute(f"record {quote_arg(path)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        await self.execute("stop")
        self.assertIsNone(self.session.demo)
        self.assertTrue(Path(str(path) + ".dem").read_bytes().endswith(b"\x02"))
        self.assertEqual(self.messages(), [])
        await self.execute(f"demo {quote_arg(self.directory / 'second.dem')}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        await self.session.disconnect()
        self.assertIsNone(self.session.demo)
        self.assertTrue((self.directory / "second.dem").is_file())

    async def test_empty_demo_does_not_replace_existing_file(self):
        path = self.directory / "existing.dem"
        path.write_bytes(b"existing recording")
        await self.execute(f"record {quote_arg(path)}; stop")
        self.assertEqual(path.read_bytes(), b"existing recording")
        self.assertIsNone(self.session.demo)

    async def test_demo_missing_directory_fails_before_recording_starts(self):
        path = self.directory / "missing" / "match.dem"
        await self.execute(f"record {quote_arg(path)}")
        self.assertIsNone(self.session.demo)
        self.assertTrue(any("Could not start demo" in line for line in self.output()))
        path.parent.mkdir()
        await self.execute(f"record {quote_arg(path)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        await self.execute("stop")
        self.assertIsNone(self.session.demo)
        self.assertTrue(path.read_bytes().endswith(b"\x02"))

    async def test_interrupted_demo_save_keeps_existing_file_and_allows_retry(self):
        path = self.directory / "match.dem"
        path.write_bytes(b"previous recording")
        await self.execute(f"record {quote_arg(path)}")
        demo = self.session.demo
        demo.add_message((0, 0, 0), server_info(), True)

        with mock.patch("pyquake.demo.os.fsync", side_effect=OSError("disk full")):
            await self.execute("stop")
        self.assertIs(self.session.demo, demo)
        self.assertEqual(path.read_bytes(), b"previous recording")
        self.assertEqual(set(self.directory.iterdir()), {path, demo.recovery_path})
        await self.execute("stop")
        self.assertIsNone(self.session.demo)
        with path.open("rb") as stream:
            messages = list(proto.read_demo_file(stream))
        self.assertEqual(messages[-1][2].msg_type, proto.ServerMessageType.DISCONNECT)

    async def test_server_disconnect_saves_demo_without_console_input(self):
        path = self.directory / "server-ended.dem"
        self.active._read_task = asyncio.create_task(self.active._read_messages())
        self.active._read_task.add_done_callback(self.active._reader_finished)
        await self.execute(f"record {quote_arg(path)}")
        self.conn.incoming.put_nowait(server_info() + b"\x19\x03\x02")
        await self.active._read_task
        await asyncio.sleep(0)
        self.assertIsNone(self.session.demo)
        with path.open("rb") as stream:
            messages = list(proto.read_demo_file(stream))
        self.assertEqual(messages[-1][2].msg_type, proto.ServerMessageType.DISCONNECT)

    async def test_demo_replace_failure_keeps_old_file_and_recovery_file(self):
        path = self.directory / "match.dem"
        path.write_bytes(b"previous recording")
        await self.execute(f"record {quote_arg(path)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        with mock.patch("pyquake.console.os.replace", side_effect=PermissionError("file in use")):
            await self.execute("stop")
        self.assertEqual(path.read_bytes(), b"previous recording")
        self.assertEqual(set(self.directory.iterdir()), {path, self.session.demo.recovery_path})
        self.assertIsNotNone(self.session.demo)
        await self.execute("stop")
        self.assertIsNone(self.session.demo)

    async def test_repeated_recordings_save_current_demo_when_reader_fails(self):
        self.active._read_task = asyncio.create_task(self.active._read_messages())
        self.active._read_task.add_done_callback(self.active._reader_finished)
        first = self.directory / "first.dem"
        second = self.directory / "second.dem"
        await self.execute(f"record {quote_arg(first)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        await self.execute(f"stop; record {quote_arg(second)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        with self.assertLogs(level="WARNING"):
            self.conn.incoming.put_nowait(b"\x07")
            await asyncio.gather(self.active._read_task, return_exceptions=True)
            await asyncio.sleep(0)
        self.assertIsNone(self.session.demo)
        self.assertEqual(sum("Demo saved" in line for line in self.output()), 2)
        for path in (first, second):
            with path.open("rb") as stream:
                messages = list(proto.read_demo_file(stream))
            self.assertEqual(messages[-1][2].msg_type, proto.ServerMessageType.DISCONNECT)

    async def test_disconnect_attempts_demo_save_once_and_keeps_failed_recording(self):
        self.active._read_task = asyncio.create_task(self.active._read_messages())
        self.active._read_task.add_done_callback(self.active._reader_finished)
        path = self.directory / "match.dem"
        await self.execute(f"record {quote_arg(path)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        with mock.patch("pyquake.console.os.replace", side_effect=OSError("disk full")) as replace:
            await self.execute("disconnect")
        replace.assert_called_once()
        self.assertIsNotNone(self.session.demo)
        self.assertFalse(path.exists())
        await self.execute("stop")
        self.assertIsNone(self.session.demo)
        self.assertTrue(path.is_file())

    async def test_markers_and_recording_status_survive_save(self):
        path = self.directory / "marked.dem"
        await self.execute(f"record {quote_arg(path)}")
        self.assertEqual(self.cvars.get_string("cl_recordingdemo"), str(path))
        await self.execute('cl_recordingdemo "cannot change"')
        self.assertEqual(self.cvars.get_string("cl_recordingdemo"), str(path))
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        await self.execute("markdemo; stop")
        self.assertEqual(self.cvars.get_string("cl_recordingdemo"), "")
        entries = json.loads((self.directory / "demomarks.json").read_text())
        self.assertEqual(entries[0]["demo"], str(path))
        with path.open("rb") as stream:
            messages = list(proto.read_demo_file(stream))
        self.assertEqual(messages[1][2].string, "//demomark\n")
        self.assertEqual(entries[0]["frame"], 2)
        await self.execute("cl_chatmode 1; markdemo")
        self.assertEqual(self.messages(), [])
        self.assertTrue(any(str(path) in line and "frame 2" in line for line in self.output()))

    async def test_short_demo_discard_preserves_old_destination(self):
        path = self.directory / "old.dem"
        path.write_bytes(b"keep this demo")
        await self.execute(f"cl_demo_minframes -10; record {quote_arg(path)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        await self.execute("markdemo; stop")
        self.assertEqual(path.read_bytes(), b"keep this demo")
        self.assertFalse((self.directory / "demomarks.json").exists())
        self.assertEqual(list(self.directory.iterdir()), [path])

    async def test_automatic_map_recordings_split_at_serverinfo(self):
        self.active._read_task = asyncio.create_task(self.active._read_messages())
        self.active._read_task.add_done_callback(self.active._reader_finished)
        paths = [self.directory / "first.dem", self.directory / "second.dem"]
        await self.execute("cl_autodemo 1")
        with mock.patch.object(self.session, "_new_demo_path", side_effect=paths):
            self.conn.incoming.put_nowait(server_info(level=b"first") + b"\x19\x03")
            for _ in range(5):
                await asyncio.sleep(0)
            first = self.session.demo
            self.assertIsNotNone(first)
            self.conn.incoming.put_nowait(server_info(level=b"second") + b"\x19\x03")
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertIsNot(self.session.demo, first)
            await self.execute("stop")
        for path, level in zip(paths, ("first", "second")):
            with path.open("rb") as stream:
                messages = [m for _, _, m in proto.read_demo_file(stream)]
            self.assertEqual([m.level_name for m in messages if m.msg_type == proto.ServerMessageType.SERVERINFO], [level])
            self.assertEqual(messages[-1].msg_type, proto.ServerMessageType.DISCONNECT)

    async def test_automatic_match_recording_ignores_chat_and_stops_on_match_end(self):
        self.active._read_task = asyncio.create_task(self.active._read_messages())
        self.active._read_task.add_done_callback(self.active._reader_finished)
        await self.execute("cl_autodemo 2")
        path = self.directory / "match.dem"
        with mock.patch.object(self.session, "_new_demo_path", return_value=path):
            for payload in (server_info() + b"\x19\x03", b"\x08\x01The match has begun!\n\0"):
                self.conn.incoming.put_nowait(payload)
                for _ in range(5):
                    await asyncio.sleep(0)
            self.assertIsNone(self.session.demo)
            self.conn.incoming.put_nowait(b"\x08The match has begun!\n\0")
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertIsNotNone(self.session.demo)
            self.conn.incoming.put_nowait(b"\x08\x01The match is over\n\0")
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertIsNotNone(self.session.demo)
            self.conn.incoming.put_nowait(b"\x08The match is over\n\0")
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertIsNone(self.session.demo)
        self.assertTrue(path.is_file())

    async def test_default_name_is_timestamped_and_avoids_existing_recovery_files(self):
        self.active.models = ["maps/dm3.bsp"]
        with mock.patch("pyquake.console.Path", side_effect=lambda name: self.directory / name), \
                mock.patch("pyquake.console.time.strftime", return_value="10-14-2026-123000"):
            first = self.session._new_demo_path()
            (first.parent / (first.stem + ".recording-crash.dem")).touch()
            second = self.session._new_demo_path()
            self.assertNotEqual(first, second)
            self.assertEqual(second.name, "dm3_10-14-2026-123000_2.dem")
            first.touch()
            (second.parent / (second.stem + ".recording-crash.dem")).touch()
            third = self.session._new_demo_path()
            self.assertEqual(third.name, "dm3_10-14-2026-123000_3.dem")

    async def test_marker_history_tracks_map_at_each_marker(self):
        path = self.directory / "maps.dem"
        self.active.models = ["maps/dm3.bsp"]
        await self.execute(f"record {quote_arg(path)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        await self.execute("markdemo")
        self.active.models = ["maps/dm6.bsp"]
        await self.execute("markdemo; stop")
        entries = json.loads((self.directory / "demomarks.json").read_text())
        self.assertEqual([entry["map"] for entry in entries], ["dm3", "dm6"])

    async def test_recoverdemo_is_local_and_refuses_the_active_recovery_file(self):
        path = self.directory / "match.dem"
        recovered = self.directory / "recovered.dem"
        await self.execute(f"cl_chatmode 1; record {quote_arg(path)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        recovery = self.session.demo.recovery_path
        await self.execute(f"recoverdemo {quote_arg(recovery)} {quote_arg(recovered)}")
        self.assertFalse(recovered.exists())
        self.session.demo.close()
        self.session.demo = None  # Model a recording left by a terminated process.
        await self.execute(f"recoverdemo {quote_arg(recovery)} {quote_arg(recovered)}")
        self.assertEqual(self.messages(), [])
        self.assertTrue(recovery.exists())
        with recovered.open("rb") as stream:
            messages = list(proto.read_demo_file(stream))
        self.assertEqual(messages[-1][2].msg_type, proto.ServerMessageType.DISCONNECT)
        # The original client still owns the unfinished object; dispose it after
        # verifying recovery rather than having teardown finish that simulation.
        self.active._demos[0].discard()

    async def test_dzip_format_is_selected_before_recording(self):
        path = self.directory / "compressed"
        await self.execute(f"cl_demo_format dz; record {quote_arg(path)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        await self.execute("stop")
        self.assertEqual(path.with_suffix(".dz").read_bytes()[:4], b"DZ\x03\x02")

    async def test_dzip_finalization_keeps_the_receive_loop_responsive(self):
        path = self.directory / "responsive.dz"
        await self.execute(f"record {quote_arg(path)}")
        demo = self.session.demo
        demo.add_message((0, 0, 0), server_info(), True)
        started, release = threading.Event(), threading.Event()
        publish = demo.publish
        def slow_publish(path):
            started.set()
            release.wait(2)
            publish(path)
        self.active._read_task = asyncio.create_task(self.active._read_messages())
        self.active._read_task.add_done_callback(self.active._reader_finished)
        with mock.patch.object(demo, "publish", side_effect=slow_publish):
            stopping = asyncio.create_task(self.execute("stop"))
            try:
                await asyncio.wait_for(asyncio.to_thread(started.wait), 3)
                self.assertFalse(stopping.done())
                waiting = asyncio.create_task(self.active.wait_for_center_print())
                await asyncio.sleep(0)
                self.conn.incoming.put_nowait(b"\x1astill receiving\0")
                self.assertEqual(await asyncio.wait_for(waiting, .5), "still receiving")
            finally:
                release.set()
                await stopping
        self.assertTrue(path.exists())

    async def test_overwriting_demo_clears_its_old_marker_history_only_after_save(self):
        path = self.directory / "repeat.dem"
        await self.execute(f"record {quote_arg(path)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        await self.execute("markdemo; stop")
        await self.execute(f"record {quote_arg(path)}")
        self.session.demo.add_message((0, 0, 0), server_info(), True)
        with mock.patch("pyquake.demo.os.replace", side_effect=OSError("cannot publish")):
            await self.execute("stop")
        self.assertEqual(len(json.loads((self.directory / "demomarks.json").read_text())), 1)
        await self.execute("stop")
        self.assertEqual(json.loads((self.directory / "demomarks.json").read_text()), [])

    async def test_cancelling_dzip_save_waits_for_the_writer_before_releasing_demo(self):
        path = self.directory / "cancelled-save.dz"
        await self.execute(f"record {quote_arg(path)}")
        demo = self.session.demo
        demo.add_message((0, 0, 0), server_info(), True)
        started, release = threading.Event(), threading.Event()
        publish = demo.publish
        def slow_publish(path):
            started.set()
            release.wait(2)
            publish(path)
        with mock.patch.object(demo, "publish", side_effect=slow_publish) as writer:
            saving = asyncio.create_task(self.session.stop_demo())
            await asyncio.wait_for(asyncio.to_thread(started.wait), 3)
            saving.cancel()
            await asyncio.sleep(0)
            saving.cancel()
            await asyncio.sleep(0)
            retry = asyncio.create_task(self.session.stop_demo())
            try:
                self.assertFalse(saving.done())
                self.assertFalse(retry.done())
            finally:
                release.set()
                await asyncio.gather(saving, retry, return_exceptions=True)
            self.assertTrue(saving.cancelled())
            self.assertEqual(writer.call_count, 1)
        self.assertIsNone(self.session.demo)
        self.assertTrue(path.exists())

    async def test_shutdown_cancellation_still_finishes_the_archive(self):
        path = self.directory / "shutdown.dz"
        await self.execute(f"record {quote_arg(path)}")
        demo = self.session.demo
        demo.add_message((0, 0, 0), server_info(), True)
        started, release = threading.Event(), threading.Event()
        publish = demo.publish
        def slow_publish(path):
            started.set()
            release.wait(2)
            publish(path)
        before = asyncio.all_tasks()
        with mock.patch.object(demo, "publish", side_effect=slow_publish):
            saving = asyncio.create_task(self.session.stop_demo())
            await asyncio.wait_for(asyncio.to_thread(started.wait), 3)
            shutdown_tasks = asyncio.all_tasks() - before
            for task in shutdown_tasks:
                task.cancel()
            release.set()
            await asyncio.wait_for(asyncio.gather(*shutdown_tasks, return_exceptions=True), 2)
            self.assertTrue(saving.cancelled())
        self.assertIsNone(self.session.demo)
        self.assertTrue(path.exists())

    async def test_download_commands_are_local_and_use_selected_source(self):
        await self.execute("cl_chatmode 1")
        with mock.patch.object(self.active.downloads, "start") as start:
            await self.execute("download dm3.bsp server")
        start.assert_called_once_with("dm3.bsp", "server")
        self.assertEqual(self.messages(), [])
        await self.execute("allow_download 0; download dm3.bsp web")
        self.assertTrue(any("downloads disabled" in line for line in self.output()))
        self.assertEqual(self.messages(), [])

    async def test_offline_web_download_and_stop_route_to_offline_manager(self):
        await self.session.disconnect()
        manager = self.session._offline_downloads
        with mock.patch.object(manager, "start") as start:
            await self.execute("cl_chatmode 1; download dm3.loc web")
        start.assert_called_once_with("dm3.loc", "web")
        manager.task = asyncio.create_task(asyncio.Event().wait())
        with mock.patch.object(manager, "cancel", wraps=manager.cancel) as cancel:
            await self.execute("stopdownload")
            cancel.assert_called_once_with(resume_signon=True)
        await asyncio.gather(manager.task, return_exceptions=True)
        self.assertEqual(self.messages(), [])

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

    async def test_styled_prompt_keeps_chat_space_editable(self):
        for mode in (0, 1, 2, 3):
            with self.subTest(mode=mode):
                self.cvars.set("cl_chatmode", str(mode))
                with mock.patch("pyquake.console.terminal_input_supported", return_value=True), \
                        mock.patch.object(self.session._terminal_input, "read", return_value=" ready") as read:
                    self.assertEqual(self.session.read_line("> "), " ready")
                read.assert_called_once_with(" " if mode in (2, 3) else "")

    async def test_styled_prompt_preserves_alias_edit_prefill(self):
        await self.execute('alias go "ready"; cl_chatmode 3')
        with mock.patch("pyquake.console.terminal_input_supported", return_value=True), \
                mock.patch.object(client, "terminal_input_supported", return_value=True), \
                mock.patch.object(self.session._terminal_input, "read", return_value="alias go ready") as read:
            await self.execute("aliasedit go")
            self.session.read_line("> ")
            read.assert_called_once_with('alias "go" "ready"')
            self.assertFalse(client._EDIT_PREFILL)
            self.assertEqual(client._TERMINAL_PREFILL, "")

    async def test_styled_completion_starts_a_new_command_after_semicolon(self):
        self.assertIn("status", self.session._complete_input("set name woods; ", "sta"))
        self.assertIn("cl_chatmode", self.session._complete_input("set ", "cl_chat"))

    async def test_chatmode_space_is_part_of_the_editable_input(self):
        readline = mock.Mock()

        def read_input(prompt):
            self.assertEqual(prompt, "> ")
            readline.set_startup_hook.call_args.args[0]()
            readline.insert_text.assert_called_once_with(" ")
            return " ready"

        for mode in (2, 3):
            with self.subTest(mode=mode):
                self.cvars.set("cl_chatmode", str(mode))
                readline.reset_mock()
                with mock.patch.object(client, "_readline", readline), \
                        mock.patch.object(client, "_rl_is_libedit", return_value=False), \
                        mock.patch("builtins.input", side_effect=read_input):
                    await self.execute(self.session.read_line("> "))
                readline.set_startup_hook.assert_called_with(None)
        self.assertEqual(self.messages(), ['say "ready"', 'say "ready"'])

    async def test_libedit_queues_space_past_its_buffer_reset(self):
        readline = mock.Mock(__file__="readline.so")
        library = mock.Mock()

        def read_input(prompt):
            readline.set_startup_hook.call_args.args[0]()
            # Direct buffer insertion is discarded when libedit starts reading.
            readline.insert_text.assert_not_called()
            library.rl_insert.assert_called_once_with(1, ord(" "))
            return " ready"

        for mode in (2, 3):
            with self.subTest(mode=mode):
                self.cvars.set("cl_chatmode", str(mode))
                readline.reset_mock()
                library.reset_mock()
                with mock.patch.object(client, "_readline", readline), \
                        mock.patch.object(client, "_rl_is_libedit", return_value=True), \
                        mock.patch("pyquake.console.ctypes.CDLL", return_value=library) as load, \
                        mock.patch("builtins.input", side_effect=read_input):
                    await self.execute(self.session.read_line("> "))
                load.assert_called_once_with("readline.so")
                readline.set_startup_hook.assert_called_with(None)
        self.assertEqual(self.messages(), ['say "ready"', 'say "ready"'])

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

    async def test_auto_demo_reconnect_saves_old_recording_and_starts_new_file(self):
        self.session.last_connect_spec = "localhost:26001"
        self.session.enable_demo = True
        first = client.AsyncClient(FakeConnection())
        second = client.AsyncClient(FakeConnection())
        for active in (first, second):
            active._spawned_fut.set_result(None)

        async def background(active):
            await asyncio.Event().wait()

        with mock.patch.object(client.AsyncClient, "connect", side_effect=[first, second]), \
                mock.patch.object(client, "_simple_keep_alive", background), \
                mock.patch.object(client, "_watch_chatinfo", background), \
                mock.patch.object(client, "_kickstart_handshake", background), \
                mock.patch.object(client, "_connection_guard", background), \
                mock.patch("pyquake.console.Path", side_effect=lambda name: self.directory / name):
            await self.execute("reconnect")
            old = self.session.demo
            old.add_message((0, 0, 0), server_info(), True)
            await self.execute("reconnect")
            self.assertTrue(old.recording_complete)
            self.assertTrue((self.directory / "pyquake.dem").is_file())
            self.assertIsNot(self.session.demo, old)
            self.assertIn(self.session.demo, second._demos)
            self.assertEqual(self.session.demo_path, self.directory / "pyquake_2.dem")

    async def test_idle_reconnect_does_not_queue_during_connection_attempt(self):
        self.enable_idle()
        self.active.disconnected = True
        self.session.connecting = True
        manager = client._IdleReconnectManager()
        with mock.patch.object(self.dispatcher, "add_text") as queue:
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

    def enable_idle(self):
        client._ensure_cl_idle_cvar()
        with mock.patch.object(client, "_ensure_idle_reconnect_task"):
            self.cvars.set("cl_idle", "1")
        self.session.last_connect_spec = "localhost:26001"

    async def idle_tick(self):
        with mock.patch.object(client.asyncio, "sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await client._IDLE_RECONNECT._loop()

    async def test_queued_idle_retry_is_cancelled_when_disabled(self):
        self.enable_idle()
        self.active.disconnected = True
        waiting, release = asyncio.Event(), asyncio.Event()

        async def wait_frame(count):
            waiting.set()
            await release.wait()

        self.dispatcher.wait_frame = wait_frame
        executing = asyncio.create_task(self.execute("wait; cl_idle 0"))
        await asyncio.wait_for(waiting.wait(), 1)
        await self.idle_tick()
        with mock.patch.object(client.AsyncClient, "connect") as connect:
            release.set()
            await asyncio.wait_for(executing, 1)
        connect.assert_not_called()
        self.assertFalse(client._IDLE_RECONNECT._pending)
        self.assertFalse(any("Attempting idle reconnect" in text for text in self.output()))

    async def test_queued_idle_retry_keeps_a_new_connection(self):
        self.enable_idle()
        self.active.disconnected = True
        replacement = client.AsyncClient(FakeConnection())
        replacement._spawned_fut.set_result(None)
        self.dispatcher.add_text("connect last")
        await self.idle_tick()

        async def background(active):
            await asyncio.Event().wait()

        with mock.patch.object(client.AsyncClient, "connect", return_value=replacement) as connect, \
                mock.patch.object(client, "_simple_keep_alive", background), \
                mock.patch.object(client, "_watch_chatinfo", background), \
                mock.patch.object(client, "_kickstart_handshake", background), \
                mock.patch.object(client, "_connection_guard", background):
            await asyncio.wait_for(self.dispatcher.flush(), 1)
        connect.assert_awaited_once_with("localhost", 26001)
        self.assertIs(client.CURRENT_CLIENT, replacement)
        self.assertFalse(replacement._conn.closed)
        self.assertFalse(client._IDLE_RECONNECT._pending)

    async def test_idle_retry_queues_once_and_times_actual_attempt(self):
        self.enable_idle()
        self.active.disconnected = True
        await self.idle_tick()
        await self.idle_tick()
        self.assertEqual(client._IDLE_RECONNECT._last_attempt, 0)
        before = client.time.monotonic()
        with mock.patch.object(client.AsyncClient, "connect", side_effect=OSError("unreachable")) as connect:
            await asyncio.wait_for(self.dispatcher.flush(), 1)
        connect.assert_awaited_once_with("localhost", 26001)
        self.assertGreaterEqual(client._IDLE_RECONNECT._last_attempt, before)
        self.assertFalse(client._IDLE_RECONNECT._pending)
        self.console.reset_mock()
        await self.idle_tick()
        self.assertFalse(client._IDLE_RECONNECT._pending)
        self.assertEqual(self.output(), [])

    async def test_changing_idle_interval_rearms_retry(self):
        self.enable_idle()
        self.active.disconnected = True
        client._IDLE_RECONNECT._last_attempt = client.time.monotonic()
        client._IDLE_RECONNECT._saw_dc = True
        with mock.patch.object(client, "_ensure_idle_reconnect_task") as ensure:
            await self.execute("cl_idle 2")
        ensure.assert_called_once()
        self.assertEqual(client._IDLE_RECONNECT._last_attempt, 0)
        await self.idle_tick()
        with mock.patch.object(client.AsyncClient, "connect", side_effect=OSError("unreachable")):
            await asyncio.wait_for(self.dispatcher.flush(), 1)
        self.assertIn("Attempting idle reconnect; will retry every 2 minutes.", self.output())

    async def test_idle_retry_waits_for_the_configured_interval(self):
        self.enable_idle()
        self.active.disconnected = True
        with mock.patch.object(client, "_ensure_idle_reconnect_task"):
            self.cvars.set("cl_idle", "2")
        manager = client._IDLE_RECONNECT
        manager._saw_dc = True
        manager._last_attempt = client.time.monotonic() - 110
        await self.idle_tick()
        self.assertFalse(manager._pending)
        manager._last_attempt = client.time.monotonic() - 130
        await self.idle_tick()
        with mock.patch.object(client.AsyncClient, "connect", side_effect=OSError("unreachable")) as connect:
            await asyncio.wait_for(self.dispatcher.flush(), 1)
        connect.assert_awaited_once_with("localhost", 26001)

    async def test_idle_without_previous_server_stays_quiet(self):
        self.enable_idle()
        self.session.last_connect_spec = None
        self.active.disconnected = True
        await self.idle_tick()
        self.assertFalse(client._IDLE_RECONNECT._pending)
        self.assertEqual(self.output(), [])

    async def test_disconnect_explains_how_to_disable_idle(self):
        self.enable_idle()
        with mock.patch.object(client.AsyncClient, "connect", side_effect=OSError("unreachable")):
            await asyncio.wait_for(self.execute("disconnect"), 1)
        self.assertIn("cl_idle is enabled and will reconnect automatically. Use cl_idle 0 to stay offline.",
                      self.output())

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
