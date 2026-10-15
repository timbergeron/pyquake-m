# Copyright (c) 2018 Matthew Earl
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
#     The above copyright notice and this permission notice shall be included
#     in all copies or substantial portions of the Software.
# 
#     THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
#     OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
#     MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
#     NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
#     DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
#     OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
#     USE OR OTHER DEALINGS IN THE SOFTWARE.

__all__ = (
    'MalformedNetworkData',
    'ServerMessage',
    'read_demo_file',
    'clear_cache',
    'UnsupportedProtocol',
)


import dataclasses
import enum
import functools
import inspect
import math
import os
import struct


class MalformedNetworkData(Exception):
    pass


class UnsupportedProtocol(Exception):
    pass


def _read(f, n):
    s = f.read(n)
    if len(s) != n:
        raise MalformedNetworkData
    return s


class ProtocolFlags(enum.IntFlag):
    SHORTANGLE = (1 << 1)
    FLOATANGLE = (1 << 2)
    _24BITCOORD = (1 << 3)
    FLOATCOORD = (1 << 4)
    EDICTSCALE = (1 << 5)
    ALPHASANITY = (1 << 6)
    INT32COORD = (1 << 7)
    MOREFLAGS = (1 << 31)


class ProtocolVersion(enum.IntEnum):
    NETQUAKE = 15
    FITZQUAKE = 666
    RMQ = 999


PROTOCOL_FTE_PEXT1 = int.from_bytes(b'FTEX', 'little')
PROTOCOL_FTE_PEXT2 = int.from_bytes(b'FTE2', 'little')
PEXT2_REPLACEMENTDELTAS = 0x08
PEXT2_PREDINFO = 0x20
PEXT2_NEWSIZEENCODING = 0x40
PEXT2_SUPPORTED = PEXT2_REPLACEMENTDELTAS | PEXT2_PREDINFO | PEXT2_NEWSIZEENCODING


def pext_command():
    return f'pext {PROTOCOL_FTE_PEXT1} 0 {PROTOCOL_FTE_PEXT2} {PEXT2_SUPPORTED}'


@dataclasses.dataclass
class Protocol:
    version: ProtocolVersion
    flags: ProtocolFlags
    pext1: int = 0
    pext2: int = 0

    @property
    def name(self):
        return f'fte{int(self.version)}' if self.pext2 else str(int(self.version))


class TempEntityTypes(enum.IntEnum):
    SPIKE = 0
    SUPERSPIKE = 1
    GUNSHOT = 2
    EXPLOSION = 3
    TAREXPLOSION = 4
    LIGHTNING1 = 5
    LIGHTNING2 = 6
    WIZSPIKE = 7
    KNIGHTSPIKE = 8
    LIGHTNING3 = 9
    LAVASPLASH = 10
    TELEPORT = 11
    EXPLOSION2 = 12
    BEAM = 13


class ServerMessageType(enum.Enum):
    BAD = 0
    NOP = 1
    DISCONNECT = 2
    UPDATESTAT = 3
    VERSION = 4
    SETVIEW = 5
    SOUND = 6
    TIME = 7
    PRINT = 8
    STUFFTEXT = 9
    SETANGLE = 10
    SERVERINFO = 11
    LIGHTSTYLE = 12
    UPDATENAME = 13
    UPDATEFRAGS = 14
    CLIENTDATA = 15
    STOPSOUND = 16
    UPDATECOLORS = 17
    PARTICLE = 18
    DAMAGE = 19
    SPAWNSTATIC = 20
    FTE_SPAWNSTATIC = 21
    SPAWNBINARY = 21  # Historical name for this previously unused opcode.
    SPAWNBASELINE = 22
    TEMP_ENTITY = 23
    SETPAUSE = 24
    SIGNONNUM = 25
    CENTERPRINT = 26
    KILLEDMONSTER = 27
    FOUNDSECRET = 28
    SPAWNSTATICSOUND = 29
    INTERMISSION = 30
    FINALE = 31
    CDTRACK = 32
    SELLSCREEN = 33
    CUTSCENE = 34
    UPDATE = 128

    # protocol 666 message types
    SKYBOX = 37
    BF = 40
    FOG = 41
    SPAWNBASELINE2 = 42
    SPAWNSTATIC2 = 43
    SPAWNSTATICSOUND2 = 44
    DOWNLOADDATA = 50  # QSS-M/DarkPlaces legacy asset transfer
    UPDATESTATBYTE = 51
    PRECACHE = 54
    DP_SPAWNSTATICSOUND = 59
    TRAILPARTICLES = 60
    POINTPARTICLES = 61
    POINTPARTICLES1 = 62
    FTE_SPAWNBASELINE = 66
    UPDATESTATSTRING = 78
    UPDATESTATFLOAT = 79
    UPDATEENTITIES = 86


class ItemFlags(enum.IntFlag):
    SHOTGUN = 1
    SUPER_SHOTGUN = 2
    NAILGUN = 4
    SUPER_NAILGUN = 8
    GRENADE_LAUNCHER = 16
    ROCKET_LAUNCHER = 32
    LIGHTNING = 64
    SUPER_LIGHTNING = 128
    SHELLS = 256
    NAILS = 512
    ROCKETS = 1024
    CELLS = 2048
    AXE = 4096
    ARMOR1 = 8192
    ARMOR2 = 16384
    ARMOR3 = 32768
    SUPERHEALTH = 65536
    KEY1 = 131072
    KEY2 = 262144
    INVISIBILITY = 524288
    INVULNERABILITY = 1048576
    SUIT = 2097152
    QUAD = 4194304
    SIGIL1 = (1<<28)
    SIGIL2 = (1<<29)
    SIGIL3 = (1<<30)
    SIGIL4 = (1<<31)


class _UpdateFlags(enum.IntFlag):
    MOREBITS = (1<<0)
    ORIGIN1 = (1<<1)
    ORIGIN2 = (1<<2)
    ORIGIN3 = (1<<3)
    ANGLE2 = (1<<4)
    STEP = (1<<5)
    FRAME = (1<<6)
    SIGNAL = (1<<7)
    ANGLE1 = (1<<8)
    ANGLE3 = (1<<9)
    MODEL = (1<<10)
    COLORMAP = (1<<11)
    SKIN = (1<<12)
    EFFECTS = (1<<13)
    LONGENTITY = (1<<14)

    # protocol 666 flags
    EXTEND1 = (1<<15)
    ALPHA = (1<<16)
    FRAME2 = (1<<17)
    MODEL2 = (1<<18)
    LERPFINISH = (1<<19)
    SCALE = (1<<20)
    UNUSED21 = (1<<21)
    UNUSED22 = (1<<22)
    EXTEND2 = (1<<23)

    @classmethod
    def fitzquake_flags(cls):
        return (cls.ALPHA | cls.FRAME2 | cls.MODEL2 | cls.LERPFINISH | cls.SCALE |
                cls.UNUSED21 | cls.UNUSED22)


