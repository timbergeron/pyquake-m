import asyncio
import struct
import unittest
from unittest import mock

from pyquake import aiodgram


ADDRESS = ("127.0.0.1", 26001)


def packet(flags, sequence, body=b""):
    return struct.pack(">HHI", flags, 8 + len(body), sequence) + body


def control(body):
    return struct.pack(">HH", aiodgram._NetFlags.CTL, 4 + len(body)) + body


class FakeUdp:
    def __init__(self):
        self._transport = mock.Mock()
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def wait_until_connected(self):
        pass

    async def recvfrom(self):
        return await self.incoming.get()

    def sendto(self, data, address):
        self.outgoing.put_nowait((data, address))


class DatagramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.udp = FakeUdp()
        self.conn = aiodgram.DatagramConnection(self.udp)
        self.conn._host, self.conn._port = ADDRESS
        self.conn._tasks = (asyncio.create_task(self.conn._recv_loop()),
                            asyncio.create_task(self.conn._send_reliable_loop()))
        for task in self.conn._tasks:
            task.add_done_callback(self.conn._task_done)

    async def asyncTearDown(self):
        self.conn.disconnect()
        await self.conn.wait_until_disconnected()

    def receive(self, data, address=ADDRESS):
        self.udp.incoming.put_nowait((data, address))

    async def outgoing(self):
        data, address = await asyncio.wait_for(self.udp.outgoing.get(), 1)
        self.assertEqual(address, ADDRESS)
        return data

    async def test_reliable_send_retries_lost_ack(self):
        with mock.patch.object(aiodgram, "_ACK_TIMEOUT", 0.01):
            sending = asyncio.create_task(self.conn.send_reliable(b"hello"))
            first = await self.outgoing()
            self.assertEqual(await self.outgoing(), first)
            self.receive(packet(aiodgram._NetFlags.ACK, 0))
            await asyncio.wait_for(sending, 1)

    async def test_duplicate_eom_is_acked_without_empty_message(self):
        self.receive(packet(aiodgram._NetFlags.DATA, 0, b"a"))
        self.receive(packet(aiodgram._NetFlags.DATA | aiodgram._NetFlags.EOM, 1, b"b"))
        self.receive(packet(aiodgram._NetFlags.DATA | aiodgram._NetFlags.EOM, 1, b"b"))
        self.receive(packet(aiodgram._NetFlags.DATA | aiodgram._NetFlags.EOM, 2, b"c"))
        self.assertEqual(await asyncio.wait_for(self.conn.read_message(), 1), b"ab")
        self.assertEqual(await asyncio.wait_for(self.conn.read_message(), 1), b"c")
        self.assertTrue(self.conn._message_queue.empty())
        for expected in (0, 1, 1, 2):
            self.assertEqual(await self.outgoing(), packet(aiodgram._NetFlags.ACK, expected))

    async def test_cancelled_sender_does_not_kill_reliable_loop(self):
        sending = asyncio.create_task(self.conn.send_reliable(b"first"))
        await self.outgoing()
        sending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await sending
        self.receive(packet(aiodgram._NetFlags.ACK, 0))
        sending = asyncio.create_task(self.conn.send_reliable(b"second"))
        self.assertEqual(await self.outgoing(), packet(aiodgram._NetFlags.DATA | aiodgram._NetFlags.EOM,
                                                     1, b"second"))
        self.receive(packet(aiodgram._NetFlags.ACK, 1))
        await asyncio.wait_for(sending, 1)

    async def test_disconnect_wakes_readers_and_senders(self):
        sending = asyncio.create_task(self.conn.send_reliable(b"pending"))
        reading = asyncio.create_task(self.conn.read_message())
        await self.outgoing()
        self.conn.disconnect()
        results = await asyncio.gather(sending, reading, return_exceptions=True)
        self.assertTrue(all(isinstance(result, aiodgram.DatagramError) for result in results))
        self.udp._transport.close.assert_called_once()
        with self.assertRaises(aiodgram.DatagramError):
            await self.conn.send_reliable(b"closed")

    async def test_retry_deadline_closes_connection_and_fails_waiters(self):
        with mock.patch.object(aiodgram, "_ACK_TIMEOUT", 0.005), \
                mock.patch.object(aiodgram, "_RELIABLE_ATTEMPTS", 2):
            reading = asyncio.create_task(self.conn.read_message())
            with self.assertRaisesRegex(aiodgram.DatagramError, "acknowledgement timed out"):
                await asyncio.wait_for(self.conn.send_reliable(b"lost"), 1)
            with self.assertRaises(aiodgram.DatagramError):
                await asyncio.wait_for(reading, 1)
            self.assertTrue(self.conn._closing)

    async def test_bad_and_foreign_packets_do_not_disconnect(self):
        self.receive(b"short", ("127.0.0.2", 26001))
        self.receive(b"\0")
        self.receive(struct.pack(">HH", aiodgram._NetFlags.DATA, 4))
        self.receive(packet(aiodgram._NetFlags.UNRELIABLE, 0, b"ok"))
        self.assertEqual(await asyncio.wait_for(self.conn.read_message(), 1), b"ok")

    async def test_rcon_uses_control_header_and_skips_reply_type(self):
        with mock.patch("builtins.print") as printing:
            self.receive(control(b"\x86status output\0"))
            self.receive(packet(aiodgram._NetFlags.UNRELIABLE, 0, b"alive"))
            self.assertEqual(await asyncio.wait_for(self.conn.read_message(), 1), b"alive")
            printing.assert_called_once_with("[rcon] status output")

    async def test_unreliable_skips_are_positive_and_stale_packets_are_ignored(self):
        with self.assertLogs(aiodgram.logger, level="WARNING") as logs:
            self.receive(packet(aiodgram._NetFlags.UNRELIABLE, 2, b"new"))
            self.receive(packet(aiodgram._NetFlags.UNRELIABLE, 1, b"stale"))
            self.receive(packet(aiodgram._NetFlags.UNRELIABLE, 3, b"next"))
            self.assertEqual(await asyncio.wait_for(self.conn.read_message(), 1), b"new")
            self.assertEqual(await asyncio.wait_for(self.conn.read_message(), 1), b"next")
        self.assertTrue(any("Skipped 2 unreliable" in line for line in logs.output))

    async def test_unreliable_send_sequence_wraps(self):
        self.conn._unreliable_send_seq = 0xffffffff
        self.conn.send(b"last")
        self.conn.send(b"first")
        self.assertEqual(await self.outgoing(), packet(aiodgram._NetFlags.UNRELIABLE, 0xffffffff, b"last"))
        self.assertEqual(await self.outgoing(), packet(aiodgram._NetFlags.UNRELIABLE, 0, b"first"))


class HandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.udp = FakeUdp()
        loop = asyncio.get_running_loop()
        resolver = mock.patch.object(loop, "getaddrinfo", new=mock.AsyncMock(return_value=[
            (2, 2, 17, "", ("127.0.0.1", 26000))]))
        resolver.start()
        self.addCleanup(resolver.stop)

    async def accept(self, trailer=b"", *, server_port=26001, password="", desired_version=None):
        udp = FakeUdp()
        udp.incoming.put_nowait((control(b"\x81" + struct.pack("<I", server_port) + trailer),
                                ("127.0.0.1", 26000)))
        conn = aiodgram.DatagramConnection(udp)

        async def close():
            conn.disconnect()
            await conn.wait_until_disconnected()

        self.addAsyncCleanup(close)
        await conn._connect("localhost", 26000, desired_version, password=password)
        request, address = await asyncio.wait_for(udp.outgoing.get(), 1)
        self.assertEqual(address, ("127.0.0.1", 26000))
        return conn, request

    async def test_proquake_accept_and_password_request(self):
        conn, request = await self.accept(b"\x01\x23\x00", password="password")
        self.assertEqual(request, control(b"\x01QUAKE\0\x03\x01\x23\0\xd4\x0a\x67\xe4"))
        self.assertTrue(conn.proquake_angle_hack)
        self.assertEqual(conn.proquake_version, 35)
        self.assertIsNone(conn.joequake_version)
        conn.send(b"\x01")
        data, address = await asyncio.wait_for(conn._udp.outgoing.get(), 1)
        self.assertEqual(address, ADDRESS)
        self.assertEqual(data, packet(aiodgram._NetFlags.UNRELIABLE, 0, b"\x01"))

    async def test_vanilla_and_foreign_mods_do_not_negotiate_proquake(self):
        for trailer in (b"", b"\0", b"\x02\x23\x81"):
            with self.subTest(trailer=trailer):
                conn, _ = await self.accept(trailer)
                self.assertFalse(conn.proquake_angle_hack)
                self.assertIsNone(conn.proquake_version)
                self.assertEqual((conn._host, conn._port), ADDRESS)

    async def test_ignoreport_keeps_original_destination(self):
        for advertised_port in (0, 26099, 0xffffffff):
            with self.subTest(advertised_port=advertised_port):
                conn, _ = await self.accept(b"\x01\x23\x80", server_port=advertised_port)
                self.assertEqual(conn.proquake_flags, 0x80)
                self.assertEqual(conn._port, 26000)
                conn.send(b"\x01")
                _, address = await asyncio.wait_for(conn._udp.outgoing.get(), 1)
                self.assertEqual(address, ("127.0.0.1", 26000))

    async def test_zero_port_keeps_original_destination(self):
        conn, _ = await self.accept(server_port=0)
        self.assertEqual(conn._port, 26000)

    async def test_invalid_port_is_rejected_without_ignoreport(self):
        with self.assertRaisesRegex(aiodgram.DatagramError, "Invalid server port"):
            await self.accept(b"\x01\x23\0", server_port=65536)

    async def test_cheatfree_accept_is_rejected(self):
        with self.assertRaisesRegex(aiodgram.DatagramError, "cheat-free"):
            await self.accept(b"\x01\x23\x01")

    async def test_joequake_version_option_still_requires_matching_proquake_mod(self):
        conn, request = await self.accept(b"\x01\x04\0", desired_version=4)
        self.assertTrue(conn.proquake_angle_hack)
        self.assertEqual(conn.joequake_version, 4)
        self.assertEqual(request, control(b"\x01QUAKE\0\x03\x01\x04\0\0\0\0\0"))
        for trailer in (b"", b"\x01\x23\0", b"\x02\x04\0"):
            with self.subTest(trailer=trailer), self.assertRaises(aiodgram.BadJoeQuakeVersion):
                await self.accept(trailer, desired_version=4)

    async def test_public_connect_passes_password_to_handshake(self):
        loop = asyncio.get_running_loop()
        with mock.patch.object(loop, "create_datagram_endpoint", new=mock.AsyncMock(
                return_value=(self.udp._transport, self.udp))), \
                mock.patch.object(aiodgram.DatagramConnection, "_connect", new=mock.AsyncMock()) as connect:
            conn = await aiodgram.DatagramConnection.connect("localhost", 26000, password="1234")
        connect.assert_awaited_once_with("localhost", 26000, None, password="1234")
        conn.disconnect()

    async def test_rejection_reports_server_reason(self):
        self.udp.incoming.put_nowait((control(b"\x82Server is full\0"), ("127.0.0.1", 26000)))
        conn = aiodgram.DatagramConnection(self.udp)
        with self.assertRaisesRegex(aiodgram.DatagramError, "Server is full"):
            await conn._connect("localhost", 26000)

    async def test_truncated_accept_is_reported(self):
        self.udp.incoming.put_nowait((control(b"\x81"), ("127.0.0.1", 26000)))
        conn = aiodgram.DatagramConnection(self.udp)
        with self.assertRaisesRegex(aiodgram.DatagramError, "Truncated"):
            await conn._connect("localhost", 26000)

    async def test_connect_timeout_closes_transport(self):
        loop = asyncio.get_running_loop()
        with mock.patch.object(loop, "create_datagram_endpoint", new=mock.AsyncMock(
                return_value=(self.udp._transport, self.udp))), \
                mock.patch.object(aiodgram, "_CONNECT_TIMEOUT", 0.01):
            with self.assertRaisesRegex(aiodgram.DatagramError, "timed out"):
                await aiodgram.DatagramConnection.connect("localhost", 26000)
        self.udp._transport.close.assert_called_once()
