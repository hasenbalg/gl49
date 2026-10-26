#!/usr/bin/env python3
"""GL49 USB diagnostic downloader — thin wrapper over the glucobridge
`gl49_usb` backend (protocol details + record decode live there).

Usage:  sudo .venv/bin/python tools/gl49_usb.py
Switch the meter OFF, plug the USB cable while it runs, wait for the
USB symbol on the LCD. Prints raw frames + decoded records.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from glucobridge.backends.gl49_usb import (  # noqa: E402
    Gl49UsbMeter, SessionLost, UsbError, frame, parse_record, swap_pairs)


def dump(label, r):
    print(f"{label}: {r.hex(' ') if r else '(no reply)'}"
          + (f"  ({len(r)} bytes)" if r else ""))


def main():
    m = Gl49UsbMeter()
    try:
        m.connect()
    except UsbError as e:
        sys.exit(str(e))
    try:
        if not m.wait_for_session(90.0):
            sys.exit("no session — meter OFF -> plug USB -> wait for the "
                     "USB symbol, then retry")
        r = m.cmd(frame(0x24), expect_cmd=0x24)
        dump("24 ID   ", r)
        dump("29 info ", m.cmd(frame(0x29), expect_cmd=0x29))
        total = m.record_count()
        print(f"records: {total}")
        for idx in range((total + 1) // 2):
            r = m.cmd(frame(0x85, idx & 0xFF, (idx >> 8) & 0xFF),
                      expect_cmd=0x85, min_len=16)
            dump(f"idx {idx}", r)
            if r and len(r) >= 14:
                for half in range(2):
                    rec = r[2 + half * 6: 8 + half * 6]
                    if rec == b"\x00" * 6:
                        continue
                    rd = parse_record(rec)
                    sw = swap_pairs(rec)
                    print(f"    rec{2 * idx + half}: {rec.hex(' ')}  -> "
                          f"{rd if rd else '(undecoded)'}")
                    # unknown fields the vendor app ignores (meal-mark cand.)
                    print(f"        sw={sw.hex(' ')}  "
                          f"r2hi={sw[2] >> 5}  r3hi={sw[3] >> 6}  "
                          f"r4hi={sw[4] >> 2}")
    except SessionLost as e:
        sys.exit(f"{e} — replug and retry")
    finally:
        m.close()


if __name__ == "__main__":
    main()
