#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""A tiny test host for the deck's HID handshake, on the same UDP relay as
usb_hid_port.py. It plays the host side (what rekordbox, Traktor or Serato do),
so the handshake can be checked end to end without a physical deck:

    deck: 0x11 announce          host: 0x12 challenge (SeedA)
    deck: 0x13 reply (HashA, SeedE)   host: 0x14 reply (HashE, device id)
    deck: 0x15 ack               host: keepalive

Frame layout and algorithm follow the public notes at swiftb0y.github.io/CDJHidProtocol:
byte 0 the report id (0), byte 1 0xF0, then a 2+2 byte fragment header, then the
super-TLV [type][length incl. header] holding argument TLVs. Buffers in arguments
3, 4 and 5 are "spread" (each byte sent as two nibbles). FNV-1a 32 bit hashes.

What the NXS2 model needs from a host (all three cost time to find):
  * the product string in the 0x14 reply must be the one the host sent in its own
    0x12 (not the deck's "CDJ2000NXS2");
  * a one-packet 0x14 goes with fragment header index 1, total 1;
  * from the 0x12 on, send an all-zero report about every 0.4 s, or the deck
    drops the handshake after about a second of silence.

  usage: python3 scripts/net/hid_handshake_host.py [--port 27020] [--seconds 20]
  start the deck with CDJ_USBF_HOST=27020, then LINK > CONTROL MODE > USB MIDI.
"""
import argparse
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usb_hid_port import Deck  # noqa: E402

MANUFACTURER = b"PioneerDJ"
PRODUCT = b"rekordbox"
# the public table: vendor-specific device id for PioneerDJ x CDJ2000NXS2
DEVICE_ID = bytes.fromhex("02F2F4510ED30A2C1042")
SECRET_PIONEERDJ = 0x680131FB       # public table of manufacturer values
REPORT_LEN = 64


def fnv1a(buf):
    h = 0x811C9DC5
    for b in buf:
        h = ((h ^ b) * 0x1000193) & 0xFFFFFFFF
    return h


def spread(buf):
    return bytes(n for b in buf for n in (b >> 4, b & 15))


def unspread(buf):
    return bytes((buf[i] << 4) | buf[i + 1] for i in range(0, len(buf) - 1, 2))


def tlv(t, payload):
    return bytes([t, len(payload) + 2]) + payload


def frame(stage, args, i=1, n=1):
    body = tlv(stage, b"".join(args))
    f = bytes([0, 0xF0]) + struct.pack("<HH", i, n) + body
    return f.ljust(REPORT_LEN, b"\0")


def parse(report):
    """(stage, {arg type: payload}) of a handshake frame, or None."""
    if len(report) < 8 or report[1] != 0xF0:
        return None
    stage, length = report[6], report[7]
    end = 6 + length
    args, p = {}, 8
    while p + 2 <= end:
        t, ln = report[p], report[p + 1]
        if ln < 2:
            break
        args[t] = bytes(report[p + 2:p + ln])
        p += ln
    return stage, args


def log(msg):
    now = time.time()
    print("%s.%03d %s" % (time.strftime("%H:%M:%S", time.localtime(now)),
                          now % 1 * 1000, msg), flush=True)


class Host:
    def __init__(self, sock, deck):
        self.sock, self.deck = sock, deck
        self.seed_a = b""
        self.done_at = None
        self.last_keepalive = 0.0
        self.hash_ok = None
        self.talking = False

    def on_report(self, report, vt):
        report = bytes(report)
        info = parse(report)
        hexs = report.rstrip(b"\0").hex(" ").upper()
        if info is None:
            log("deck -> host  %s" % (hexs or "(all zero: reset)"))
            return
        stage, args = info
        log("deck -> host  stage 0x%02X  %s" % (stage, hexs))
        if stage == 0x11:
            self.talking = True
            maj_min = args.get(1, b"")
            if len(maj_min) == 2:
                log("  firmware %d.%d" % (maj_min[0], maj_min[1]))
            self.seed_a = b"\0\0" + os.urandom(2) + b"\0\0" + os.urandom(2)
            self.send(frame(0x12, [tlv(1, MANUFACTURER), tlv(2, PRODUCT),
                                   tlv(3, spread(self.seed_a))]),
                      "0x12 challenge, SeedA=" + self.seed_a.hex())
        elif stage == 0x13:
            hash_a = unspread(args.get(4, b""))
            seed_e = unspread(args.get(3, b""))
            log("  manufacturer=%r product=%r HashA=%s SeedE=%s"
                % (args.get(1), args.get(2), hash_a.hex(), seed_e.hex()))
            secret = struct.pack(">I", int.from_bytes(seed_e, "big") ^ SECRET_PIONEERDJ)
            self.hash_ok = fnv1a(self.seed_a + secret) == int.from_bytes(hash_a, "big")
            log("  HashA %s the public recipe FNV1a(SeedA + (SeedE xor 0x%08X))"
                % ("MATCHES" if self.hash_ok else "DOES NOT MATCH", SECRET_PIONEERDJ))
            hash_e = fnv1a(seed_e + secret)
            self.send(frame(0x14, [tlv(1, MANUFACTURER), tlv(2, PRODUCT),
                                   tlv(4, spread(struct.pack(">I", hash_e))),
                                   tlv(5, spread(DEVICE_ID))]),
                      "0x14 reply, HashE=%08x" % hash_e)
        elif stage == 0x15:
            self.done_at = time.time()
            log("  ACK: the deck accepted the handshake")
            self.talking = True

    def send(self, report, what):
        log("host -> deck  %s  %s" % (what, report.rstrip(b"\0").hex(" ").upper()))
        self.deck.send(self.sock, report)

    def keepalive(self, now, every=0.4):
        if self.talking and now - self.last_keepalive >= every:
            self.last_keepalive = now
            self.deck.send(self.sock, bytes(REPORT_LEN))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=27020)
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="how long to keep going after the ack (keepalive)")
    ap.add_argument("--wait", type=float, default=600.0,
                    help="give up if the deck never announces itself")
    args = ap.parse_args(argv[1:])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", args.port))
    sock.settimeout(0.01)
    holder = {}
    deck = Deck(lambda r, vt: holder["host"].on_report(r, vt))
    host = holder["host"] = Host(sock, deck)
    log("hid_handshake_host: 127.0.0.1:%d, waiting for the deck" % args.port)
    start = time.time()
    descs = None
    while True:
        now = time.time()
        try:
            data, addr = sock.recvfrom(65535)
            deck.datagram(data, addr)
            if deck.hid_desc and deck.hid_desc != descs:
                descs = deck.hid_desc
                log("deck report descriptor (%d bytes): %s" % (len(descs), descs.hex()))
        except socket.timeout:
            pass
        host.keepalive(now)
        if host.done_at and now - host.done_at > args.seconds:
            log("kept alive for %.0f s; done" % args.seconds)
            return 0
        if not host.done_at and now - start > args.wait:
            log("the deck never completed the handshake")
            return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        pass
