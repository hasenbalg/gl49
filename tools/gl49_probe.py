#!/usr/bin/env python3
"""GL49 USB probe: talk to the meter over its CP2110 HID-UART bridge.

The Beurer GL49 exposes its PC interface as a Silicon Labs CP2110
(HID USB-to-UART bridge, 10c4:ea80). The meter protocol (extracted
from the vendor PC software):

  * serial 19200 baud, 8N1, read timeout ~1 s, ~50 ms between frames
  * FORA-style 8-byte frames: 51 CMD p2 p3 p4 p5 A3 CK
    with CK = (sum of first 7 bytes) & 0xFF; meter echoes CMD
  * CMD 0x24 device ID, 0x29 device info, 0x2B record count,
    0x85 idxL idxH -> 16-byte response with TWO 6-byte records
    (trailer byte 0xA5 at offset 14)

Run against the meter while it shows the USB symbol on its display:

    sudo python tools/gl49_probe.py               # protocol probe (19200)
    sudo python tools/gl49_probe.py --sweep       # blind sweep
    sudo python tools/gl49_probe.py --baud 9600   # protocol at one baud
"""

import argparse
import binascii
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("needs hidapi:  pip install hidapi pyserial")

import serial

CP2110_VID = 0x10C4
CP2110_PID = 0xEA80

def fora_frame(cmd, p2=0, p3=0, p4=0, p5=0):
    f = bytes([0x51, cmd, p2, p3, p4, p5, 0xA3])
    return f + bytes([sum(f) & 0xFF])


# Commands extracted from the vendor PC software (see FINDINGS.md):
#   download loop: 0x2B count (8-byte resp), then 0x85 idxL idxH
#   per pair of records (16-byte resp, 2x 6-byte records, 0xA5 trailer)
def protocol_probes():
    return [
        ("24 ID", fora_frame(0x24)),
        ("29 info", fora_frame(0x29)),
        ("2B count", fora_frame(0x2B)),
        ("85 rec0", fora_frame(0x85, 0, 0)),
        ("27 lot1", fora_frame(0x27)),
        ("28 lot2", fora_frame(0x28)),
    ]

SWEEP_PROBES = [
    ("ENQ", b"\x05"),
    ("DLE-ENQ", b"\x10\x05"),
    ("STX-idle", b"\x02"),
    ("ESC-@", b"\x1b@"),
    ("CR", b"\r"),
    ("CRLF", b"\r\n"),
    ("LF", b"\n"),
    ("0xFF", b"\xff"),
    ("A", b"A"),
    ("R", b"R"),
    ("S", b"S"),
    ("I", b"I"),
]

SWEEP_BAUDS = [9600, 19200, 38400, 57600, 115200, 4800, 2400, 1200]


def find_cp2110():
    for d in hid.enumerate(CP2110_VID, CP2110_PID):
        return d["path"].decode()
    return None


def drain(ser, seconds, quiet=0.3):
    """collect bytes for up to `seconds` (quiet-gap terminated)."""
    ser.timeout = 0.15
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            end = max(end, time.time() + quiet)
        else:
            time.sleep(0.02)
    return buf


def hexs(b):
    return binascii.hexlify(b, " ").decode()


def probe_baud(ser, baud, probes, settle=0.13):
    ser.baudrate = baud
    try:
        ser.dtr = True
        ser.rts = True
    except Exception:
        pass
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    except Exception:
        pass
    print(f"\n=== {baud} baud ===")
    got = drain(ser, 1.0)
    print(f"  idle-listen: {hexs(got) if got else '(nothing)'}")
    if got:
        print(f"  ASCII      : {got!r}")
    for name, payload in probes:
        try:
            ser.reset_input_buffer()
            ser.write(payload)
            ser.flush()
        except Exception as exc:
            print(f"  write failed: {exc}")
            return
        time.sleep(settle)
        got = drain(ser, 1.5)
        flag = "  <<<" if got else ""
        print(f"  {name:<9} -> {hexs(payload):<16} resp: {hexs(got) if got else '-'}{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", help="hidraw path (default: auto-find CP2110)")
    ap.add_argument("--baud", type=int, help="probe only this baud rate")
    ap.add_argument("--sweep", action="store_true",
                    help="blind sweep (handshakes, many bauds)")
    ap.add_argument("--no-dtr", action="store_true",
                    help="leave DTR deasserted (default: asserted)")
    args = ap.parse_args()

    path = args.path or find_cp2110()
    if not path:
        sys.exit("no CP2110 (10c4:ea80) found - replug the meter (meter OFF), "
                 "use the port that enumerated, check `lsusb | grep 10c4`")
    print(f"opening CP2110 at {path}")
    try:
        ser = serial.serial_for_url(f"cp2110://{path}")
    except Exception as exc:
        sys.exit(f"cannot open {path}: {exc}\n(hint: run with sudo, or add a udev rule for 10c4:ea80)")

    probes = SWEEP_PROBES if args.sweep else protocol_probes()
    bauds = [args.baud] if args.baud else (
        SWEEP_BAUDS if args.sweep else [19200, 9600])

    try:
        for baud in bauds:
            probe_baud(ser, baud, probes)
    finally:
        try:
            ser.close()
        except Exception:
            pass  # pyserial cp2110 backend teardown bug

    print("\ndone. any line with '<<<' is the meter talking.")
    print("If 2B answered, next step is the 0x85 record download.")


if __name__ == "__main__":
    main()