class _ClientDataFlags(enum.IntFlag):
    VIEWHEIGHT = 1<<0
    IDEALPITCH = 1<<1
    PUNCH1 = 1<<2
    PUNCH2 = 1<<3
    PUNCH3 = 1<<4
    VELOCITY1 = 1<<5
    VELOCITY2 = 1<<6
    VELOCITY3 = 1<<7
    UNUSED8 = 1<<8
    ITEMS = 1<<9
    ONGROUND = 1<<10
    INWATER = 1<<11
    WEAPONFRAME = 1<<12
    ARMOR = 1<<13
    WEAPON = 1<<14

    # protocol 666 flags
    EXTEND1 = 1<<15
    WEAPON2 = 1<<16
    ARMOR2 = 1<<17
    AMMO2 = 1<<18
    SHELLS2 = 1<<19
    NAILS2 = 1<<20
    ROCKETS2 = 1<<21
    CELLS2 = 1<<22
    EXTEND2 = 1<<23
    WEAPONFRAME2 = 1<<24
    WEAPONALPHA = 1<<25
    UNUSED26 = 1<<26
    UNUSED27 = 1<<27
    UNUSED28 = 1<<28
    UNUSED29 = 1<<29
    UNUSED30 = 1<<30
    EXTEND3 = 1<<31

    @classmethod
    def fitzquake_flags(cls):
        return (cls.EXTEND1 | cls.WEAPON2 | cls.ARMOR2 | cls.AMMO2 | cls.SHELLS2 | cls.NAILS2 |
                cls.ROCKETS2 | cls.CELLS2 | cls.EXTEND2 | cls.WEAPONFRAME2 | cls.WEAPONALPHA |
                cls.UNUSED26 | cls.UNUSED27 | cls.UNUSED28 | cls.UNUSED29 | cls.UNUSED30 |
                cls.EXTEND3)


class _SoundFlags(enum.IntFlag):
    VOLUME = (1<<0)
    ATTENUATION = (1<<1)
    LOOPING = (1<<2)

    # protocol 666 flags
    LARGEENTITY = (1<<3)
    LARGESOUND = (1<<4)

    @classmethod
    def fitzquake_flags(cls):
        return _SoundFlags.LARGEENTITY | _SoundFlags.LARGESOUND


class _BaselineBits(enum.IntFlag):
    LARGEMODEL = (1<<0)
    LARGEFRAME = (1<<1)
    ALPHA = (1<<2)


_MESSAGE_CLASSES = {}
def _register_server_message(cls):
    _MESSAGE_CLASSES[cls.msg_type] = cls
    return cls


_DEFAULT_VIEW_HEIGHT = 22
_DEFAULT_SOUND_PACKET_ATTENUATION = 1.0
_DEFAULT_SOUND_PACKET_VOLUME = 255


class ServerMessage:
    protocols = set(ProtocolVersion)
    field_names = None

    @classmethod
    @functools.lru_cache(None)
    def _get_sig(cls):
        return inspect.Signature([inspect.Parameter(n, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                                  for n in cls.field_names])

    def __init__(self, *args, **kwargs):
        bound_args = self._get_sig().bind(*args, **kwargs)
        for key, val in bound_args.arguments.items():
            setattr(self, key, val)

    def __repr__(self):
        return "{}({})".format(
                    self.__class__.__name__,
                    ", ".join("{}={!r}".format(k, getattr(self, k)) for k in self.field_names))

    @classmethod
    def _parse_struct(cls, fmt, m):
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, m[:size]), m[size:]

    @classmethod
    def _parse_string(cls, m):
        if b'\0' not in m:
            raise MalformedNetworkData('Null terminator not found')
        idx = m.index(b'\0')
        return m[:idx].decode('latin'), m[idx + 1:]

    @classmethod
    def _parse_angle(cls, m, protocol):
        proto_flags = int(protocol.flags)
        if proto_flags & int(ProtocolFlags.FLOATANGLE):
            (angle,), m = cls._parse_struct("<f", m)
            angle = math.pi * angle / 180
        elif proto_flags & int(ProtocolFlags.SHORTANGLE):
            (angle,), m = cls._parse_struct("<h", m)
            angle = math.pi * angle / 32768
        else:
            angle, m = m[0], m[1:]
            angle = angle * math.pi / 128.
        return angle, m

    @classmethod
    def _parse_coord(cls, m, protocol):
        proto_flags = int(protocol.flags)
        if proto_flags & int(ProtocolFlags.FLOATCOORD):
            (coord,), m = cls._parse_struct("<f", m)
        elif proto_flags & int(ProtocolFlags.INT32COORD):
            (coord,), m = cls._parse_struct("<i", m)
            coord = coord / 16
        elif proto_flags & int(ProtocolFlags._24BITCOORD):
            (high, low), m = cls._parse_struct("<hB", m)
            coord = high + low / 255
        else:
            (coord,), m = cls._parse_struct("<h", m)
            coord = coord / 8
        return coord, m

    @classmethod
    def _parse_angle_optional(cls, bit, flags, m, protocol):
        if int(bit) & int(flags):
            angle, m = cls._parse_angle(m, protocol)
        else:
            angle = None
        return angle, m

    @classmethod
    def _parse_coord_optional(cls, bit, flags, m, protocol):
        if int(bit) & int(flags):
            coord, m = cls._parse_coord(m, protocol)
        else:
            coord = None
        return coord, m

    @classmethod
    def _parse_tuple(cls, n, el_parser, m, protocol):
        l = []
        for _ in range(n):
            x, m = el_parser(m, protocol)
            l.append(x)
        return tuple(l), m

    @classmethod
    def _parse_angles(cls, m, protocol):
        return cls._parse_tuple(3, cls._parse_angle, m, protocol)

    @classmethod
    def _parse_coords(cls, m, protocol):
        return cls._parse_tuple(3, cls._parse_coord, m, protocol)

    @classmethod
    def _parse_entity(cls, m, protocol):
        (entity,), m = cls._parse_struct('<H', m)
        if protocol.pext2 & PEXT2_REPLACEMENTDELTAS and entity & 0x8000:
            (low,), m = cls._parse_struct('<B', m)
            entity = ((entity & 0x7fff) << 8) | low
        return entity, m

    @classmethod
    def _parse_optional(cls, bit, flags, fmt, m, post_func=None, default=None):
        if int(bit) & int(flags):
            (val,), m = cls._parse_struct(fmt, m)
            if post_func:
                val = post_func(val)
            return val, m
        else:
            return default, m

    @classmethod
    def _parse_upper_byte(cls, bit, flags, lower_byte, m):
        upper_byte, m = cls._parse_optional(bit, flags, "<B", m)
        if upper_byte is not None:
            if lower_byte is None:
                raise MalformedNetworkData('Upper byte present but lower byte not present')
            assert (lower_byte & 0xff) == lower_byte
            out = (upper_byte << 8) | lower_byte
        else:
            out = lower_byte
        return out, m

    @classmethod
    def parse_message(cls, m, protocol):
        try:
            return cls._parse_message(m, protocol)
        except (IndexError, struct.error) as exc:
            raise MalformedNetworkData("Truncated server message") from exc

    @classmethod
    def _parse_message(cls, m, protocol):
        msg_type_int = m[0]

        if msg_type_int & int(_UpdateFlags.SIGNAL):
            msg_cls = ServerMessageUpdate
        else:
            try:
                msg_type = ServerMessageType(msg_type_int)
            except ValueError:
                raise MalformedNetworkData("Invalid message type {}".format(msg_type_int))

            try:
                msg_cls = _MESSAGE_CLASSES[msg_type]
            except KeyError:
                raise MalformedNetworkData("No handler for message type {}".format(msg_type))

            if protocol is not None and protocol.version not in msg_cls.protocols:
                raise MalformedNetworkData(f"Received {msg_type} message but protocol is {protocol.version}")

            m = m[1:]

        return msg_cls.parse(m, protocol)

    @classmethod
    def parse(cls, m, protocol):
        raise NotImplementedError


