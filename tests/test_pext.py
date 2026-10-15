import asyncio
import dataclasses
import io
import math
import struct
import unittest

from pyquake import proto
import test_client
import test_aiodgram


FTE = proto.Protocol(proto.ProtocolVersion.FITZQUAKE, proto.ProtocolFlags(0),
                     pext2=proto.PEXT2_SUPPORTED)


def info(bits=proto.PEXT2_SUPPORTED, version=666):
    header = b'\x0b' + struct.pack('<III', proto.PROTOCOL_FTE_PEXT2, bits, version)
    if version == 999:
        header += struct.pack('<I', 0)
    if bits & proto.PEXT2_PREDINFO:
        header += b'id1\0'
    return header + b'\x10\x01test\0maps/test.bsp\0\0sound.wav\0\0'


def delta(bits, data=b''):
    return proto._encode_fte_bits(bits) + data


def frame(updates=b'', time=1., ack=0, bits=proto.PEXT2_SUPPORTED):
    header = b'\x56' + (struct.pack('<H', ack) if bits & proto.PEXT2_PREDINFO else b'')
    return header + struct.pack('<f', time) + updates + b'\0\0'


class PextProtocolTests(unittest.TestCase):
    def parse(self, data, protocol=FTE):
        message, tail = proto.ServerMessage.parse_message(data + b'\x01', protocol)
        self.assertEqual(tail, b'\x01')
        return message

    def test_negotiation_uses_protocol_identifier_and_mask_pairs(self):
        self.assertEqual(proto.pext_command().split(),
                         ['pext', str(int.from_bytes(b'FTEX', 'little')), '0',
                          str(int.from_bytes(b'FTE2', 'little')), '104'])

    def test_headers_for_all_base_protocols_and_dialects(self):
        for version in (15, 666, 999):
            for bits in (8, 40, 104):
                with self.subTest(version=version, bits=bits):
                    message = self.parse(info(bits, version))
                    self.assertEqual(message.protocol.pext2, bits)
                    self.assertEqual(message.protocol.name, f'fte{version}')
                    self.assertEqual(message.gamedir, 'id1' if bits & 32 else '')
                    self.assertEqual(message.models, ['maps/test.bsp'])
        # Both header ordering and a zero FTEX mask are valid.
        message = self.parse(info()[:1] + struct.pack('<II', proto.PROTOCOL_FTE_PEXT1, 0) + info()[1:])
        self.assertEqual(message.protocol, FTE)

    def test_unknown_and_inconsistent_extensions_are_rejected(self):
        for bits in (2, 32, 128):
            with self.subTest(bits=bits), self.assertRaises(proto.UnsupportedProtocol):
                self.parse(info(bits))
        with self.assertRaises(proto.MalformedNetworkData):
            self.parse(info()[:1] + info()[1:9] + info()[1:])
        with self.assertRaises(proto.MalformedNetworkData):
            self.parse(frame(), proto.Protocol(proto.ProtocolVersion.FITZQUAKE, proto.ProtocolFlags(0)))

    def test_delta_order_signed_velocity_and_extended_values(self):
        bits = (1 | 2 | 4 | 8 | 16 | 32 | 64 | 256 | 512 | 1024 | 2048 | 4096
                | 8192 | 16384 | (1 << 16) | (1 << 17) | (1 << 29))
        data = (struct.pack('<HhhhBBBIBBhhhHHBBIBBB',
                            600, 8, -16, 24, 32, 64, 128, 0x12345678,
                            56, 3, -80, 160, -240, 700, 500, 2,
                            32, 0x12345678, 128, 127, 16))
        message = self.parse(frame(b'\x01\0' + delta(bits, data), time=4., ack=65535))
        state = message.updates[0][1]
        self.assertTrue(state.reset)
        self.assertEqual(state.values['frame'], 600)
        self.assertEqual(state.values['model_num'], 700)
        self.assertEqual(state.values['skin'], 500)
        self.assertEqual(state.values['originxy'], (1, -2))
        self.assertEqual(state.values['velocity'], (-10, 20, -30))
        self.assertEqual(state.values['effects'], 0x12345678)
        self.assertEqual((message.time, message.input_ack), (4., 65535))

    def test_extended_entity_numbers_and_world_reset(self):
        number = (17 << 14) | 5
        message = self.parse(frame(b'\0\x80' + proto.encode_fte_entity_number(number) + b'\0'
                                  + struct.pack('<HB', 0xc005, 17)))
        self.assertEqual([n for n, _ in message.updates], [0, number, number])
        self.assertIsNone(message.updates[-1][1])

    def test_baseline_and_tag_use_different_extended_entity_encoding(self):
        entity = 0x12345
        encoded = struct.pack('<HB', 0x8000 | (entity >> 8), entity & 255)
        message = self.parse(b'\x42' + encoded + delta(1 << 20, encoded + b'\x07'))
        self.assertEqual(message.entity_num, entity)
        self.assertEqual(message.delta.values['tag'], (entity, 7))

    def test_older_delta_dialect_uses_short_predicted_angles(self):
        protocol = dataclasses.replace(FTE, pext2=8)
        message = self.parse(frame(b'\x01\0' + delta(8 | 16 | 64,
                            struct.pack('<hhhBB', 8192, -8192, 16384, 128, 129) + b'\x02'), bits=8), protocol)
        self.assertEqual(message.updates[0][1].values['anglesxz'], (math.pi / 4, -math.pi / 4))

    def test_size_encodings_validate_and_preserve_following_bytes(self):
        for encoding, payload in ((0, b''), (1, b''), (2, b''), (3, b''),
                                   (16, b'\x12\x34'), (32, b'\x12\x34\x56\x78')):
            self.parse(frame(b'\x01\0' + delta(8192, bytes([encoding]) + payload)))
        with self.assertRaises(proto.MalformedNetworkData):
            self.parse(frame(b'\x01\0' + delta(8192, b'\x05')))
        self.parse(frame(b'\x01\0' + delta(8192, b'\x12\x34'), bits=8), dataclasses.replace(FTE, pext2=8))

    def test_render_fields_are_consumed_and_retained_for_demo_snapshot(self):
        bits = sum(1 << n for n in (18, 19, 20, 21, 22, 24, 25, 26, 27, 28))
        data = (b'\xc0\x01' + bytes(14) + b'\x02\x03\0' + b'\x07\xff'
                + b'\x01\0\x02' + bytes(10) + b'\x01\x80\x02\0'
                + bytes(3 + 5 + 1 + 1 + 2))
        parsed = self.parse(frame(b'\x01\0' + delta(bits, data))).updates[0][1]
        state = proto.merge_fte_delta(None, None, parsed)
        encoded = proto.encode_fte_delta(state, FTE)
        reparsed = self.parse(frame(b'\x01\0' + encoded)).updates[0][1]
        for name in parsed.payloads:
            self.assertEqual(reparsed.payloads[name], parsed.payloads[name])

    def test_snapshot_normalizes_mixed_index_widths_and_clears_old_velocity(self):
        first = self.parse(frame(b'\x01\0' + delta(1 | 512 | 64,
                            struct.pack('<HBhh', 600, 16, 80, 160)))).updates[0][1]
        state = proto.merge_fte_delta(None, None, first)
        second = self.parse(frame(b'\x01\0' + delta(1024, b'\x05'))).updates[0][1]
        state = proto.merge_fte_delta(state, None, second)
        snapshot = self.parse(frame(b'\x01\0' + proto.encode_fte_delta(state, FTE))).updates[0][1]
        self.assertEqual(snapshot.values['frame'], 600)
        self.assertEqual(snapshot.values['model_num'], 5)
        self.assertEqual(snapshot.values['velocity'], (0., 0., 0.))

    def test_sound_extended_flags_consume_optional_fields(self):
        # Moreflags value 1 adds velocity (bit 8).
        # Payload: pitch byte, time short, velocity shorts, DP pitch short,
        # entity short/channel byte, sound short, coordinates.
        sound = b'\x06\xfc\x01' + struct.pack('<BhhhhhHBHhhh',
                    110, -3, 8, -16, 24, 100, 5000, 3, 600, 8, -16, 24)
        message = self.parse(sound)
        self.assertEqual((message.entity_num, message.channel, message.sound_num), (5000, 3, 600))
        self.assertEqual(message.pos, (1, -2, 3))

    def test_stats_time_and_version_preserve_negotiated_protocol(self):
        for data, value in ((b'\x33\x03\xff', 255), (b'\x4f\x03' + struct.pack('<f', -2.5), -2.5),
                            (b'\x4e\x03hello\0', 'hello')):
            self.assertEqual(self.parse(data).value, value)
        self.assertEqual(self.parse(b'\x07' + struct.pack('<fH', 2., 123)).input_ack, 123)
        self.assertEqual(self.parse(b'\x04' + struct.pack('<I', 666)).protocol, FTE)

    def test_snapshot_preserves_old_dialect_angles_without_prior_prediction(self):
        protocol = dataclasses.replace(FTE, pext2=8)
        parsed = self.parse(frame(b'\x01\0' + delta(8 | 16, b'\x20\x40\x80'), bits=8), protocol)
        state = proto.merge_fte_delta(None, None, parsed.updates[0][1])
        encoded = proto.encode_fte_delta(state, protocol)
        snapshot = self.parse(frame(b'\x01\0' + encoded, bits=8), protocol)
        self.assertEqual(snapshot.updates[0][1].values['anglesxz'], state.values['anglesxz'])
        self.assertAlmostEqual(math.remainder(snapshot.updates[0][1].values['anglesy']
                                             - state.values['anglesy'], math.tau), 0.)

    def test_precaches_particles_and_static_sounds_preserve_packet_boundaries(self):
        self.assertEqual(self.parse(b'\x36\x02\x80new.wav\0').name, 'new.wav')
        self.parse(b'\x15' + delta(1024, b'\x03'))
        self.parse(b'\x3b' + struct.pack('<hhhHBB', 8, 16, 24, 500, 255, 64))
        self.parse(b'\x3c' + struct.pack('<HHhhhhhh', 0x8001, 3, 8, 16, 24, 32, 40, 48))
        self.parse(b'\x3d' + struct.pack('<HhhhhhhH', 3, 8, 16, 24, 32, 40, 48, 2))
        self.parse(b'\x3e' + struct.pack('<Hhhh', 3, 8, 16, 24))

    def test_all_truncated_headers_frames_and_baselines_raise_protocol_errors(self):
        samples = (info(), frame(b'\x01\0' + delta(256 | 2 | 1024, struct.pack('<hhB', 8, -16, 3))),
                   b'\x42\x01\0' + delta(1024, b'\x03'))
        for data in samples:
            for length in range(1, len(data)):
                with self.subTest(data=data[0], length=length), self.assertRaises(proto.MalformedNetworkData):
                    proto.ServerMessage.parse_message(data[:length], FTE)


class PextClientTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_client.ClientTests.asyncSetUp
    asyncTearDown = test_client.ClientTests.asyncTearDown
    receive = test_client.ClientTests.receive

    async def test_negotiation_and_fallback_banner(self):
        await self.receive(b'\x09cmd pext\n\0')
        self.assertIn(b'\x04' + proto.pext_command().encode() + b'\n\0', self.conn.sent)
        from pyquake import client
        from test_proto import server_info
        await self.receive(server_info(666))
        client.CON.write.assert_any_call('  Using protocol 666')
        self.assertEqual(self.client.protocol.pext2, 0)

    async def test_persistent_entities_removals_resets_stats_and_acks(self):
        await self.receive(info() + b'\x05\x01\0\x19\x03')
        self.assertIn(b'\x32\xff\xff\xff\xff', self.conn.sent)
        self.conn.last_message_sequence = 42
        await self.receive(b'\x33\x00\x64\x4f\x10' + struct.pack('<f', 22.)
                           + frame(b'\x01\0' + delta(256 | 2 | 1024 | 16384,
                                             struct.pack('<hhBB', 8, 16, 3, 128))))
        self.assertEqual(self.client.entities[1].origin, (1, 2, 0))
        self.assertEqual(self.client.stats[0], 100)
        self.assertEqual(self.client.view_height, 22.)
        self.assertTrue(self.client.on_ground)
        self.assertIn(b'\x32\x2a\0\0\0', self.conn.sent)
        await self.receive(frame(b'\x02\0' + delta(1024, b'\x04'), time=2.))
        self.assertEqual(set(self.client.entities), {1, 2})
        await self.receive(frame(b'\x01\x80', time=3.))
        self.assertEqual(set(self.client.entities), {2})
        await self.receive(frame(b'\0\x80', time=4.))
        self.assertFalse(self.client.entities)

    async def test_predinfo_input_sequence_and_map_reset(self):
        await self.receive(info() + b'\x19\x03')
        self.client._move_sequence = 65535
        await self.client._send_heartbeat()
        await self.client._small_move_reliable()
        self.assertEqual(struct.unpack_from('<H', self.conn.sent[-2], 1)[0], 65535)
        self.assertEqual(struct.unpack_from('<H', self.conn.sent[-1], 1)[0], 0)
        self.assertEqual(len(self.conn.sent[-1]), 21)
        await self.receive(info())
        self.assertEqual(self.client._move_sequence, 0)
        self.assertFalse(self.client.stats)
        self.assertFalse(self.client._fte_entities)

    async def test_bad_packet_does_not_ack_a_partially_parsed_frame(self):
        await self.receive(info())
        self.conn.last_message_sequence = 42
        with self.assertLogs(level='WARNING'):
            self.conn.incoming.put_nowait(frame() + b'\x7f')
            await asyncio.gather(self.client._read_task, return_exceptions=True)
            await asyncio.sleep(0)
        self.assertNotIn(b'\x32\x2a\0\0\0', self.conn.sent)
        self.assertTrue(self.client.disconnected)

    async def test_float_angle_inputs_include_predinfo_sequence(self):
        from pyquake import client
        body = client._make_move_body(math.pi / 2, -math.pi / 2, math.pi,
                                      100, -100, 0, 2, 3, True, 4., input_sequence=5,
                                      protocol_flags=proto.ProtocolFlags.FLOATANGLE)
        values = struct.unpack('<BHffffhhhBB', body)
        self.assertEqual(values, (3, 5, 4., 90., -90., 180., 100, -100, 0, 2, 3))

    async def test_mid_map_demo_includes_complete_entity_and_latest_stat(self):
        await self.receive(info() + b'\x19\x03')
        await self.receive(b'\x33\x03\x14' + frame(b'\x01\0' + delta(256 | 2 | 1024,
                                             struct.pack('<hhB', 8, 16, 3))))
        await self.receive(b'\x4f\x03' + struct.pack('<f', 17.)
                           + frame(b'\x01\0' + delta(4, struct.pack('<h', 24)), time=2.))
        demo = self.client.record_demo()
        demo.stop_recording()
        output = io.BytesIO()
        demo.dump(output)
        output.seek(0)
        messages = [m for _, _, m in proto.read_demo_file(output)]
        stats = [m.value for m in messages if m.msg_type in
                 (proto.ServerMessageType.UPDATESTATBYTE, proto.ServerMessageType.UPDATESTATFLOAT)]
        self.assertEqual(stats, [17.])
        snapshots = [m for m in messages if m.msg_type == proto.ServerMessageType.UPDATEENTITIES]
        self.assertEqual(snapshots[-2].updates, [(0, None)])
        entity = snapshots[-1].updates[0][1]
        self.assertEqual(entity.values['originxy'], (1, 2))
        self.assertEqual(entity.values['originz'], 3)
        self.assertEqual(entity.values['model_num'], 3)

    async def test_baseline_reset_and_dynamic_precache_static_survive_demo_start(self):
        await self.receive(info() + b'\x42\x01\0' + delta(1024 | 2, struct.pack('<hhB', 8, 16, 3))
                           + b'\x19\x03')
        await self.receive(b'\x36\x02\x00progs/new.mdl\0\x15' + delta(1024, b'\x02')
                           + frame(b'\x01\0' + delta(256 | 4, struct.pack('<h', 24))))
        self.assertEqual(self.client.entities[1].origin, (1, 2, 3))
        self.assertEqual(self.client.entities[1].model_num, 3)
        self.assertEqual(self.client.models[1], 'progs/new.mdl')
        demo = self.client.record_demo()
        demo.stop_recording()
        output = io.BytesIO()
        demo.dump(output)
        output.seek(0)
        messages = [m for _, _, m in proto.read_demo_file(output)]
        kinds = [m.msg_type for m in messages]
        self.assertLess(kinds.index(proto.ServerMessageType.PRECACHE), kinds.index(proto.ServerMessageType.FTE_SPAWNSTATIC))
        self.assertEqual(messages[-2].updates[0][1].values['originxy'], (1, 2))


class PextTransportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_aiodgram.DatagramTests.asyncSetUp
    asyncTearDown = test_aiodgram.DatagramTests.asyncTearDown
    receive = test_aiodgram.DatagramTests.receive
    outgoing = test_aiodgram.DatagramTests.outgoing

    async def test_sequence_metadata_tracks_each_queued_message(self):
        self.receive(test_aiodgram.packet(16, 0, b'first'))
        self.receive(test_aiodgram.packet(16, 1, b'second'))
        self.receive(test_aiodgram.packet(9, 0, b'reliable'))
        await self.outgoing()  # reliable ACK means all three messages are queued
        for expected, sequence in ((b'first', 0), (b'second', 1), (b'reliable', None)):
            self.assertEqual(await self.conn.read_message(), expected)
            self.assertEqual(self.conn.last_message_sequence, sequence)
