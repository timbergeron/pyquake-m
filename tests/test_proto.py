import math
import struct
import unittest

from pyquake import proto


NQ = proto.Protocol(proto.ProtocolVersion.NETQUAKE, proto.ProtocolFlags(0))
FQ = proto.Protocol(proto.ProtocolVersion.FITZQUAKE, proto.ProtocolFlags(0))


def server_info(protocol=15, level=b"test"):
    return b"\x0b" + struct.pack("<IBB", protocol, 16, 1) + level + b"\0\0\0"


def client_data():
    return b"\x0f" + struct.pack("<HIhBBBBBB", 0, 0, 100, 0, 0, 0, 0, 0, 1)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        proto.clear_cache()

    def test_rmq_24_bit_coords_consume_fractional_byte(self):
        protocol = proto.Protocol(proto.ProtocolVersion.RMQ, proto.ProtocolFlags._24BITCOORD)
        value, rest = proto.ServerMessage._parse_coord(struct.pack("<hB", -2, 128) + b"tail", protocol)
        self.assertAlmostEqual(value, -2 + 128 / 255)
        self.assertEqual(rest, b"tail")

    def test_skybox_on_fitzquake_and_rmq(self):
        for version in (proto.ProtocolVersion.FITZQUAKE, proto.ProtocolVersion.RMQ):
            with self.subTest(version=version):
                protocol = proto.Protocol(version, proto.ProtocolFlags(0))
                message, rest = proto.ServerMessage.parse_message(b"\x25sky\0\x01", protocol)
                self.assertEqual(message.string, "sky")
                self.assertEqual(rest, b"\x01")

    def test_large_entity_and_sound_indexes(self):
        packet = b"\x06\x18" + struct.pack("<HBHhhh", 5000, 3, 600, 8, -16, 24)
        message, rest = proto.ServerMessage.parse_message(packet, FQ)
        self.assertEqual((message.entity_num, message.channel, message.sound_num), (5000, 3, 600))
        self.assertEqual(message.pos, (1, -2, 3))
        self.assertEqual(rest, b"")

    def test_standard_sound_index_is_scalar(self):
        packet = b"\x06\x00" + struct.pack("<HBhhh", (10 << 3) | 2, 7, 0, 0, 0)
        message, _ = proto.ServerMessage.parse_message(packet, NQ)
        self.assertEqual((message.entity_num, message.channel, message.sound_num), (10, 2, 7))

    def test_negative_stats_and_frags(self):
        message, _ = proto.ServerMessage.parse_message(b"\x0e" + struct.pack("<Bh", 2, -5), NQ)
        self.assertEqual(message.count, -5)
        message, _ = proto.ServerMessage.parse_message(b"\x03" + struct.pack("<Bi", 0, -10), NQ)
        self.assertEqual(message.value, -10)

    def test_signed_client_data(self):
        flags = (proto._ClientDataFlags.VIEWHEIGHT | proto._ClientDataFlags.IDEALPITCH
                 | proto._ClientDataFlags.PUNCH1 | proto._ClientDataFlags.PUNCH2
                 | proto._ClientDataFlags.PUNCH3)
        packet = (b"\x0f" + struct.pack("<HbbbbbIhBBBBBB", flags, -16, -3, -1, -2, -4,
                                          0, -10, 1, 2, 3, 4, 5, 1))
        message, rest = proto.ServerMessage.parse_message(packet, NQ)
        self.assertEqual((message.view_height, message.ideal_pitch, message.health), (-16, -3, -10))
        self.assertEqual(message.punch_angles, (-1, -2, -4))
        self.assertEqual(rest, b"")

    def test_extended_baseline_has_no_native_padding(self):
        packet = (b"\x2a" + struct.pack("<HBBHBB", 1, 2, 3, 0x1234, 4, 5)
                  + struct.pack("<hBhBhB", 8, 0, 16, 64, 24, 128))
        message, rest = proto.ServerMessage.parse_message(packet, FQ)
        self.assertEqual((message.model_num, message.frame, message.colormap, message.skin),
                         (3, 0x1234, 4, 5))
        self.assertEqual(message.origin, (1, 2, 3))
        self.assertAlmostEqual(message.angles[1], math.pi / 2)
        self.assertEqual(rest, b"")

    def test_update_cache_is_scoped_to_protocol(self):
        packet = b"\x82\x01" + struct.pack("<f", 1.0) + b"tail"
        message, _ = proto.ServerMessage.parse_message(packet, NQ)
        self.assertEqual(message.origin[0], 0)
        rmq = proto.Protocol(proto.ProtocolVersion.RMQ, proto.ProtocolFlags.FLOATCOORD)
        message, rest = proto.ServerMessage.parse_message(packet, rmq)
        self.assertEqual(message.origin[0], 1)
        self.assertEqual(rest, b"tail")

    def test_update_cache_is_bounded_for_live_sessions(self):
        for coord in range(4100):
            proto.ServerMessage.parse_message(struct.pack("<BBh", 0x82, 1, coord), NQ)
        self.assertLessEqual(len(proto.ServerMessageUpdate._msg_cache), 4096)

    def test_truncated_messages_raise_protocol_error(self):
        for packet in (b"", b"\x07", b"\x82", b"\x0d", b"\x06\x18"):
            with self.subTest(packet=packet), self.assertRaises(proto.MalformedNetworkData):
                proto.ServerMessage.parse_message(packet, FQ)

    def test_nehahra_flag_is_rejected_before_reading_fitzquake_extensions(self):
        with self.assertRaises(proto.UnsupportedProtocol):
            proto.ServerMessage.parse_message(b"\x81\x80", NQ)

    def test_unknown_protocol_and_temp_entity_have_clear_errors(self):
        with self.assertRaises(proto.UnsupportedProtocol):
            proto.ServerMessage.parse_message(server_info(1234), NQ)
        with self.assertRaises(proto.MalformedNetworkData):
            proto.ServerMessage.parse_message(b"\x17\xff", NQ)