class StructServerMessage(ServerMessage):
    @classmethod
    def parse(cls, m, protocol):
        vals, m = cls._parse_struct(cls.fmt, m)
        return cls(**dict(zip(cls.field_names, vals))), m


@_register_server_message
class ServerMessageDownloadData(ServerMessage):
    field_names = ('offset', 'data')
    msg_type = ServerMessageType.DOWNLOADDATA

    @classmethod
    def parse(cls, m, protocol):
        (offset, size), m = cls._parse_struct("<IH", m)
        if len(m) < size:
            raise MalformedNetworkData("Truncated download data")
        return cls(offset, m[:size]), m[size:]


class ServerMessageUpdate(ServerMessage):
    msg_type = ServerMessageType.UPDATE
    field_names = (
        'entity_num',
        'model_num',
        'frame',
        'colormap',
        'skin',
        'effects',
        'origin',
        'angle',
        'step',
    )

    _size_cache = {}
    _msg_cache = {}

    @classmethod
    def clear_cache(cls):
        cls._size_cache = {}
        cls._msg_cache = {}

    @classmethod
    def _parse_flags_fast(cls, m, protocol):
        """Parse out flags but for efficiency don't convert to enum types.

        In addition test against numbers rather than enum values to avoid the extra lookups.
        """
        flags = m[0]
        n = 1
        if flags & 1: # MOREBITS
            flags |= (m[n] << 8)
            n += 1
            if protocol.version != ProtocolVersion.NETQUAKE and flags & (1 << 15):  # EXTEND1
                flags |= m[n] << 16
                n += 1
                if flags & (1 << 23): # EXTEND2
                    flags |= m[n] << 24
                    n += 1
        return flags, m[n:]

    @classmethod
    def _parse_flags_safe(cls, m, protocol):
        """Like _parse_flags_fast but converts to enum type and does some checks.

        Used when a cache miss occurs to check that _parse_flags_fast is returning the same value.
        """
        flags, m = _UpdateFlags(m[0]), m[1:]
        assert flags & _UpdateFlags.SIGNAL

        if flags & _UpdateFlags.MOREBITS:
            more_flags, m = m[0], m[1:]
            flags |= (more_flags << 8)

        if protocol.version != ProtocolVersion.NETQUAKE:
            if flags & _UpdateFlags.EXTEND1:
                extend1_flags, m = m[0], m[1:]
                flags |= extend1_flags << 16
            if flags & _UpdateFlags.EXTEND2:
                extend2_flags, m = m[0], m[1:]
                flags |= extend2_flags << 24
        else:
            if flags & (1 << 15):   # U_TRANS
                # Some mods will send this flag for transparency when using
                # protocol 15.  We don't support it yet, but could do.
                raise UnsupportedProtocol('Nehahra not supported')

            fq_flags = flags & _UpdateFlags.fitzquake_flags()
            if fq_flags:
                raise MalformedNetworkData(f'{fq_flags} passed but protocol is {protocol}')

        return flags, m

    @classmethod
    def _parse_no_cache(cls, flags, m, protocol):
        (entity_num,), m = cls._parse_struct("<H" if flags & _UpdateFlags.LONGENTITY else "<B", m)
        model_num, m = cls._parse_optional(_UpdateFlags.MODEL, flags, "<B", m)
        frame, m = cls._parse_optional(_UpdateFlags.FRAME, flags, "<B", m)
        colormap, m = cls._parse_optional(_UpdateFlags.COLORMAP, flags, "<B", m)
        skin, m = cls._parse_optional(_UpdateFlags.SKIN, flags, "<B", m)
        effects, m = cls._parse_optional(_UpdateFlags.EFFECTS, flags, "<B", m)

        fix_coord = lambda c: c / 8.
        fix_angle = lambda a: a * math.pi / 128.

        origin1, m = cls._parse_coord_optional(_UpdateFlags.ORIGIN1, flags, m, protocol)
        angle1, m = cls._parse_angle_optional(_UpdateFlags.ANGLE1, flags, m, protocol)
        origin2, m = cls._parse_coord_optional(_UpdateFlags.ORIGIN2, flags, m, protocol)
        angle2, m = cls._parse_angle_optional(_UpdateFlags.ANGLE2, flags, m, protocol)
        origin3, m = cls._parse_coord_optional(_UpdateFlags.ORIGIN3, flags, m, protocol)
        angle3, m = cls._parse_angle_optional(_UpdateFlags.ANGLE3, flags, m, protocol)
        origin = (origin1, origin2, origin3)
        angle = (angle1, angle2, angle3)

        if protocol.version != ProtocolVersion.NETQUAKE:
            # TODO: Store alpha / scale / lerpfinish
            alpha, m = cls._parse_optional(_UpdateFlags.ALPHA, flags, "<B", m)
            scale, m = cls._parse_optional(_UpdateFlags.SCALE, flags, "<B", m)
            frame, m = cls._parse_upper_byte(_UpdateFlags.FRAME2, flags, frame, m)
            model_num, m = cls._parse_upper_byte(_UpdateFlags.MODEL2, flags, model_num, m)
            lerp_finish, m = cls._parse_optional(_UpdateFlags.LERPFINISH, flags, "<B", m)

        step = bool(flags & _UpdateFlags.STEP)

        return cls(entity_num,
                   model_num,
                   frame,
                   colormap,
                   skin,
                   effects,
                   origin,
                   angle,
                   step), m, flags

    @classmethod
    def parse(cls, m, protocol):
        int_flags, m_after_flags = cls._parse_flags_fast(m, protocol)
        protocol_key = (protocol.version, protocol.flags)
        size_key = (*protocol_key, int_flags)

        msg = None
        size = cls._size_cache.get(size_key)
        if size is not None:
            msg = cls._msg_cache.get((*protocol_key, m[:size]))

        if msg is None:
            flags, _ = cls._parse_flags_safe(m, protocol)
            assert flags == int_flags, f"flags={flags} int_flags={int_flags}"
            msg, m_after, flags = cls._parse_no_cache(flags, m_after_flags, protocol)
            size = len(m) - len(m_after)
            # Live sessions continually receive new entity positions.
            if len(cls._msg_cache) >= 4096:
                cls.clear_cache()
            cls._size_cache[size_key] = size
            cls._msg_cache[(*protocol_key, m[:size])] = msg

        return msg, m[size:]


class NoFieldsServerMessage(ServerMessage):
    field_names = ()

    @classmethod
    def parse(cls, m, protocol):
        return cls(), m


@_register_server_message
class ServerMessageNop(NoFieldsServerMessage):
    msg_type = ServerMessageType.NOP


@_register_server_message
class ServerMessageVersion(ServerMessage):
    field_names = ('protocol',)
    msg_type = ServerMessageType.VERSION

    @classmethod
    def parse(cls, m, protocol):
        (version,), m = cls._parse_struct("<I", m)
        try:
            version = ProtocolVersion(version)
        except ValueError as exc:
            raise UnsupportedProtocol(f"Unsupported protocol: {version}") from exc
        flags = protocol.flags if protocol is not None and protocol.version == version else ProtocolFlags(0)
        next_protocol = dataclasses.replace(protocol) if protocol is not None and protocol.version == version else Protocol(version, flags)
        return cls(next_protocol), m


