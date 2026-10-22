#!/usr/bin/env python3
"""GL49 USB record downloader (CP2110 HID-UART, FORA frames @19200).

Live-verified recipe: the meter MCU only answers AFTER the first poll
frame wakes it, so every command is retried until it replies.

  51 2B 00 00 00 00 A3 1F        -> 51 2B <cnt lo> <cnt hi> ? ? A5 CK
  51 85 idxL idxH 00 00 A3 CK    -> record pair (12 data bytes)

CK = sum(first 7 bytes) & 0xFF.  Response words are byte-swapped on
the wire (see vendor binary: adjacent pairs swapped before use).
"""

import struct
import sys
import time

import hid

VID, PID = 0x10C4, 0xEA80


class SessionLost(RuntimeError):
    """USB device vanished mid-session (unplug / meter left transfer mode)."""


def frame(cmd, p2=0, p3=0):
    body = bytes([0x51, cmd, p2, p3, 0, 0, 0xA3])
    return body + bytes([sum(body) & 0xFF])


def swap_pairs(b):
    return bytes(x for i in range(0, len(b) - 1, 2) for x in (b[i + 1], b[i]))


MEAL = {0: "", 1: " [before meal]", 2: " [after meal]", 3: " [QC]"}


def decode_record(rec):
    """6-byte GL49 record, EXACT vendor parser (GlucoMemory 0x418fa0):
    value  = r5 | ((r4 & 3) << 8)
    year   = 1984 + (r0 >> 1)        (epoch pinned vs meter display)
    month  = (r1 >> 5) | ((r0 & 1) << 3)
    day    = r1 & 0x1F
    hour   = r2 & 0x1F
    minute = r3 & 0x3F
    r4>>2 and r2>>5, r3>>6 are NOT parsed by the vendor app (unknown).
    """
    r0, r1, r2, r3, r4, r5 = rec
    value = r5 | ((r4 & 3) << 8)
    year = 1984 + (r0 >> 1)
    month = (r1 >> 5) | ((r0 & 1) << 3)
    day = r1 & 0x1F
    hour = r2 & 0x1F
    minute = r3 & 0x3F
    if not (day and 1 <= month <= 12 and hour < 24 and minute < 60):
        return None
    return (f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}  "
            f"{value:4d} mg/dL")


class Gl49Usb:
    def __init__(self):
        devs = hid.enumerate(VID, PID)
        if not devs:
            sys.exit(f"no CP2110 {VID:04x}:{PID:04x} found "
                     "(meter OFF -> plug USB -> wait for USB symbol)")
        self.h = hid.device()
        self.h.open_path(devs[0]["path"])
        self.h.set_nonblocking(0)
        cfg = bytes([0x50]) + struct.pack(">IBBBB", 19200, 0, 0, 3, 0)
        self.h.send_feature_report(cfg)
        self.h.send_feature_report(bytes([0x41, 0x01]))
        self.h.send_feature_report(bytes([0x43, 0x03]))

    def _drain(self, quiet=0.3, limit=64):
        buf = b""
        t0 = time.time()
        while time.time() - t0 < quiet and len(buf) < limit:
            try:
                r = self.h.read(64, timeout_ms=50)
            except OSError:
                raise SessionLost() from None
            if r:
                r = bytes(r)
                n = r[0]
                buf += r[1:1 + n] if n and len(r) > 1 else r
                t0 = time.time()
        return buf

    def cmd(self, fr, retries=40, expect_cmd=None, min_len=8):
        """Send frame, retrying every 250 ms until a VALID reply arrives.
        The first frame only wakes the MCU; replies follow on retries.
        A reply is valid if it starts 51 <expect_cmd> ... A5 with a
        correct trailing checksum (sum of all preceding bytes & 0xFF)."""
        for i in range(retries):
            self.h.write(bytes([len(fr)]) + fr)
            r = self._drain(quiet=0.25)
            if not r:
                continue
            ok = (len(r) >= min_len and r[0] == 0x51
                  and (expect_cmd is None or r[1] == expect_cmd)
                  and sum(r[:-1]) & 0xFF == r[-1])
            if ok:
                return r
            print(f"  (bad frame: {r.hex(' ')})")
        return None

    def wait_for_session(self, timeout=90.0):
        """Poll 2B until the meter answers — plug the meter in NOW
        (OFF -> cable -> USB symbol) while this runs."""
        print(f"waiting up to {timeout:.0f} s for a fresh session — "
              "replug the meter now (OFF -> cable -> wait for symbol) ...")
        fr = frame(0x2B)
        t_end = time.time() + timeout
        while time.time() < t_end:
            self.h.write(bytes([len(fr)]) + fr)
            r = self._drain(quiet=0.2)
            if (len(r) >= 8 and r[0] == 0x51 and r[1] == 0x2B
                    and sum(r[:-1]) & 0xFF == r[-1]):
                print(f"  session live: {r.hex(' ')}")
                return r
        return None

    def count(self):
        r = self.cmd(frame(0x2B), expect_cmd=0x2B)
        if not r:
            sys.exit("no valid 2B reply")
        print(f"2B resp: {r.hex(' ')}")
        return r[2] | r[3] << 8

    def fetch(self, idx):
        return self.cmd(frame(0x85, idx & 0xFF, (idx >> 8) & 0xFF),
                        expect_cmd=0x85, min_len=16)

    def close(self):
        try:
            self.h.close()
        except Exception:
            pass


def dump(label, r):
    if not r:
        print(f"{label}: (no reply)")
        return
    print(f"{label}: {r.hex(' ')}  ({len(r)} bytes)")


def main():
    g = Gl49Usb()
    try:
        run(g)
    except SessionLost:
        g.close()
        sys.exit("device unplugged / meter left transfer mode — replug and rerun")


def run(g):
    # The meter leaves PC-transfer mode shortly after plugging in, so
    # wait for a live session (replug while this runs) then download fast.
    if g.wait_for_session() is None:
        sys.exit("no session — replug (meter OFF -> cable) and retry")
    dump("24 ID   ", g.cmd(frame(0x24), expect_cmd=0x24))
    dump("29 info ", g.cmd(frame(0x29), expect_cmd=0x29))
    total = g.count()
    print(f"records: {total}")
    # idx N carries records 2N and 2N+1 (12 bytes = two 6-byte records)
    for idx in range((total + 1) // 2):
        r = g.fetch(idx)
        dump(f"idx {idx}", r)
        if r and len(r) >= 14:
            data = r[2:14]
            for half in range(2):
                rec = data[half * 6:half * 6 + 6]
                if rec == b"\x00" * 6:
                    continue
                dec = decode_record(rec)
                print(f"    rec{2 * idx + half}: {rec.hex(' ')}  "
                      f"-> {dec if dec else '(undecoded)'}")
    g.close()


if __name__ == "__main__":
    main()
