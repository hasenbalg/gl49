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


def frame(cmd, p2=0, p3=0):
    body = bytes([0x51, cmd, p2, p3, 0, 0, 0xA3])
    return body + bytes([sum(body) & 0xFF])


def swap_pairs(b):
    return bytes(x for i in range(0, len(b) - 1, 2) for x in (b[i + 1], b[i]))


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
            r = self.h.read(64, timeout_ms=50)
            if r:
                r = bytes(r)
                n = r[0]
                buf += r[1:1 + n] if n and len(r) > 1 else r
                t0 = time.time()
        return buf

    def cmd(self, fr, retries=40, expect=8):
        """Send frame, retrying every 250 ms until a reply arrives.
        The first frame only wakes the MCU; replies follow on retries."""
        for i in range(retries):
            self.h.write(bytes([len(fr)]) + fr)
            r = self._drain(quiet=0.25)
            if r:
                if i:
                    pass  # woke up on retry i
                return r
        return None

    def count(self):
        r = self.cmd(frame(0x2B))
        if not r or len(r) < 8:
            sys.exit(f"no 2B reply ({r!r})")
        print(f"2B resp: {r.hex(' ')}")
        return r[2] | r[3] << 8

    def fetch(self, idx):
        r = self.cmd(frame(0x85, idx & 0xFF, (idx >> 8) & 0xFF))
        return r

    def close(self):
        try:
            self.h.close()
        except Exception:
            pass


def main():
    g = Gl49Usb()
    total = g.count()
    print(f"records: {total}")
    for idx in range(0, total + 1, 2):
        r = g.fetch(idx)
        if not r:
            print(f"idx {idx}: (no reply)")
            continue
        print(f"idx {idx}: raw  {r.hex(' ')}  ({len(r)} bytes)")
        if len(r) >= 14:
            data = r[2:14]
            sw = swap_pairs(data)
            print(f"         data {data.hex(' ')}")
            print(f"         swapped {sw.hex(' ')}")
            for half in range(2):
                rec = sw[half * 6:half * 6 + 6]
                w0 = rec[0] | rec[1] << 8
                w1 = rec[2] | rec[3] << 8
                w2 = rec[4] | rec[5] << 8
                print(f"         rec{half}: {rec.hex(' ')}  "
                      f"words {w0:#06x} {w1:#06x} {w2:#06x}")
    g.close()


if __name__ == "__main__":
    main()
