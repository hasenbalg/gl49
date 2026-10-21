#!/usr/bin/env python3
"""GL49 USB probe: talk to the meter over its CP2110 HID-UART bridge.

The Beurer GL49 exposes its PC interface as a Silicon Labs CP2110
(HID USB-to-UART bridge, 10c4:ea80). The meter protocol
(reverse-engineered from the vendor PC software):

  * serial 1200 baud, 8N1, DTR asserted, read timeouts ~1.2 s
  * commands are single ASCII characters:
      'E'  handshake/ID   -> 5-byte response
      'a'  read records   -> 32-byte response chunks (polled)
      'P'  set time (?)
  * the PC waits ~100-130 ms after each command before reading

Run against the meter while it shows the USB symbol on its display:

    sudo python tools/gl49_probe.py               # protocol probe (1200/9600)
    sudo python tools/gl49_probe.py --sweep       # old blind sweep
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

# Commands (reverse-engineered):
#   'E': purge, write, sleep 100 ms, read 5 bytes
#   'a': handshake, write, sleep 130 ms, read 32-byte chunks
PROTOCOL_PROBES = [
    ("E (ID)", b"E"),
    ("a (all)", b"a"),
    ("P (time)", b"P"),
    ("e", b"e"),
    ("A", b"A"),
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

    probes = SWEEP_PROBES if args.sweep else PROTOCOL_PROBES
    bauds = [args.baud] if args.baud else (
        SWEEP_BAUDS if args.sweep else [1200, 9600])

    try:
        for baud in bauds:
            probe_baud(ser, baud, probes)
    finally:
        try:
            ser.close()
        except Exception:
            pass  # pyserial cp2110 backend teardown bug

    print("\ndone. any line with '<<<' is the meter talking.")
    print("If 'E' answered, next step is the 'a' record download.")


if __name__ == "__main__":
    main()
