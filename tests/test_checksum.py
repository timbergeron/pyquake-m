import struct
import unittest

from pyquake import aiodgram, checksum


class ChecksumTests(unittest.TestCase):
    def test_rfc_1320_vectors(self):
        vectors = (
            (b"", "31d6cfe0d16ae931b73c59d7e0c089c0"),
            (b"a", "bde52cb31de33e46245e05fbdbd6fb24"),
            (b"abc", "a448017aaf21d8525fc10ae87aa6729d"),
            (b"message digest", "d9130a8164549fe818874806e1c7014b"),
            (b"abcdefghijklmnopqrstuvwxyz", "d79e1c308aa5bbcdeea8ed63df412da9"),
            (b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
             "043f8582f241db351ce627e153e7f0e4"),
            (b"1234567890" * 8, "e33b4ddc9c38f2199c3e7b164fcc0536"),
        )
        for data, expected in vectors:
            with self.subTest(data=data):
                self.assertEqual(checksum._md4(data).hex(), expected)
                a, b, c, d = struct.unpack("<4I", bytes.fromhex(expected))
                self.assertEqual(checksum.block_checksum(data), a ^ b ^ c ^ d)

    def test_password_numeric_formats_and_hash_fallback(self):
        vectors = {"": 0, "none": 0, "0": 0, "1234": 1234, "0x1234": 0x1234,
                   "0123": 0o123, "-1": 0xffffffff, " +42": 42,
                   "password": 0xe4670ad4, "08": 0x5584b7af}
        for password, expected in vectors.items():
            with self.subTest(password=password):
                self.assertEqual(aiodgram._proquake_password(password), expected)