@_register_server_message
class ServerMessageStopSound(ServerMessage):
    field_names = ('entity_num', 'channel')
    msg_type = ServerMessageType.STOPSOUND

    @classmethod
    def parse(cls, m, protocol):
        (channel,), m = cls._parse_struct("<H", m)
        return cls(channel >> 3, channel & 7), m


@_register_server_message
class ServerMessageSellScreen(NoFieldsServerMessage):
    msg_type = ServerMessageType.SELLSCREEN


@_register_server_message
class ServerMessageFoundSecret(NoFieldsServerMessage):
    msg_type = ServerMessageType.FOUNDSECRET


@_register_server_message
class ServerMessageBonusFlash(NoFieldsServerMessage):
    protocols = {ProtocolVersion.FITZQUAKE, ProtocolVersion.RMQ}
    msg_type = ServerMessageType.BF


@_register_server_message
class ServerMessageFog(ServerMessage):
    field_names = ('density', 'color', 'time')
    protocols = {ProtocolVersion.FITZQUAKE, ProtocolVersion.RMQ}
    msg_type = ServerMessageType.FOG

    @classmethod
    def parse(cls, m, protocol):
        (density, r, g, b, time_short), m = cls._parse_struct("<BBBBH", m)
        return cls(density, (r, g, b), time_short / 100.), m


@_register_server_message
class ServerMessagePrint(ServerMessage):
    field_names = ('string',)
    msg_type = ServerMessageType.PRINT

    @classmethod
    def parse(cls, m, protocol):
        s, m = cls._parse_string(m)
        return cls(s), m


@_register_server_message
class ServerMessageCenterPrint(ServerMessage):
    field_names = ('string',)
    msg_type = ServerMessageType.CENTERPRINT

    @classmethod
    def parse(cls, m, protocol):
        s, m = cls._parse_string(m)
        return cls(s), m


@_register_server_message
class ServerMessageCutScene(ServerMessage):
    field_names = ('string',)
    msg_type = ServerMessageType.CUTSCENE

    @classmethod
    def parse(cls, m, protocol):
        s, m = cls._parse_string(m)
        return cls(s), m


@_register_server_message
class ServerMessageStuffText(ServerMessage):
    field_names = ('string',)
    msg_type = ServerMessageType.STUFFTEXT

    @classmethod
    def parse(cls, m, protocol):
        s, m = cls._parse_string(m)
        return cls(s), m


@_register_server_message
class ServerMessageSkybox(ServerMessage):
    protocols = {ProtocolVersion.FITZQUAKE, ProtocolVersion.RMQ}
    field_names = ('string',)
    msg_type = ServerMessageType.SKYBOX

    @classmethod
    def parse(cls, m, protocol):
        s, m = cls._parse_string(m)
        return cls(s), m


class _SpawnStaticSoundBase(ServerMessage):
    field_names = ("origin", "sound_num", "vol", "atten")

    @classmethod
    def _parse_generic(cls, m, protocol, version):
        origin, m = cls._parse_coords(m, protocol)

        fmt = "<HBB" if version == 2 else "<BBB"
        (sound_num, vol, atten), m = cls._parse_struct(fmt, m)

        return cls(origin, sound_num, vol, atten), m


@_register_server_message
class ServerMessageSpawnStaticSound(_SpawnStaticSoundBase):
    msg_type = ServerMessageType.SPAWNSTATICSOUND

    @classmethod
    def parse(cls, m, protocol):
        return cls._parse_generic(m, protocol, 1)


@_register_server_message
class ServerMessageSpawnStaticSound2(_SpawnStaticSoundBase):
    msg_type = ServerMessageType.SPAWNSTATICSOUND2

    @classmethod
    def parse(cls, m, protocol):
        return cls._parse_generic(m, protocol, 2)


@_register_server_message
class ServerMessageDPStaticSound(ServerMessageSpawnStaticSound2):
    msg_type = ServerMessageType.DP_SPAWNSTATICSOUND

    @classmethod
    def parse(cls, m, protocol):
        if protocol is None or not protocol.pext2 & PEXT2_REPLACEMENTDELTAS:
            raise MalformedNetworkData('Extended static sound without negotiated extension')
        return super().parse(m, protocol)


@_register_server_message
class ServerMessageCdTrack(StructServerMessage):
    fmt = "<BB"
    field_names = ("track", "loop")
    msg_type = ServerMessageType.CDTRACK


@_register_server_message
class ServerMessageSetView(StructServerMessage):
    fmt = "<H"
    field_names = ("viewentity",)
    msg_type = ServerMessageType.SETVIEW


@_register_server_message
class ServerMessageSignOnNum(StructServerMessage):
    fmt = "<B"
    field_names = ("num",)
    msg_type = ServerMessageType.SIGNONNUM


@_register_server_message
class ServerMessageSetPause(StructServerMessage):
    fmt = "<B"
    field_names = ("paused",)
    msg_type = ServerMessageType.SETPAUSE


class _SpawnBaselineBase(ServerMessage):
    @classmethod
    def _parse_generic(cls, m, protocol, include_entity_num, version):
        if include_entity_num:
            (entity_num,), m = cls._parse_struct("<H", m)

        if version == 2:
            (bits,), m = cls._parse_struct("<B", m)
            bits = _BaselineBits(bits)
            fmt = (f"<{'H' if bits & _BaselineBits.LARGEMODEL else 'B'}"
                   f"{'H' if bits & _BaselineBits.LARGEFRAME else 'B'}"
                   "BB")
        else:
            bits = _BaselineBits(0)
            fmt = "<BBBB"

        (model_num, frame, colormap, skin), m = cls._parse_struct(fmt, m)
        origin, angles = [], []
        for _ in range(3):
            o, m = cls._parse_coord(m, protocol)
            a, m = cls._parse_angle(m, protocol)
            origin.append(o)
            angles.append(a)

        if bits & _BaselineBits.ALPHA:
            # TODO: Store alpha
            (alpha,), m = cls._parse_struct("<B", m)

        if include_entity_num:
            return cls(entity_num, model_num, frame, colormap, skin, tuple(origin), tuple(angles)), m
        else:
            return cls(model_num, frame, colormap, skin, tuple(origin), tuple(angles)), m


@_register_server_message
class ServerMessageSpawnBaseline(_SpawnBaselineBase):
    field_names = ("entity_num", "model_num", "frame", "colormap", "skin", "origin", "angles")
    msg_type = ServerMessageType.SPAWNBASELINE

    @classmethod
    def parse(cls, m, protocol):
        return cls._parse_generic(m, protocol, True, 1)


@_register_server_message
class ServerMessageSpawnBaseline2(_SpawnBaselineBase):
    protocols = {ProtocolVersion.FITZQUAKE, ProtocolVersion.RMQ}
    field_names = ("entity_num", "model_num", "frame", "colormap", "skin", "origin", "angles")
    msg_type = ServerMessageType.SPAWNBASELINE2

    @classmethod
    def parse(cls, m, protocol):
        return cls._parse_generic(m, protocol, True, 2)


@_register_server_message
class ServerMessageSpawnStatic(_SpawnBaselineBase):
    field_names = ("model_num", "frame", "colormap", "skin", "origin", "angles")
    msg_type = ServerMessageType.SPAWNSTATIC

    @classmethod
    def parse(cls, m, protocol):
        return cls._parse_generic(m, protocol, False, 1)


