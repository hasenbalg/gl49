#!/usr/bin/env python3
"""GL49 raw-HID diagnostic: prove where the bytes die.

Bypasses pyserial entirely and drives the CP2110 (10c4:ea80) through
hidapi, printing every return code:

  1. GET_FEATURE 0x01 (GPIO state)  -> proves HID comms with the chip
  2. SET_FEATURE UART config+enable -> proves the UART is configured
  3. OUTPUT report with a 0x2B frame -> proves TX leaves the host
  4. INPUT reports for 2 s          -> shows whether the meter answers

If (1) fails, the HID path is broken (port/permissions/dodgy contact).
If (1)-(3) are clean but (4) is silent, the meter's MCU is not
listening: it only talks on the CP2110's UART in PC-transfer mode —
unplug, switch the meter OFF, plug USB back in while OFF until the USB
symbol appears on the meter display, then rerun.
"""

import sys
import time

import hid

VID, PID = 0x10C4, 0xEA80

FRAME = bytes([0x51, 0x2B, 0, 0, 0, 0, 0xA3, 0x1F])  # record count


def main():
    devs = hid.enumerate(VID, PID)
    if not devs:
        sys.exit(f"no CP2110 {VID:04x}:{PID:04x} found "
                 "(replug; meter OFF first)")
    d = devs[0]
    print(f"opening {d['path'].decode()} (serial {d.get('serial_number_string')})")
    h = hid.device()
    h.open_path(d["path"])
    h.set_nonblocking(0)

    # 1. GPIO feature report — does the chip talk back at all?
    try:
        r = h.get_feature_report(0x01, 10)
        print(f"1. GET_FEATURE 0x01 (GPIO): {bytes(r).hex(' ')}")
    except Exception as e:
        print(f"1. GET_FEATURE 0x01 FAILED: {e}  <- HID path broken")

    # 2. UART config 19200 8N1 + enable (AN434 feature reports)
    import struct
    cfg = bytes([0x50]) + struct.pack("<IBBBB", 19200, 0, 0, 3, 0)
    rc = h.send_feature_report(cfg)
    print(f"2a. SET_FEATURE UART_CONFIG 19200 8N1 -> rc {rc}")
    rc = h.send_feature_report(bytes([0x41, 0x01]))
    print(f"2b. SET_FEATURE UART_ENABLE          -> rc {rc}")

    # 3. write a frame as an OUTPUT report (AN434: byte 0 = payload length)
    n = h.write(bytes([len(FRAME)]) + FRAME)
    print(f"3.  OUTPUT report {FRAME.hex(' ')} -> wrote {n} bytes")

    # 4. listen for INPUT reports (meter replies arrive here)
    print("4.  listening 2 s for INPUT reports ...")
    t0 = time.time()
    got = 0
    while time.time() - t0 < 2.0:
        r = h.read(64)
        if r:
            got += 1
            print(f"    INPUT: {bytes(r).hex(' ')}")
        else:
            time.sleep(0.01)
    if not got:
        print("    (nothing)")
        print("\nHID + UART side is fine => meter MCU is not answering.")
        print("Make sure the meter shows the USB SYMBOL on its display:")
        print("unplug -> switch meter OFF -> plug USB while OFF -> wait for symbol")
        print("then rerun this tool. If the symbol never appears, the")
        print("meter never enters PC-transfer mode and no protocol works.")
    h.close()


if __name__ == "__main__":
    main()
