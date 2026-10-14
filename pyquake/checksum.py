"""Legacy Quake checksums used by the ProQuake connection handshake."""

import struct


def _md4(data: bytes) -> bytes:
    """MD4 as specified in RFC 1320 (https://www.rfc-editor.org/rfc/rfc1320).

    Kept here because MD4 is not available in all Python/OpenSSL builds.
    This is only for compatibility with the legacy wire protocol.
    """
    bit_length = (len(data) * 8) & 0xffffffffffffffff
    data += b"\x80" + b"\0" * ((55 - len(data)) % 64)
    data += struct.pack("<Q", bit_length)
    state = (0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476)
    rounds = (
        (range(16), (3, 7, 11, 19), 0),
        ((0, 4, 8, 12, 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15),
         (3, 5, 9, 13), 0x5a827999),
        ((0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15),
         (3, 9, 11, 15), 0x6ed9eba1),
    )
    for offset in range(0, len(data), 64):
        words = struct.unpack_from("<16I", data, offset)
        a, b, c, d = state
        for round_number, (order, shifts, constant) in enumerate(rounds):
            for step, index in enumerate(order):
                if round_number == 0:
                    value = (b & c) | (~b & d)
                elif round_number == 1:
                    value = (b & c) | (b & d) | (c & d)
                else:
                    value = b ^ c ^ d
                value = (a + value + words[index] + constant) & 0xffffffff
                shift = shifts[step % 4]
                value = ((value << shift) | (value >> (32 - shift))) & 0xffffffff
                a, b, c, d = d, value, b, c
        state = tuple((old + new) & 0xffffffff
                      for old, new in zip(state, (a, b, c, d)))
    return struct.pack("<4I", *state)


def block_checksum(data: bytes) -> int:
    """Return Quake's Com_BlockChecksum: XOR the four little-endian MD4 words."""
    a, b, c, d = struct.unpack("<4I", _md4(data))
    return a ^ b ^ c ^ d
