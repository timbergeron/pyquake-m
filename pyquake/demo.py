"""NetQuake demo records, with optional crash-recoverable disk storage."""

import math
import os
from pathlib import Path
import struct
import tempfile
import time
import zlib

if __package__:
    from . import proto
else:
    import proto


class Demo:
    def __init__(self, path=None, *, on_error=None):
        # The in-memory form remains useful to programmatic clients and angle tools.
        self._msgs = []
        self._angles = []
        self.record_pending = False
        self.recording_started = False
        self.recording_complete = False
        self.message_count = 0
        self.markers = []
        self.marker_info = {}
        self.write_error = None
        self.cleanup_warning = None
        self._on_error = on_error
        self._last_angles = (0., 0., 0.)
        self._pending_record = None
        self._pending_disconnect = False
        self._offset = 3
        self._stream = None
        self.recovery_path = None
        if path is not None:
            path = Path(path)
            # Keep the original destination intact, and make incomplete recordings
            # discoverable after a killed process. Never use the system temp disk.
            output = tempfile.NamedTemporaryFile(
                mode="w+b", buffering=0, dir=path.parent,
                prefix=path.stem + ".recording-", suffix=".dem", delete=False)
            self._stream = output
            self.recovery_path = Path(output.name)
            try:
                if output.write(b"-1\n") != 3:
                    raise OSError("short demo header write")
            except OSError:
                output.close()
                self.recovery_path.unlink(missing_ok=True)
                raise

    def start_recording(self):
        self.record_pending = True

    def _write_record(self, record):
        if self._stream.closed:
            self._stream = self.recovery_path.open("r+b", buffering=0)
        self._stream.seek(self._offset)
        self._stream.truncate()
        if self._stream.write(record) != len(record):
            raise OSError("short demo write")
        self._offset += len(record)

    def _append(self, angles, msg):
        angles, msg = tuple(angles), msg.encode("latin-1") if isinstance(msg, str) else bytes(msg)
        if self._stream is None:
            self._msgs.append(msg)
            self._angles.append(angles)
        else:
            record = struct.pack("<Ifff", len(msg), *(math.degrees(a) for a in angles)) + msg
            try:
                self._write_record(record)
            except OSError as exc:
                # Retain at most one packet for retry. Never let a disk failure
                # take down the live connection, or silently resume with a gap.
                self._pending_record = record
                self._last_angles = angles
                self.write_error = exc
                self.record_pending = self.recording_started = False
                if self._on_error is not None:
                    self._on_error(exc)
                return False
        self._last_angles = angles
        self.message_count += 1
        return True

    def add_message(self, angles, msg, has_server_info):
        if self.write_error is not None:
            return
        if self.recording_complete:
            raise RuntimeError("add_message called when recording is complete")
        if self.record_pending and has_server_info:
            self.record_pending = False
            self.recording_started = True
        if self.recording_started:
            self._append(angles, msg)

    def stop_recording(self):
        if self.recording_complete:
            return
        if self._pending_record is not None:
            self._write_record(self._pending_record)
            self._pending_record = None
            self.message_count += 1
        if self.message_count and not self._pending_disconnect:
            if not self._append(self._last_angles, b"\x02"):
                self._pending_disconnect = True
                raise self.write_error
        self.record_pending = self.recording_started = False
        self.recording_complete = True

    def note_marker(self, frame, map_name=None):
        self.markers.append(frame)
        self.marker_info[frame] = {"created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if map_name is not None:
            self.marker_info[frame]["map"] = map_name

    def mark(self, map_name=None):
        if not self.recording_started or self.write_error is not None:
            return None
        frame = self.message_count + 1
        if self._append(self._last_angles, b"\x09//demomark\n\0"):
            self.note_marker(frame, map_name)
            return frame
        return None

    def _records(self):
        if self.recovery_path is None:
            yield from zip(self._msgs, self._angles)
            return
        with self.recovery_path.open("rb") as source:
            source.readline()
            while source.tell() < self._offset:
                size, *angles = struct.unpack("<Ifff", source.read(16))
                yield source.read(size), tuple(math.radians(a) for a in angles)

    def _parse_messages(self):
        protocol = proto.Protocol(proto.ProtocolVersion.NETQUAKE, proto.ProtocolFlags(0))
        for msg, _ in self._records():
            parsed_msgs = []
            while msg:
                parsed, msg = proto.ServerMessage.parse_message(msg, protocol)
                if parsed.msg_type in (proto.ServerMessageType.SERVERINFO, proto.ServerMessageType.VERSION):
                    protocol = parsed.protocol
                parsed_msgs.append(parsed)
            yield parsed_msgs

    def dump(self, f, *, angle_calculator=None):
        f.write(b"-1\n")
        angles = iter(angle_calculator.calculate(self._parse_messages())) if angle_calculator else None
        for msg, original in self._records():
            view = next(angles) if angles is not None else original
            f.write(struct.pack("<Ifff", len(msg), *(math.degrees(a) for a in view)))
            f.write(msg)

    def publish(self, path):
        """Close and rename a completed disk recording; keep it on any failure."""
        if not self.recording_complete:
            raise RuntimeError("stop the demo before publishing")
        if self._stream.closed:
            self._stream = self.recovery_path.open("r+b", buffering=0)
        self._stream.flush()
        # fsync before publication, including the final disconnect marker.
        os.fsync(self._stream.fileno())
        if Path(path).suffix.lower() == ".dz":
            self._publish_dzip(Path(path))
            self._stream.close()
            try:
                self.recovery_path.unlink()
            except OSError as exc:
                self.cleanup_warning = f"Demo saved; could not remove raw recovery file {self.recovery_path}: {exc}"
        else:
            # Close first for Windows, where an open source cannot be renamed.
            self._stream.close()
            try:
                os.replace(self.recovery_path, path)
            except OSError:
                self._stream = self.recovery_path.open("r+b", buffering=0)
                raise
        self._stream = None
        self.recovery_path = None

    def _publish_dzip(self, path):
        """DZip 3.2 NORMAL member, the same container used by QSS-M's writer."""
        name = path.with_suffix(".dem").name.encode("utf-8") + b"\0"
        if len(name) > 65535 or self._offset > 0xffffffff:
            raise OSError("demo exceeds DZip format limits; raw recovery file kept")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w+b", dir=path.parent,
                                             prefix=".pyquake-dzip-", delete=False) as output:
                temporary = Path(output.name)
                output.write(struct.pack("<4sII", b"DZ\x03\x02", 0, 1))
                compressor = zlib.compressobj()
                crc, size = 0, 0
                with self.recovery_path.open("rb") as source:
                    while data := source.read(65536):
                        crc = zlib.crc32(data, crc)
                        size += len(data)
                        output.write(compressor.compress(data))
                output.write(compressor.flush())
                directory = output.tell()
                if directory > 0xffffffff:
                    raise OSError("compressed demo exceeds DZip format limits")
                output.write(struct.pack("<IIIHHIIII", 12, directory - 12, size,
                                         len(name), 0, crc ^ 0xffffffff, 0, 0, size))
                output.write(name)
                output.seek(0)
                output.write(struct.pack("<4sII", b"DZ\x03\x02", directory, 1))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def discard(self):
        self.record_pending = self.recording_started = False
        self.recording_complete = True
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self.recovery_path is not None:
            self.recovery_path.unlink(missing_ok=True)
            self.recovery_path = None

    def close(self):
        """Release the disk handle without deleting an unpublished recording."""
        if self._stream is not None:
            self._stream.close()


def recover_demo(source, destination):
    """Copy complete records from an interrupted recording and terminate it.

    Leave the source untouched, including a partial final record, for inspection.
    """
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("choose a destination different from the recovery file")
    temporary = None
    try:
        with source.open("rb") as original, tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=".pyquake-recover-", delete=False) as output:
            temporary = Path(output.name)
            track = original.readline(128)
            try:
                int(track)
            except ValueError as exc:
                raise ValueError("invalid demo track header") from exc
            if not track.endswith(b"\n"):
                raise ValueError("invalid demo track header")
            output.write(track)
            count, last, angles = 0, None, (0., 0., 0.)
            while header := original.read(16):
                if len(header) != 16:
                    break
                size, *view = struct.unpack("<Ifff", header)
                if size > 16 * 1024 * 1024 or not all(math.isfinite(a) for a in view):
                    raise ValueError("invalid demo record header")
                data = original.read(size)
                if len(data) != size:
                    break
                output.write(header)
                output.write(data)
                count, last, angles = count + 1, data, view
                if last == b"\x02":
                    break
            if not count:
                raise ValueError("recovery file has no complete records")
            if last != b"\x02":
                output.write(struct.pack("<Ifff", 1, *angles) + b"\x02")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        return count
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
