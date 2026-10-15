import io
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

from pyquake import client, proto
from pyquake.demo import Demo, recover_demo
from test_proto import server_info


class DiskDemoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "match.dem"
        self.demo = Demo(self.path)
        self.addCleanup(self.demo.discard)
        self.demo.start_recording()

    def messages(self):
        output = io.BytesIO()
        self.demo.dump(output)
        output.seek(0)
        return [m for _, _, m in proto.read_demo_file(output)]

    def test_packets_are_visible_on_disk_without_retaining_gameplay_in_memory(self):
        self.demo.add_message((0, math.pi / 2, 0), server_info(), True)
        for frame in range(3000):
            self.demo.add_message((0, math.pi / 2, 0), b"\x07" + struct.pack("<f", frame), False)
        self.assertEqual(self.demo._msgs, [])
        self.assertEqual(self.demo._angles, [])
        self.assertGreater(self.demo.recovery_path.stat().st_size, 60000)
        self.assertFalse(self.path.exists())
        self.demo.stop_recording()
        self.demo.stop_recording()
        self.assertEqual(self.messages()[-1].msg_type, proto.ServerMessageType.DISCONNECT)
        self.assertEqual(sum(m.msg_type == proto.ServerMessageType.DISCONNECT for m in self.messages()), 1)
        self.demo.publish(self.path)
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_partial_write_is_truncated_on_retry_and_connection_can_continue(self):
        errors = []
        self.demo._on_error = errors.append
        self.demo.add_message((0, 0, 0), server_info(), True)

        def partial(record):
            self.demo._stream.write(record[:8])
            raise OSError("disk full")

        with mock.patch.object(self.demo, "_write_record", side_effect=partial):
            self.demo.add_message((0, 1, 0), b"\x07" + struct.pack("<f", 10), False)
        self.demo.add_message((0, 2, 0), b"\x07" + struct.pack("<f", 20), False)
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.demo.recording_started)
        self.demo.stop_recording()
        self.assertEqual([m.time for m in self.messages() if m.msg_type == proto.ServerMessageType.TIME], [10])
        self.assertEqual(self.demo.message_count, 3)

    def test_failed_disconnect_write_retries_without_duplicate_disconnect(self):
        self.demo.add_message((0, 0, 0), server_info(), True)
        with mock.patch.object(self.demo, "_write_record", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.demo.stop_recording()
            with self.assertRaises(OSError):
                self.demo.stop_recording()
        self.demo.stop_recording()
        self.assertEqual(sum(m.msg_type == proto.ServerMessageType.DISCONNECT for m in self.messages()), 1)

    def test_dzip_member_crc_and_payload_round_trip(self):
        self.demo.add_message((0, 0, 0), server_info(), True)
        self.demo.mark()
        self.demo.stop_recording()
        raw = self.demo.recovery_path.read_bytes()
        path = self.path.with_suffix(".dz")
        self.demo.publish(path)
        data = path.read_bytes()
        signature, directory, count = struct.unpack_from("<4sII", data)
        self.assertEqual((signature, count), (b"DZ\x03\x02", 1))
        offset, size, real, name_len, _, crc, kind, _, real2 = struct.unpack_from("<IIIHHIIII", data, directory)
        self.assertEqual((kind, real, real2), (0, len(raw), len(raw)))
        restored = zlib.decompress(data[offset:offset + size])
        self.assertEqual(restored, raw)
        self.assertEqual(crc, zlib.crc32(raw) ^ 0xffffffff)
        self.assertEqual(data[directory + 32:directory + 32 + name_len], b"match.dem\0")

    def test_hard_process_exit_keeps_recorded_packets(self):
        path = self.directory / "crashed.dem"
        script = ("import os,sys; from pyquake.demo import Demo; "
                  "d=Demo(sys.argv[1]); d.start_recording(); "
                  f"d.add_message((0,0,0),{server_info()!r},True); os._exit(0)")
        subprocess.run([sys.executable, "-B", "-c", script, str(path)], check=True)
        recovery, = self.directory.glob("crashed.recording-*.dem")
        data = recovery.read_bytes()
        self.assertEqual(data[:3], b"-1\n")
        self.assertEqual(data[19:], server_info())
        self.assertFalse(path.exists())
        recovered = self.directory / "recovered.dem"
        with recovery.open("ab") as output:
            output.write(struct.pack("<Ifff", 100, 0, 0, 0) + b"partial")
        self.assertEqual(recover_demo(recovery, recovered), 1)
        with recovered.open("rb") as stream:
            messages = list(proto.read_demo_file(stream))
        self.assertEqual(messages[-1][2].msg_type, proto.ServerMessageType.DISCONNECT)
        self.assertTrue(recovery.read_bytes().endswith(b"partial"))

    def test_recovery_failure_does_not_replace_existing_destination(self):
        path = self.directory / "invalid.dem"
        path.write_bytes(b"not a demo")
        destination = self.directory / "keep.dem"
        destination.write_bytes(b"old demo")
        with self.assertRaises(ValueError):
            recover_demo(path, destination)
        self.assertEqual(destination.read_bytes(), b"old demo")

    def test_close_keeps_recovery_and_save_can_be_retried(self):
        self.demo.add_message((0, 0, 0), server_info(), True)
        self.demo.close()
        self.assertTrue(self.demo.recovery_path.exists())
        self.demo.stop_recording()
        self.demo.publish(self.path)
        self.assertTrue(self.path.exists())

    def test_streamed_angle_calculators_remain_usable(self):
        self.demo.add_message((0, 0, 0), server_info(), True)
        self.demo.stop_recording()
        output = io.BytesIO()
        self.demo.dump(output, angle_calculator=client.AngleCalculatorSmoothed())
        output.seek(0)
        self.assertEqual(len(list(proto.read_demo_file(output))), 2)

    def test_successful_dzip_save_stays_successful_when_raw_cleanup_fails(self):
        self.demo.add_message((0, 0, 0), server_info(), True)
        self.demo.stop_recording()
        raw = self.demo.recovery_path
        unlink = Path.unlink
        def fail_raw(path, *args, **kwargs):
            if path == raw:
                raise PermissionError("raw file in use")
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", fail_raw):
            self.demo.publish(self.path.with_suffix(".dz"))
        self.assertTrue(self.path.with_suffix(".dz").exists())
        self.assertIsNone(self.demo.recovery_path)
        self.assertIsNotNone(self.demo.cleanup_warning)

    def test_discard_ends_capture_instead_of_switching_to_memory(self):
        self.demo.add_message((0, 0, 0), server_info(), True)
        self.demo.discard()
        self.assertTrue(self.demo.recording_complete)
        self.assertFalse(self.demo.recording_started)
