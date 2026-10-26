#!/usr/bin/env python3
"""Probe the GlucoCheck Advance UART dialect behind its CP2110 bridge.

The meter enumerates the same 10c4:ea80 CP2110 HID-UART bridge as the
Beurer GL 49, but its firmware dialect is unknown: candidates are the
FORA frame protocol (GL49, works with `usb-dump --backend gl49`
plumbing) and the TaiDoc TD-42xx family (same 51/.. /A3/CK frame shape,
19200 baud, but per-record 0x25/0x26 reads and a 0x22 connect first).

This script hammers frames and prints RAW replies — no validation — so
the capture can be decoded offline.  Run against a live meter:

    sudo .venv/bin/python tools/glucocheck_probe.py

Keep the meter powered on; if the line stays silent, replug the cable
(mirrors the GL49's off->plug->session-window behaviour).
"""
import time
import sys

from glucobridge.backends.gl49_usb import Gl49UsbMeter, UsbError


def frame(cmd: int, p2=0, p3=0, p4=0, p5=0) -> bytes:
    f = bytes([0x51, cmd, p2, p3, p4, p5, 0xA3])
    return f + bytes([sum(f) & 0xFF])


def send(m, fr, label, wait=0.25):
    m.h.write(bytes([len(fr)]) + fr)
    r = m._drain(quiet=wait)
    print(f"{label:<28} -> {r.hex() if r else '(silent)'}", flush=True)
    return r


def main() -> int:
    try:
        m = Gl49UsbMeter()
        m.connect()
    except UsbError as e:
        print(f"error: {e}")
        return 1
    print("CP2110 open @19200 8N1; hammering frames (meter should be ON)")

    # TD-42xx opens with a 0x22 connect request; FORA has no connect
    # step, so also try plain 0x24 (model) — whichever answers first
    # identifies the family.
    got = None
    t_end = time.time() + 30
    i = 0
    while time.time() < t_end and not got:
        cmd = {0: 0x22, 1: 0x24}.get(i % 2, 0x22)
        r = send(m, frame(cmd), f"hammer {i:02d} cmd 0x{cmd:02x}",
                 wait=0.15)
        if r:
            got = cmd
        i += 1
    if not got:
        print("line stayed silent after 30 attempts — replug cable / "
              "power the meter and rerun")
        m.close()
        return 1
    print(f"\nfirst reply came from 0x{got:02x} — running identity set\n")

    for name, cmd, msg in (
        ("model 0x24", 0x24, ()),
        ("datetime 0x23", 0x23, ()),
        ("count 0x2B", 0x2B, ()),
    ):
        send(m, frame(cmd, *msg), name)

    r = send(m, frame(0x2B), "count reread")
    total = None
    if r and len(r) >= 8:
        total = r[2] | (r[3] << 8)
        print(f"  count (if TD42xx u16le) = {total}")

    # one record both ways so we can compare dialects
    for cid in (0, 1):
        send(m, frame(0x25, cid, 0, 0, 0), f"record {cid} datetime 0x25")
        send(m, frame(0x26, cid, 0, 0, 0), f"record {cid} value 0x26")
    send(m, frame(0x85, 0, 0, 0, 0), "GL49 dual-fetch 0x85 idx0")
    m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