@_register_server_message
class ServerMessageSpawnStatic2(_SpawnBaselineBase):
    protocols = {ProtocolVersion.FITZQUAKE, ProtocolVersion.RMQ}
    field_names = ("model_num", "frame", "colormap", "skin", "origin", "angles")
    msg_type = ServerMessageType.SPAWNSTATIC2

    @classmethod
    def parse(cls, m, protocol):
        return cls._parse_generic(m, protocol, False, 2)


@_register_server_message
class ServerMessageTime(StructServerMessage):
    fmt = "<f"
    field_names = ("time",)
    msg_type = ServerMessageType.TIME

    @classmethod
    def parse(cls, m, protocol):
        (time,), m = cls._parse_struct('<f', m)
        message = cls(time)
        message.input_ack = None
        if protocol is not None and protocol.pext2 & PEXT2_PREDINFO:
            (message.input_ack,), m = cls._parse_struct('<H', m)
        return message, m


@_register_server_message
class ServerMessageUpdateName(ServerMessage):
    msg_type = ServerMessageType.UPDATENAME
    field_names = ('client_num', 'name')

    @classmethod
    def parse(cls, m, protocol):
        client_num, m = m[0], m[1:]
        name, m = cls._parse_string(m)
        return cls(client_num, name), m


@_register_server_message
class ServerMessageUpdateFrags(StructServerMessage):
    fmt = "<Bh"
    field_names = ("client_num", "count")
    msg_type = ServerMessageType.UPDATEFRAGS


@_register_server_message
class ServerMessageUpdateColors(StructServerMessage):
    fmt = "<BB"
    field_names = ("client_num", "color")
    msg_type = ServerMessageType.UPDATECOLORS


@_register_server_message
class ServerMessageLightStyle(ServerMessage):
    field_names = ('index', 'style')
    msg_type = ServerMessageType.LIGHTSTYLE

    @classmethod
    def parse(cls, m, protocol):
        index, m = m[0], m[1:]
        style, m = cls._parse_string(m)
        return cls(index, style), m


@_register_server_message
class ServerMessageUpdateStat(StructServerMessage):
    fmt = "<Bi"
    field_names = ('index', 'value')
    msg_type = ServerMessageType.UPDATESTAT


@_register_server_message
class ServerMessageSetAngle(ServerMessage):
    field_names = ('view_angles',)
    msg_type = ServerMessageType.SETANGLE

    @classmethod
    def parse(cls, m, protocol):
        view_angles, m = cls._parse_angles(m, protocol)
        return cls(view_angles), m


@_register_server_message
class ServerMessageServerInfo(ServerMessage):
    field_names = ('protocol', 'max_clients', 'game_type', 'level_name', 'models', 'sounds')
    msg_type = ServerMessageType.SERVERINFO

    @classmethod
    def _parse_string_list(cls, m):
        l = []
        while True:
            s, m = cls._parse_string(m)
            if not s:
                break
            l.append(s)
        return l, m

    @classmethod
    def parse(cls, m, protocol):
        (protocol_version,), m = cls._parse_struct("<I", m)
        extensions = {PROTOCOL_FTE_PEXT1: 0, PROTOCOL_FTE_PEXT2: 0}
        seen = set()
        while protocol_version in extensions:
            if protocol_version in seen:
                raise MalformedNetworkData('Duplicate PEXT header')
            seen.add(protocol_version)
            (extensions[protocol_version], protocol_version), m = cls._parse_struct('<II', m)
        pext1, pext2 = extensions.values()
        if pext1 or pext2 & ~PEXT2_SUPPORTED:
            raise UnsupportedProtocol(f'Unsupported extensions: {pext1:#x}/{pext2:#x}')
        if pext2 & PEXT2_PREDINFO and not pext2 & PEXT2_REPLACEMENTDELTAS:
            raise UnsupportedProtocol('PREDINFO requires REPLACEMENTDELTAS')
        try:
            protocol_version = ProtocolVersion(protocol_version)
        except ValueError as exc:
            raise UnsupportedProtocol(f"Unsupported protocol: {protocol_version}") from exc

        if protocol_version == ProtocolVersion.RMQ:
            (protocol_flags,), m = cls._parse_struct("<I", m)
            protocol_flags = ProtocolFlags(protocol_flags)
        else:
            protocol_flags = ProtocolFlags(0)

        next_protocol = Protocol(protocol_version, protocol_flags, pext1, pext2)
        gamedir = ''
        if pext2 & PEXT2_PREDINFO:
            gamedir, m = cls._parse_string(m)

        (max_clients, game_type), m = cls._parse_struct("<BB", m)
        level_name, m = cls._parse_string(m)
        models, m = cls._parse_string_list(m)
        sounds, m = cls._parse_string_list(m)

        message = cls(next_protocol, max_clients, game_type, level_name, models, sounds)
        message.gamedir = gamedir
        return message, m


@_register_server_message
class ServerMessageClientData(ServerMessage):
    field_names = (
        'view_height',
        'ideal_pitch',
        'punch_angles',
        'm_velocity',
        'items',
        'on_ground',
        'in_water',
        'weapon_frame',
        'armor',
        'weapon_model_index',
        'health',
        'ammo',
        'shells',
        'nails',
        'rockets',
        'cells',
        'active_weapon',
    )
    msg_type = ServerMessageType.CLIENTDATA

    @classmethod
    def parse(cls, m, protocol):
        (flags_int,), m = cls._parse_struct("<H", m)
        flags = _ClientDataFlags(flags_int)

        if protocol.version != ProtocolVersion.NETQUAKE:
            if flags & _ClientDataFlags.EXTEND1:
                extend1_flags, m = m[0], m[1:]
                flags |= extend1_flags << 16
            if flags & _ClientDataFlags.EXTEND2:
                extend1_flags, m = m[0], m[1:]
                flags |= extend1_flags << 24
        else:
            fq_flags = flags & _ClientDataFlags.fitzquake_flags()
            if fq_flags:
                raise MalformedNetworkData(f'{fq_flags} passed but protocol is {protocol}')

        view_height, m = cls._parse_optional(_ClientDataFlags.VIEWHEIGHT, flags, "<b", m,
                                             default=_DEFAULT_VIEW_HEIGHT)
        ideal_pitch, m = cls._parse_optional(_ClientDataFlags.IDEALPITCH, flags, "<b", m, default=0)

        fix_velocity = lambda v: v * 16
        punch1, m = cls._parse_optional(_ClientDataFlags.PUNCH1, flags, "<b", m, default=0)
        m_velocity1, m = cls._parse_optional(_ClientDataFlags.VELOCITY1, flags, "<b", m, fix_velocity,
                                             default=0)
        punch2, m = cls._parse_optional(_ClientDataFlags.PUNCH2, flags, "<b", m, default=0)
        m_velocity2, m = cls._parse_optional(_ClientDataFlags.VELOCITY2, flags, "<b", m, fix_velocity,
                                             default=0)
        punch3, m = cls._parse_optional(_ClientDataFlags.PUNCH3, flags, "<b", m, default=0)
        m_velocity3, m = cls._parse_optional(_ClientDataFlags.VELOCITY3, flags, "<b", m, fix_velocity,
                                             default=0)
        punch_angles = (punch1, punch2, punch3)
        m_velocity = (m_velocity1, m_velocity2, m_velocity3)

        (items_int,), m = cls._parse_struct("<I", m)
        items = ItemFlags(items_int)

        on_ground = bool(flags & _ClientDataFlags.ONGROUND)
        in_water = bool(flags & _ClientDataFlags.INWATER)

        weapon_frame, m = cls._parse_optional(_ClientDataFlags.WEAPONFRAME, flags, "<B", m, default=0)
        armor, m = cls._parse_optional(_ClientDataFlags.ARMOR, flags, "<B", m, default=0)
        weapon_model_index, m = cls._parse_optional(_ClientDataFlags.WEAPON, flags, "<B", m, default=0)

        (health, ammo, shells, nails, rockets, cells, active_weapon), m = cls._parse_struct("<hBBBBBB", m)
        active_weapon = ItemFlags(active_weapon)

        if protocol.version != ProtocolVersion.NETQUAKE:
            weapon_model_index, m = cls._parse_upper_byte(_ClientDataFlags.WEAPON2, flags, weapon_model_index, m)
            armor, m = cls._parse_upper_byte(_ClientDataFlags.ARMOR2, flags, armor, m)
            ammo, m = cls._parse_upper_byte(_ClientDataFlags.AMMO2, flags, ammo, m)
            shells, m = cls._parse_upper_byte(_ClientDataFlags.SHELLS2, flags, shells, m)
            nails, m = cls._parse_upper_byte(_ClientDataFlags.NAILS2, flags, nails, m)
            rockets, m = cls._parse_upper_byte(_ClientDataFlags.ROCKETS2, flags, rockets, m)
            cells, m = cls._parse_upper_byte(_ClientDataFlags.CELLS2, flags, cells, m)
            weapon_frame, m = cls._parse_upper_byte(_ClientDataFlags.WEAPONFRAME2, flags, weapon_frame, m)

            # TODO: Store weapon alpha
            weapon_alpha, m = cls._parse_optional(_ClientDataFlags.WEAPONALPHA, flags, "<B", m)

        return cls(
            view_height,
            ideal_pitch,
            punch_angles,
            m_velocity,
            items,
            on_ground,
            in_water,
            weapon_frame,
            armor,
            weapon_model_index,
            health,
            ammo,
            shells,
            nails,
            rockets,
            cells,
            active_weapon
        ), m


