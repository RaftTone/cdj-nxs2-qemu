# SPDX-License-Identifier: GPL-2.0-or-later
"""scripts/net/hid_handshake_host.py: the host side of the deck's HID handshake."""
import struct

import hid_handshake_host as hh

# A real exchange from an emulated CDJ-2000NXS2 (firmware 1.87), HID mode.
SEED_A = bytes.fromhex("00005d5e00004e06")
SEED_E = bytes.fromhex("35227052")
HASH_A = 0x559C246C
ANNOUNCE = bytes.fromhex("00 F0 01 00 01 00 11 06 01 04 01 57")


def test_spread_halves_each_byte_into_two_nibbles():
    assert hh.spread(bytes([0x12, 0x34])) == bytes([1, 2, 3, 4])
    assert hh.unspread(hh.spread(b"\xde\xad\xbe\xef")) == b"\xde\xad\xbe\xef"


def test_fnv1a_known_values():
    assert hh.fnv1a(b"") == 0x811C9DC5
    assert hh.fnv1a(b"a") == 0xE40C292C


def test_deck_hash_a_follows_the_public_recipe():
    secret = struct.pack(">I", int.from_bytes(SEED_E, "big") ^ hh.SECRET_PIONEERDJ)
    assert hh.fnv1a(SEED_A + secret) == HASH_A


def test_parse_announce_and_host_challenge():
    stage, args = hh.parse(ANNOUNCE.ljust(64, b"\0"))
    assert stage == 0x11 and args == {1: bytes([1, 0x57])}
    frame = hh.frame(0x12, [hh.tlv(1, b"PioneerDJ"), hh.tlv(2, b"rekordbox"),
                            hh.tlv(3, hh.spread(SEED_A))])
    assert len(frame) == 64 and frame[:6] == bytes.fromhex("00 F0 01 00 01 00")
    stage, args = hh.parse(frame)
    assert stage == 0x12
    assert args[1] == b"PioneerDJ" and args[2] == b"rekordbox"
    assert hh.unspread(args[3]) == SEED_A


def test_parse_ignores_anything_that_is_not_a_handshake_report():
    assert hh.parse(bytes.fromhex("00 20 00 00 00 80 60").ljust(64, b"\0")) is None
    assert hh.parse(bytes(64)) is None
