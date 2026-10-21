#!/usr/bin/env python3
"""GL49 USB probe: sweep the CP2110's UART for the meter's serial protocol.

The Beurer GL49 exposes its PC interface as a Silicon Labs CP2110
(HID USB-to-UART bridge, 10c4:ea80). The meter's MCU hangs off the
CP2110's UART pins; the wire protocol is undocumented. This tool
listens for unsolicited traffic and fires candidate request bytes at
every common baud rate, hex-dumping everything that comes back.

Run against the meter while it shows the USB symbol on its display:

    sudo python tools/gl49_probe.py            # auto-find the CP2110
    sudo python tools/gl49_probe.py --baud 9600  # single baud, deep probe
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

BAUDS = [9600, 19200, 38400, 57600, 115200, 4800, 2400, 1200]

# candidate requests: handshakes, ASCII commands, plausible Beurer frames
PROBES = [
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
    ("D", b"D"),
    ("G", b"G"),
    ("M", b"M"),
    ("a", b"a"),
    ("r", b"r"),
    ("s", b"s"),
    ("i", b"i"),
    ("d", b"d"),
    ("g", b"g"),
    ("m", b"m"),
    ("GET", b"GET\r"),
    ("ID", b"ID?\r"),
    ("beurer?", b"\x01\x03"),
    ("beurer2", b"\x02\x03"),
    ("beurer3", b"\x06\x03"),
]


def find_cp2110():
    for d in hid.enumerate(CP2110_VID, CP2110_PID):
        return d["path"].decode()
    return None


def drain(ser, seconds):
    """collect bytes for up to `seconds` (quiet-gap terminated)."""
    ser.timeout = 0.25
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            end = max(end, time.time() + 0.4)
        else:
            time.sleep(0.02)
    return buf


def hexs(b):
    return binascii.hexlify(b, " ").decode()


def probe_baud(ser, baud, deep):
    ser.baudrate = baud
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    except Exception:
        pass
    print(f"\n=== {baud} baud ===")
    got = drain(ser, 1.2)
    print(f"  idle-listen: {hexs(got) if got else '(nothing)'}")
    if got:
        print(f"  ASCII      : {got!r}")
    probes = PROBES if deep else PROBES[:12]
    for name, payload in probes:
        try:
            ser.write(payload)
        except Exception as exc:
            print(f"  write failed: {exc}")
            return
        got = drain(ser, 0.6)
        flag = "  <<<" if got else ""
        print(f"  {name:<9} -> {hexs(payload):<16} resp: {hexs(got) if got else '-'}{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", help="hidraw path (default: auto-find CP2110)")
    ap.add_argument("--baud", type=int, help="probe only this baud rate, all commands")
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

    # wake attempt: line break on the UART (some meters sleep their RX)
    try:
        ser.baudrate = 9600
        ser.break_condition = True
        time.sleep(0.15)
        ser.break_condition = False
    except Exception:
        pass

    try:
        if args.baud:
            probe_baud(ser, args.baud, deep=True)
        else:
            for baud in BAUDS:
                probe_baud(ser, baud, deep=False)
    finally:
        ser.close()
    print("\ndone. any line with '<<<' is the meter talking - rerun with --baud <that> for the full sweep")


if __name__ == "__main__":
    main()