@_register_server_message
class ServerMessageSound(ServerMessage):
    field_names = ('volume', 'attenuation', 'entity_num', 'channel', 'sound_num', 'pos')
    msg_type = ServerMessageType.SOUND

    @classmethod
    def parse(cls, m, protocol):
        flags, m = _SoundFlags(m[0]), m[1:]
        replacement = bool(protocol.pext2 & PEXT2_REPLACEMENTDELTAS)
        if replacement and flags & 4:
            # FTE's unsigned integer uses leading one bits to count following bytes.
            (head,), m = cls._parse_struct('<B', m)
            count = 0
            marker = 128
            while marker and head & marker:
                head -= marker
                count += 1
                marker >>= 1
            extra = head
            for _ in range(count):
                (byte,), m = cls._parse_struct('<B', m)
                extra = (extra << 8) | byte
            flags = int(flags) | (extra << 8)

        volume, m = cls._parse_optional(_SoundFlags.VOLUME, flags, "<B", m,
                                        default=_DEFAULT_SOUND_PACKET_VOLUME)
        attenuation, m = cls._parse_optional(_SoundFlags.ATTENUATION, flags, "<B", m, lambda b: b / 64.,
                                             default=_DEFAULT_SOUND_PACKET_ATTENUATION)

        if replacement:
            for flag, fmt in ((128, '<B'), (64, '<h'), (256, '<hhh'), (32, '<h')):
                if flags & flag:
                    _, m = cls._parse_struct(fmt, m)
        elif protocol.version == ProtocolVersion.NETQUAKE:
            fq_flags = flags & _SoundFlags.fitzquake_flags()
            if fq_flags:
                raise MalformedNetworkData(f'{fq_flags} passed but protocol is {protocol}')

        if flags & _SoundFlags.LARGEENTITY:
            (entity_num, channel), m = cls._parse_struct("<HB", m)
        else:
            (t,), m = cls._parse_struct("<H", m)
            entity_num = t >> 3
            channel = t & 7

        (sound_num,), m = cls._parse_struct("<H" if flags & _SoundFlags.LARGESOUND else "<B", m)
        pos, m = cls._parse_coords(m, protocol)

        return cls(volume, attenuation, entity_num, channel, sound_num, pos), m


@_register_server_message
class ServerMessageParticle(ServerMessage):
    field_names = ('origin', 'direction', 'count', 'color')
    msg_type = ServerMessageType.PARTICLE

    @classmethod
    def parse(cls, m, protocol):
        origin, m = cls._parse_coords(m, protocol)
        
        direction, m = cls._parse_struct("<bbb", m)
        direction = tuple(x / 16. for x in direction)

        count, m = m[0], m[1:]
        if count == 255:
            count = 1024

        color, m = m[0], m[1:]

        return cls(origin, direction, count, color), m


@dataclasses.dataclass
class FTEDelta:
    """Decoded fields and their wire bytes, retained for bounded demo snapshots."""
    reset: bool
    values: dict
    payloads: dict


_FTE_ORDER = ('frame', 'originxy', 'originz', 'anglesxz', 'anglesy', 'effects',
              'prediction', 'model_num', 'skin', 'colormap', 'solid', 'eflags',
              'alpha', 'scale', 'bones', 'drawflags', 'tag', 'light', 'trail',
              'colormod', 'glow', 'fatness', 'model2', 'gravity')


def merge_fte_delta(previous, baseline, delta):
    source = baseline if delta.reset or previous is None else previous
    values = dict(source.values) if source is not None else {}
    payloads = dict(source.payloads) if source is not None else {}
    # Velocity is sent afresh on every delta, unlike ordinary persistent fields.
    values['velocity'] = (0., 0., 0.)
    values.update(delta.values)
    payloads.update(delta.payloads)
    return FTEDelta(False, values, payloads)


def encode_fte_delta(state, protocol):
    """Serialize a complete reset for a demo begun during a live session."""
    payloads = dict(state.payloads)
    # Rebuild prediction from current values: old velocity bytes must not persist.
    velocity = state.values.get('velocity', (0., 0., 0.))
    predbits = 8 | 16 | 32
    prediction = struct.pack('<BBhhh', predbits, state.values.get('movetype', 0),
                             *(round(v * 8) for v in velocity))
    payloads['prediction'] = (1 << 6, prediction)
    if not protocol.pext2 & PEXT2_PREDINFO:
        # UF_PREDINFO selects 16-bit angles in the older delta dialect. Recode
        # angles which may have arrived in deltas without prediction fields.
        for name, bit in (('anglesxz', 8), ('anglesy', 16)):
            if name in state.values:
                angles = state.values[name] if name == 'anglesxz' else (state.values[name],)
                if protocol.flags & ProtocolFlags.FLOATANGLE:
                    data = struct.pack('<' + 'f' * len(angles), *(math.degrees(v) for v in angles))
                else:
                    data = struct.pack('<' + 'H' * len(angles),
                                       *(round(v * 32768 / math.pi) & 65535 for v in angles))
                payloads[name] = (bit, data)
    bits = 1 << 8  # UF_RESET
    for flags, _ in payloads.values():
        bits |= flags
    wide = bool(bits & (1 << 9))
    body = bytearray()
    for name in _FTE_ORDER:
        if name not in payloads:
            continue
        flags, data = payloads[name]
        if wide and name in ('frame', 'model_num', 'skin', 'model2') and not flags & (1 << 9):
            data += b'\0'
        body.extend(data)
    return _encode_fte_bits(bits) + body


