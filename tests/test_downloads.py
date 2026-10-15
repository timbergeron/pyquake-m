import asyncio
import binascii
import functools
import http.server
import io
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest import mock

from pyquake import client, proto
from pyquake.downloads import AssetStore, DownloadError, DownloadManager, asset_name, mirror_url
from test_client import FakeConnection
from test_proto import server_info


def stuff(text):
    return b"\x09" + text.encode("latin-1") + b"\0"


def data(offset, payload):
    return b"\x32" + struct.pack("<IH", offset, len(payload)) + payload


def pak(path, files):
    content = bytearray(b"PACK" + bytes(8))
    records = []
    for name, payload in files.items():
        records.append(struct.pack("<56sII", name.encode(), len(content), len(payload)))
        content.extend(payload)
    struct.pack_into("<II", content, 4, len(content), 64 * len(records))
    content.extend(b"".join(records))
    path.write_bytes(content)


class PathTests(unittest.TestCase):
    def test_normalization_and_path_rejections(self):
        self.assertEqual(asset_name("dm3.bsp", manual=True), "maps/dm3.bsp")
        self.assertEqual(asset_name("dm3.loc", manual=True), "locs/dm3.loc")
        self.assertEqual(asset_name("shot.wav", manual=True), "sound/shot.wav")
        for name in ("../maps/test.bsp", "/maps/test.bsp", "maps/../test.bsp", "maps//test.bsp",
                     "maps\\test.bsp", "maps/test.bsp;quit", "maps/test.bsp\nquit", "config.cfg",
                     "progs.dat", "https://example.com/test.bsp", "maps/test.exe", "maps/./test.bsp",
                     "maps/.hidden.bsp", "maps/.. /test.bsp", "maps/CON.bsp", "maps/aux/test.bsp"):
            with self.subTest(name=name), self.assertRaises(DownloadError):
                asset_name(name, manual=True)

    def test_qssm_mirror_layouts(self):
        self.assertEqual(mirror_url("q1tools/q1tools.github.io", "maps/ultrav.bsp"),
                         "https://raw.githubusercontent.com/q1tools/q1tools.github.io/main/maps/U/ultrav.bsp")
        self.assertEqual(mirror_url("user/repo/feature/assets", "maps/2fort.bsp"),
                         "https://raw.githubusercontent.com/user/repo/feature/assets/maps/0-9/2fort.bsp")
        self.assertEqual(mirror_url("maps.quakeworld.nu", "maps/dm3.bsp"), "https://maps.quakeworld.nu/all/dm3.bsp")
        self.assertEqual(mirror_url("maps.quakeworld.nu", "locs/dm3.loc"), "https://maps.quakeworld.nu/locs/dm3.loc")
        self.assertEqual(mirror_url("http://localhost:9999/assets", "maps/my map.bsp"),
                         "http://localhost:9999/assets/maps/my%20map.bsp")
        with self.assertRaises(DownloadError):
            mirror_url("file:///etc", "maps/dm3.bsp")

    def test_pak_lookup_and_symlink_escape(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            root = Path(temporary)
            game, outside = root / "game", root / "outside"
            game.mkdir()
            outside.mkdir()
            pak(game / "pak0.pak", {"maps/dm3.bsp": b"map", "sound/shot.wav": b"sound"})
            store = AssetStore(game)
            self.assertTrue(store.exists("maps/dm3.bsp"))
            self.assertTrue(store.exists("sound/shot.wav"))
            self.assertFalse(store.exists("maps/dm6.bsp"))
            pak(game / "pak0.pak", {"maps/dm6.bsp": b"different"})
            self.assertTrue(store.exists("maps/dm6.bsp"))
            self.assertFalse(store.exists("maps/dm3.bsp"))
            (game / "maps").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(DownloadError):
                store.stage("maps/escape.bsp")
            self.assertEqual(list(outside.iterdir()), [])

    def test_publication_preserves_a_destination_created_during_the_download(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            store = AssetStore(temporary)
            output, staging = store.stage("locs/test.loc")
            with output:
                output.write(b"downloaded")
            destination = store.destination("locs/test.loc")
            destination.write_bytes(b"installed by user")
            store.publish("locs/test.loc", staging)
            self.assertEqual(destination.read_bytes(), b"installed by user")
            self.assertFalse(staging.exists())

    def test_publication_cannot_report_a_directory_as_an_installed_asset(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            store = AssetStore(temporary)
            output, staging = store.stage("locs/test.loc")
            output.close()
            store.destination("locs/test.loc").mkdir()
            try:
                with self.assertRaises(DownloadError):
                    store.publish("locs/test.loc", staging)
            finally:
                staging.unlink(missing_ok=True)


class DownloadFixture:
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cvars = client.CvarSystem()
        self.console = mock.Mock()
        for name, value in (("cvar_system", self.cvars), ("CON", self.console), ("_autosave_config", mock.Mock())):
            patcher = mock.patch.object(client, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn = FakeConnection()
        self.active = client.AsyncClient(self.conn)
        self.manager = self.active.downloads
        self.cvars.set("cl_download_dir", str(self.root / "game"))
        self.cvars.set("cl_download_auto", "0")
        self.cvars.set("cl_web_download_url", "")
        self.cvars.set("cl_web_download_url2", "")
        self.active._read_task = asyncio.create_task(self.active._read_messages())
        self.active._read_task.add_done_callback(self.active._reader_finished)

    async def asyncTearDown(self):
        await self.active.disconnect()

    async def receive(self, payload):
        waiting = asyncio.create_task(self.active.wait_for_center_print())
        await asyncio.sleep(0)
        self.conn.incoming.put_nowait(payload + b"\x1abarrier\0")
        await asyncio.wait_for(waiting, 1)
        await asyncio.sleep(0)

    async def begin(self, name, payload):
        self.manager.server_available = True
        task = self.manager.start(name, "server")
        await asyncio.sleep(0)
        await self.receive(stuff(f'cl_downloadbegin {len(payload)} "{name}"\n'))
        return task

    async def finish(self, name, payload, crc=None):
        crc = binascii.crc_hqx(payload, 0xffff) if crc is None else crc
        await self.receive(stuff(f'cl_downloadfinished {len(payload)} {crc} "{name}"\n'))
        await asyncio.wait_for(asyncio.shield(self.manager.task), 1)

    def messages(self):
        return [value[1:-1].decode("latin-1") for value in self.conn.sent if value[:1] == b"\x04"]


class DownloadTests(DownloadFixture, unittest.IsolatedAsyncioTestCase):
    async def test_server_download_with_gaps_duplicates_and_mixed_game_messages(self):
        name, payload = "locs/test.loc", b"test location file\n"
        await self.begin(name, payload)
        await self.receive(data(5, payload[5:]) + b"\x07" + struct.pack("<f", 10))
        self.assertEqual(self.manager.server.received, 0)
        await self.receive(data(0, payload[:8]) + data(0, payload[:8]) + data(8, payload[8:]))
        await self.finish(name, payload)
        self.assertEqual((self.root / "game" / name).read_bytes(), payload)
        self.assertIn('download "locs/test.loc"', self.messages())
        self.assertIn("sv_startdownload", self.messages())
        self.assertIn(b"\x33" + struct.pack("<IH", 5, len(payload) - 5), self.conn.sent)
        self.assertEqual(self.active.time, 10)
        self.assertFalse(self.active._read_task.done())
        self.assertEqual(list(self.root.rglob(".download-*")), [])

    async def test_bad_crc_is_not_installed_and_live_reader_survives(self):
        name, payload = "locs/test.loc", b"locations\n"
        await self.begin(name, payload)
        await self.receive(data(0, payload))
        await self.finish(name, payload, crc=0)
        self.assertFalse((self.root / "game" / name).exists())
        self.assertEqual(list(self.root.rglob(".download-*")), [])
        self.assertFalse(self.active._read_task.done())
        self.assertTrue(any("CRC mismatch" in call.args[0] for call in self.console.write.call_args_list))

    async def test_unrequested_and_out_of_range_data_never_create_files(self):
        await self.receive(stuff('cl_downloadbegin 3 "locs/unrequested.loc"\n') + data(0, b"bad"))
        self.assertFalse((self.root / "game").exists())
        await self.begin("locs/test.loc", b"good")
        await self.receive(data(3, b"too much"))
        await self.manager.task
        self.assertFalse((self.root / "game/locs/test.loc").exists())
        self.assertFalse(self.active._read_task.done())

    async def test_server_refusal_and_timeout_clean_up_without_disconnect(self):
        self.manager.server_available = True
        task = self.manager.start("locs/missing.loc", "server")
        await asyncio.sleep(0)
        await self.receive(stuff("stopdownload\n"))
        await task
        self.cvars.set("cl_download_timeout", "1")
        await asyncio.wait_for(self.manager.start("locs/timeout.loc", "server"), 2)
        self.assertFalse(self.active.disconnected)
        self.assertEqual(list(self.root.rglob(".download-*")), [])

    async def test_download_transport_is_removed_from_recorded_demo(self):
        await self.receive(server_info() + b"\x19\x03")
        demo = self.active.record_demo()
        payload = b"download only content"
        await self.begin("locs/test.loc", payload)
        await self.receive(data(0, payload) + b"\x07" + struct.pack("<f", 20))
        await self.finish("locs/test.loc", payload)
        demo.stop_recording()
        output = io.BytesIO()
        demo.dump(output)
        raw = output.getvalue()
        self.assertNotIn(payload, raw)
        self.assertNotIn(b"cl_downloadbegin", raw)
        self.assertNotIn(b"cl_downloadfinished", raw)
        output.seek(0)
        self.assertTrue(any(m.msg_type == proto.ServerMessageType.TIME and m.time == 20
                            for _, _, m in proto.read_demo_file(output)))

    async def test_signon_waits_for_assets_and_resumes_after_failure_or_user_cancel(self):
        self.cvars.set("cl_download_auto", "1")
        await self.receive(stuff("cl_serverextension_download 1\n") + server_info())
        self.active.models = ["maps/test.bsp", "*1"]
        self.active.sounds = []
        await self.receive(b"\x19\x01")
        self.assertNotIn("prespawn", self.messages())
        await self.receive(stuff("stopdownload\n"))
        await self.manager.task
        self.assertIn("prespawn", self.messages())
        self.conn.sent.clear()
        self.manager.signon(["maps/another.bsp"], [])
        self.manager.cancel(resume_signon=True)  # Cancel even before the task starts.
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(self.messages().count("prespawn"), 1)

    async def test_map_change_cancels_download_without_sending_old_prespawn(self):
        self.cvars.set("cl_download_auto", "1")
        self.manager.server_available = True
        self.manager.signon(["maps/old.bsp"], [])
        await asyncio.sleep(0)
        await self.receive(server_info(level=b"new"))
        await asyncio.sleep(0)
        self.assertNotIn("prespawn", self.messages())
        self.assertIsNone(self.manager.server)

    async def test_disk_write_failure_aborts_the_file_without_killing_reader(self):
        await self.begin("locs/test.loc", b"payload")
        transfer = self.manager.server
        with mock.patch.object(transfer.output, "write", side_effect=OSError("disk full")):
            await self.receive(data(0, b"payload"))
        await self.manager.task
        self.assertFalse((self.root / "game/locs/test.loc").exists())
        self.assertFalse(self.active._read_task.done())

    async def test_disabled_downloads_and_duplicate_requests(self):
        self.cvars.set("allow_download", "0")
        with self.assertRaises(DownloadError):
            self.manager.start("test.loc", "server")
        self.cvars.set("allow_download", "2")
        await self.begin("locs/test.loc", b"payload")
        with self.assertRaises(DownloadError):
            self.manager.start("another.loc", "server")
        await self.manager.close()
        self.assertEqual(list(self.root.rglob(".download-*")), [])

    async def test_invalid_download_directory_does_not_disconnect_during_signon(self):
        self.cvars.set("cl_download_auto", "1")
        loop = self.root / "loop"
        loop.symlink_to(loop)
        self.cvars.set("cl_download_dir", str(loop))
        await self.receive(server_info())
        self.active.models = ["maps/test.bsp"]
        self.conn.incoming.put_nowait(b"\x19\x01")
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertFalse(self.active._read_task.done())
        self.assertIn("prespawn", self.messages())
        self.cvars.set("cl_download_dir", "bad\0directory")
        self.assertFalse(self.manager.signon(["maps/test.bsp"], []))

    async def test_close_waits_for_cancelled_jobs_from_previous_maps(self):
        self.cvars.set("cl_download_auto", "1")
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def web(name, *args):
            if name == "maps/old.bsp":
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                    raise
                finally:
                    finished.set()
        self.cvars.set("cl_web_download_url", "https://example.test")
        with mock.patch.object(self.manager, "_web", side_effect=web):
            self.manager.signon(["maps/old.bsp"], [])
            await started.wait()
            self.manager.cancel()
            await asyncio.sleep(0)
            self.manager.signon(["maps/new.bsp"], [])
            await self.manager.task
            closing = asyncio.create_task(self.manager.close())
            for _ in range(5):
                await asyncio.sleep(0)
            try:
                self.assertFalse(closing.done())
            finally:
                release.set()
                await closing
                await asyncio.wait_for(finished.wait(), 1)

    async def test_semicolon_download_controls_preserve_other_stufftext(self):
        self.manager.server_available = True
        task = self.manager.start("locs/test.loc", "server")
        await asyncio.sleep(0)
        await self.receive(stuff('alias keep "say preserved"; cl_downloadbegin 4 "locs/test.loc"\n'))
        await self.receive(data(0, b"test"))
        await self.finish("locs/test.loc", b"test")
        await task
        self.assertEqual((self.root / "game/locs/test.loc").read_bytes(), b"test")
        self.assertEqual(self.active.aliases["keep"], "say preserved")

    async def test_comment_or_quoted_download_text_does_not_control_a_transfer(self):
        text = '// ordinary comment; stopdownload\nalias test "echo stopdownload; cl_downloadbegin"\n'
        retained, changed = await self.manager.filter_stufftext(text)
        self.assertFalse(changed)
        self.assertEqual(retained, text)
        self.assertFalse(self.manager.server_available)

    async def test_short_server_write_fails_without_acknowledging_the_packet(self):
        await self.begin("locs/test.loc", b"payload")
        transfer = self.manager.server
        with mock.patch.object(transfer.output, "write", return_value=1):
            await self.receive(data(0, b"payload"))
        await self.manager.task
        self.assertFalse((self.root / "game/locs/test.loc").exists())
        self.assertNotIn(b"\x33" + struct.pack("<IH", 0, 7), self.conn.sent)

    async def test_flush_failure_still_removes_partial_download_and_releases_transfer(self):
        name, payload = "locs/test.loc", b"locations"
        await self.begin(name, payload)
        transfer = self.manager.server
        await self.receive(data(0, payload))
        close = transfer.output.close
        def failed_close():
            close()
            raise OSError("delayed disk write failure")
        with mock.patch.object(transfer.output, "close", side_effect=failed_close):
            await self.finish(name, payload)
        self.assertFalse((self.root / "game" / name).exists())
        self.assertFalse(transfer.temporary.exists())
        self.assertIsNone(self.manager.server)
        self.assertFalse(self.active._read_task.done())


class WebTests(DownloadFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.web = self.root / "web"
        self.web.mkdir()
        started, release = self.slow_started, self.slow_release = threading.Event(), threading.Event()
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path in ("/maps/large.bsp", "/maps/truncated.bsp", "/locs/slow.loc"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(2 * 1024 * 1024 if "large" in self.path else 1024))
                    self.end_headers()
                    try:
                        self.wfile.write(struct.pack("<I", 29))
                        self.wfile.flush()
                        if "slow" in self.path:
                            started.set()
                            release.wait(3)
                            self.wfile.write(b"x" * 1020)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    self.close_connection = True
                    return
                super().do_GET()
        handler = functools.partial(Handler, directory=str(self.web))
        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.cvars.set("cl_web_download_url", f"http://127.0.0.1:{self.http.server_port}")

    async def asyncTearDown(self):
        self.slow_release.set()
        await super().asyncTearDown()
        await asyncio.to_thread(self.http.shutdown)
        self.http.server_close()
        self.thread.join()

    async def test_http_download_and_existing_asset_skip(self):
        (self.web / "maps").mkdir()
        payload = struct.pack("<I", 29) + bytes(120) + b"map data"
        (self.web / "maps/test.bsp").write_bytes(payload)
        await asyncio.wait_for(self.manager.start("test.bsp", "web"), 3)
        self.assertEqual((self.root / "game/maps/test.bsp").read_bytes(), payload)
        self.assertIsNone(self.manager.start("test.bsp", "web"))

    async def test_web_failure_falls_back_to_qssm_server(self):
        self.manager.server_available = True
        payload = b"server fallback\n"
        task = self.manager.start("locs/fallback.loc")
        for _ in range(100):
            if self.manager.server is not None:
                break
            await asyncio.sleep(.01)
        self.assertIsNotNone(self.manager.server)
        await self.receive(stuff(f'cl_downloadbegin {len(payload)} "locs/fallback.loc"\n') + data(0, payload))
        await self.finish("locs/fallback.loc", payload)
        await task
        self.assertEqual((self.root / "game/locs/fallback.loc").read_bytes(), payload)

    async def test_html_success_page_and_bad_map_headers_are_not_installed(self):
        (self.web / "maps").mkdir()
        for payload in (b"<html>missing</html>", b"invalid map", struct.pack("<I", 29),
                        struct.pack("<Iii", 29, 124, 9999999) + bytes(112)):
            (self.web / "maps/test.bsp").write_bytes(payload)
            await self.manager.start("maps/test.bsp", "web")
            self.assertFalse((self.root / "game/maps/test.bsp").exists())
            self.assertEqual(list(self.root.rglob(".download-*")), [])

    async def test_secondary_mirror_after_primary_404(self):
        self.cvars.set("cl_web_download_url2", self.cvars.get_string("cl_web_download_url"))
        self.cvars.set("cl_web_download_url", self.cvars.get_string("cl_web_download_url") + "/missing")
        (self.web / "locs").mkdir()
        (self.web / "locs/test.loc").write_text("location data")
        await self.manager.start("test.loc", "web")
        self.assertEqual((self.root / "game/locs/test.loc").read_text(), "location data")

    async def test_size_limit_and_truncated_http_response(self):
        self.cvars.set("cl_download_maxsize", "1")
        for name in ("maps/large.bsp", "maps/truncated.bsp"):
            await self.manager.start(name, "web")
            self.assertFalse((self.root / "game" / name).exists())
        self.assertEqual(list(self.root.rglob(".download-*")), [])

    async def test_cancelled_http_request_never_publishes_late_completion(self):
        loop = asyncio.get_running_loop()
        handler, errors = loop.get_exception_handler(), []
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        self.addCleanup(loop.set_exception_handler, handler)
        task = self.manager.start("locs/slow.loc", "web")
        await asyncio.wait_for(asyncio.to_thread(self.slow_started.wait), 2)
        self.manager.cancel()
        await asyncio.sleep(0)
        self.manager.cancel()  # Disconnect/close may cancel an already stopping job.
        self.slow_release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        self.assertFalse((self.root / "game/locs/slow.loc").exists())
        self.assertEqual(list(self.root.rglob(".download-*")), [])
        await asyncio.sleep(0)
        self.assertEqual(errors, [])