def _encode_fte_bits(bits):
    if bits & 0xffffff00:
        bits |= 1 << 7
    if bits & 0xffff0000:
        bits |= 1 << 15
    if bits & 0xff000000:
        bits |= 1 << 23
    return bytes((bits >> shift) & 255 for shift in (0, 8, 16, 24)
                 if shift == 0 or bits & (1 << (shift - 1)))


def encode_fte_entity_number(entity):
    if not 0 < entity < (1 << 22):
        raise ValueError('Replacement entity number out of range')
    if entity >= 0x4000:
        return struct.pack('<HB', 0x4000 | (entity & 0x3fff), entity >> 14)
    return struct.pack('<H', entity)


class _FTEDeltaReader:
    def __init__(self, data, protocol):
        self.m = self.start = data
        self.protocol = protocol
        self.values = {}
        self.payloads = {}

    def read(self, fmt):
        values, self.m = ServerMessage._parse_struct('<' + fmt, self.m)
        return values[0] if len(values) == 1 else values

    def save(self, name, flags, value=None):
        if value is not None:
            self.values[name] = value
        self.payloads[name] = (flags, self.start[:len(self.start) - len(self.m)])
        self.start = self.m

    def coord(self):
        value, self.m = ServerMessage._parse_coord(self.m, self.protocol)
        return value

    def angle(self, high=False):
        protocol = self.protocol
        if high and not protocol.flags & ProtocolFlags.FLOATANGLE:
            protocol = dataclasses.replace(protocol, flags=protocol.flags | ProtocolFlags.SHORTANGLE)
        value, self.m = ServerMessage._parse_angle(self.m, protocol)
        return value


def _parse_fte_delta(m, protocol):
    r = _FTEDeltaReader(m, protocol)
    bits = r.read('B')
    for flag, shift in ((7, 8), (15, 16), (23, 24)):
        if bits & (1 << flag):
            bits |= r.read('B') << shift
    if bits & 0xc0000000:
        raise UnsupportedProtocol('Unknown replacement delta flags')
    r.start = r.m
    wide = bits & (1 << 9)
    size = 'H' if wide else 'B'
    if bits & 1:
        r.save('frame', 1 | wide, r.read(size))
    if bits & 2:
        r.save('originxy', 2, (r.coord(), r.coord()))
    if bits & 4:
        r.save('originz', 4, r.coord())
    high = bool(bits & 64 and not protocol.pext2 & PEXT2_PREDINFO)
    if bits & 8:
        r.save('anglesxz', 8, (r.angle(high), r.angle(high)))
    if bits & 16:
        r.save('anglesy', 16, r.angle(high))
    effects = bits & ((1 << 5) | (1 << 29))
    if effects:
        fmt = 'I' if effects == ((1 << 5) | (1 << 29)) else 'H' if effects & (1 << 29) else 'B'
        r.save('effects', effects, r.read(fmt))
    if bits & 64:
        pred = r.read('B')
        for flag in (1, 2, 4):
            if pred & flag:
                r.read('h')
        if pred & 8:
            r.values['movetype'] = r.read('B')
        xy = r.read('hh') if pred & 16 else (0, 0)
        z = r.read('h') if pred & 32 else 0
        r.values['velocity'] = tuple(v / 8 for v in (*xy, z))
        if pred & 64:
            r.read('B')
        if pred & 128:
            if protocol.pext2 & PEXT2_PREDINFO:
                if bits & 8:
                    r.read('HH')
                if bits & 16:
                    r.read('H')
            elif r.read('B') & 128:
                r.read('B')
        r.save('prediction', 64)
    for name, flag, fmt in (('model_num', 10, size), ('skin', 11, size), ('colormap', 12, 'B')):
        if bits & (1 << flag):
            r.save(name, (1 << flag) | (wide if flag != 12 else 0), r.read(fmt))
    if bits & (1 << 13):
        if protocol.pext2 & PEXT2_NEWSIZEENCODING:
            encoding = r.read('B')
            if encoding not in (0, 1, 2, 3, 16, 32):
                raise MalformedNetworkData(f'Unknown solid encoding: {encoding}')
            solid = (encoding, r.read('H' if encoding == 16 else 'I') if encoding in (16, 32) else None)
        else:
            solid = (16, r.read('H'))
        r.save('solid', 1 << 13, solid)
    for name, flag in (('eflags', 14), ('alpha', 16), ('scale', 17)):
        if bits & (1 << flag):
            r.save(name, 1 << flag, r.read('B'))
    if bits & (1 << 18):
        flags = r.read('B')
        if flags & 0x3f:
            raise UnsupportedProtocol('Unknown bone data flags')
        if flags & 0x80:
            for _ in range(r.read('B') * 7):
                r.read('h')
        if flags & 0x40:
            r.read('BH')
        r.save('bones', 1 << 18)
    if bits & (1 << 19):
        flags = r.read('B')
        if flags & 7 == 7:
            r.read('B')
        r.save('drawflags', 1 << 19, flags)
    if bits & (1 << 20):
        entity, r.m = ServerMessage._parse_entity(r.m, protocol)
        r.save('tag', 1 << 20, (entity, r.read('B')))
    if bits & (1 << 21):
        r.read('hhhhBB')
        r.save('light', 1 << 21)
    if bits & (1 << 22):
        trail = r.read('H')
        if trail & 0x8000:
            r.read('H')
        r.save('trail', 1 << 22)
    for name, flag, fmt in (('colormod', 24, 'BBB'), ('glow', 25, 'BBBBB'),
                             ('fatness', 26, 'B'), ('model2', 27, size), ('gravity', 28, 'BB')):
        if bits & (1 << flag):
            r.save(name, (1 << flag) | (wide if flag == 27 else 0), r.read(fmt))
    return FTEDelta(bool(bits & 256), r.values, r.payloads), r.m


class _FTEMessage(ServerMessage):
    @classmethod
    def require_extension(cls, protocol):
        if protocol is None or not protocol.pext2 & PEXT2_REPLACEMENTDELTAS:
            raise MalformedNetworkData('Replacement delta message without negotiated extension')


@_register_server_message
class ServerMessageUpdateEntities(_FTEMessage):
    field_names = ('time', 'input_ack', 'updates')
    msg_type = ServerMessageType.UPDATEENTITIES

    @classmethod
    def parse(cls, m, protocol):
        cls.require_extension(protocol)
        ack = None
        if protocol.pext2 & PEXT2_PREDINFO:
            (ack,), m = cls._parse_struct('<H', m)
        (time,), m = cls._parse_struct('<f', m)
        updates = []
        while True:
            (number,), m = cls._parse_struct('<H', m)
            remove = bool(number & 0x8000)
            if number & 0x4000:
                (high,), m = cls._parse_struct('<B', m)
                number = (number & 0x3fff) | (high << 14)
            else:
                number &= 0x7fff
            if not number and not remove:
                break
            delta = None
            if not remove:
                delta, m = _parse_fte_delta(m, protocol)
            updates.append((number, delta))
        return cls(time, ack, updates), m


@_register_server_message
class ServerMessageFTEBaseline(_FTEMessage):
    field_names = ('entity_num', 'delta')
    msg_type = ServerMessageType.FTE_SPAWNBASELINE

    @classmethod
    def parse(cls, m, protocol):
        cls.require_extension(protocol)
        entity, m = cls._parse_entity(m, protocol)
        delta, m = _parse_fte_delta(m, protocol)
        return cls(entity, delta), m


@_register_server_message
class ServerMessageFTEStatic(_FTEMessage):
    field_names = ('delta',)
    msg_type = ServerMessageType.FTE_SPAWNSTATIC

    @classmethod
    def parse(cls, m, protocol):
        cls.require_extension(protocol)
        delta, m = _parse_fte_delta(m, protocol)
        return cls(delta), m


@_register_server_message
class ServerMessageUpdateStatByte(_FTEMessage, StructServerMessage):
    field_names = ('index', 'value')
    fmt = '<BB'
    msg_type = ServerMessageType.UPDATESTATBYTE

    @classmethod
    def parse(cls, m, protocol):
        cls.require_extension(protocol)
        return super().parse(m, protocol)


@_register_server_message
class ServerMessageUpdateStatFloat(ServerMessageUpdateStatByte):
    fmt = '<Bf'
    msg_type = ServerMessageType.UPDATESTATFLOAT


@_register_server_message
class ServerMessageUpdateStatString(_FTEMessage):
    field_names = ('index', 'value')
    msg_type = ServerMessageType.UPDATESTATSTRING

    @classmethod
    def parse(cls, m, protocol):
        cls.require_extension(protocol)
        (index,), m = cls._parse_struct('<B', m)
        value, m = cls._parse_string(m)
        return cls(index, value), m


@_register_server_message
class ServerMessagePrecache(_FTEMessage):
    field_names = ('index', 'name')
    msg_type = ServerMessageType.PRECACHE

    @classmethod
    def parse(cls, m, protocol):
        cls.require_extension(protocol)
        (index,), m = cls._parse_struct('<H', m)
        name, m = cls._parse_string(m)
        return cls(index, name), m


@_register_server_message
class ServerMessageTrailParticles(_FTEMessage):
    field_names = ('entity_num', 'effect', 'start', 'end')
    msg_type = ServerMessageType.TRAILPARTICLES

    @classmethod
    def parse(cls, m, protocol):
        cls.require_extension(protocol)
        (entity,), m = cls._parse_struct('<H', m)
        (effect,), m = cls._parse_struct('<H', m)
        start, m = cls._parse_coords(m, protocol)
        end, m = cls._parse_coords(m, protocol)
        return cls(entity, effect, start, end), m


@_register_server_message
class ServerMessagePointParticles(_FTEMessage):
    field_names = ('effect', 'origin', 'velocity', 'count')
    msg_type = ServerMessageType.POINTPARTICLES

    @classmethod
    def parse(cls, m, protocol):
        cls.require_extension(protocol)
        (effect,), m = cls._parse_struct('<H', m)
        origin, m = cls._parse_coords(m, protocol)
        velocity, m = cls._parse_coords(m, protocol)
        (count,), m = cls._parse_struct('<H', m)
        return cls(effect, origin, velocity, count), m


@_register_server_message
class ServerMessagePointParticles1(_FTEMessage):
    field_names = ('effect', 'origin')
    msg_type = ServerMessageType.POINTPARTICLES1

    @classmethod
    def parse(cls, m, protocol):
        cls.require_extension(protocol)
        (effect,), m = cls._parse_struct('<H', m)
        origin, m = cls._parse_coords(m, protocol)
        return cls(effect, origin), m


@_register_server_message
class ServerMessageTempEntity(ServerMessage):
    field_names = ('temp_entity_type', 'entity_num', 'origin', 'end', 'color_start', 'color_length')
    msg_type = ServerMessageType.TEMP_ENTITY

    @classmethod
    def parse(cls, m, protocol):
        try:
            temp_entity_type = TempEntityTypes(m[0])
        except ValueError as exc:
            raise MalformedNetworkData(f"Invalid temp entity type: {m[0]}") from exc
        m = m[1:]

        if temp_entity_type in (TempEntityTypes.LIGHTNING1, TempEntityTypes.LIGHTNING2, TempEntityTypes.LIGHTNING3,
                                TempEntityTypes.BEAM):
            (entity_num,), m = cls._parse_struct("<H", m)
            origin, m = cls._parse_coords(m, protocol)
            end, m = cls._parse_coords(m, protocol)
        else:
            origin, m = cls._parse_coords(m, protocol)
            end = None
            entity_num = None

        if temp_entity_type == TempEntityTypes.EXPLOSION2:
            color_start, color_length, m = m[0], m[1], m[2:]
        else:
            color_start, color_length = None, None

        return cls(temp_entity_type, entity_num, origin, end, color_start, color_length), m


@_register_server_message
class ServerMessageKilledMonster(NoFieldsServerMessage):
    msg_type = ServerMessageType.KILLEDMONSTER


@_register_server_message
class ServerMessageIntermission(NoFieldsServerMessage):
    msg_type = ServerMessageType.INTERMISSION


@_register_server_message
class ServerMessageFinale(ServerMessage):
    field_names = ('string',)
    msg_type = ServerMessageType.FINALE

    @classmethod
    def parse(cls, m, protocol):
        s, m = cls._parse_string(m)
        return cls(s), m


@_register_server_message
class ServerMessageDisconnect(NoFieldsServerMessage):
    msg_type = ServerMessageType.DISCONNECT


@_register_server_message
class ServerMessageDamage(ServerMessage):
    field_names = ('armor', 'blood', 'origin')
    msg_type = ServerMessageType.DAMAGE

    @classmethod
    def parse(cls, m, protocol):
        armor, blood, m = m[0], m[1], m[2:]
        origin, m = cls._parse_coords(m, protocol)
        return cls(armor, blood, origin), m


def read_demo_file(f):
    while _read(f, 1) != b'\n':
        pass

    demo_header_fmt = "<Ifff"
    demo_header_size = struct.calcsize(demo_header_fmt)

    protocol = None

    while True:
        d = f.read(demo_header_size)
        if len(d) == 0:
            break
        if len(d) < demo_header_size:
            raise MalformedNetworkData
        msg_len, *view_angles = struct.unpack(demo_header_fmt, d)
        msg = _read(f, msg_len)
        while msg:
            parsed, msg = ServerMessage.parse_message(msg, protocol)
            if parsed.msg_type in (ServerMessageType.SERVERINFO, ServerMessageType.VERSION):
                protocol = parsed.protocol
            yield not bool(msg), view_angles, parsed


def clear_cache():
    """Some messages are cached for efficient parsing of repeated messages.

    Call this function to free up memory used by this cache.
    """

    ServerMessageUpdate.clear_cache()


def demo_parser_main():
    def f():
        import sys
        with open(sys.argv[1], "rb") as f:
            for msg in read_demo_file(f):
                if do_print:
                    print(msg)

    do_print = bool(int(os.environ.get('PYQ_PRINT', '1')))

    if int(os.environ.get('PYQ_PROFILE', '0')):
        import cProfile
        cProfile.runctx('f()', globals(), locals(), 'stats')
    else:
        f()
