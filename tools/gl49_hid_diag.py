#!/usr/bin/env python3
"""GL49 raw-HID diagnostic per AN434 (CP2110 report IDs 0x40-0x66).

Drives the CP2110 (10c4:ea80) through hidapi with the CORRECT feature
report IDs and prints every return code:

  GET  0x46 version      -> part 0x0A proves chip + HID control-IN path
  SET/GET 0x50 config    -> 19200 8N1 readback
  SET/GET 0x41 enable    -> UART enable readback
  SET  0x43 purge        -> clean FIFOs
  OUTPUT report (len-prefixed) with a 0x2B frame
  GET  0x42 status       -> TX/RX FIFO counts: THE tell
      TX FIFO empty  = bytes physically left the chip on UART TX
      RX FIFO > 0    = meter answered (INPUT reports carry it)
      RX FIFO 0      = meter deaf (transfer mode / wiring)
  INPUT report listen    -> non-blocking read with timeout (no hang)

If TX drains and RX stays 0, the meter MCU is not listening: it only
talks on the CP2110's UART in PC-transfer mode — unplug, switch the
meter OFF, plug USB back in while OFF until the USB symbol appears on
the meter display, then rerun.
"""

import struct
import sys
import time

import hid

VID, PID = 0x10C4, 0xEA80
FRAME = bytes([0x51, 0x2B, 0, 0, 0, 0, 0xA3, 0x1F])  # record count


def get(h, rid, n=64, label=""):
    try:
        r = bytes(h.get_feature_report(rid, n))
        print(f"GET  0x{rid:02X} {label}: {r.hex(' ')}")
        return r
    except Exception as e:
        print(f"GET  0x{rid:02X} {label}: FAILED ({e})")
        return None


def main():
    devs = hid.enumerate(VID, PID)
    if not devs:
        sys.exit(f"no CP2110 {VID:04x}:{PID:04x} found (replug; meter OFF first)")
    d = devs[0]
    print(f"opening {d['path'].decode()}")
    h = hid.device()
    h.open_path(d["path"])
    h.set_nonblocking(0)

    # chip identity — part number must be 0x0A for a CP2110
    get(h, 0x46, 3, "version (want part 0x0A)")

    # report 0x50: baud LE4, parity, flow, data bits (3=8), stop bits (1=1)
    cfg = bytes([0x50]) + struct.pack("<IBBBB", 19200, 0x00, 0x00, 0x03, 0x01)
    rc = h.send_feature_report(cfg)
    print(f"SET  0x50 UART_CONFIG 19200 8N1 -> rc {rc}")
    get(h, 0x50, 9, "config readback")

    rc = h.send_feature_report(bytes([0x41, 0x01]))
    print(f"SET  0x41 UART_ENABLE 1 -> rc {rc}")
    get(h, 0x41, 2, "enable readback (want 01)")

    h.send_feature_report(bytes([0x43, 0x03]))  # purge both FIFOs
    print("SET  0x43 purge both FIFOs")

    n = h.write(bytes([len(FRAME)]) + FRAME)
    print(f"OUT  {FRAME.hex(' ')} -> wrote {n} bytes")

    for t in (0.1, 0.5, 1.0):
        time.sleep(t)
        r = get(h, 0x42, 7, "status")
        if r and len(r) >= 5:
            tx = r[1] | r[2] << 8
            rx = r[3] | r[4] << 8
            print(f"     -> TX FIFO {tx} bytes, RX FIFO {rx} bytes")

    print("listening 2 s for INPUT reports ...")
    t0 = time.time()
    got = 0
    while time.time() - t0 < 2.0:
        r = h.read(64, timeout_ms=100)
        if r:
            got += 1
            print(f"INPUT: {bytes(r).hex(' ')}")
    if not got:
        print("(no INPUT reports)")
        print("\nIf TX FIFO drained and RX FIFO stayed 0, the meter MCU is")
        print("deaf: get the USB symbol on the meter display first —")
        print("unplug -> meter OFF -> plug USB while OFF -> wait for symbol.")
    h.close()


if __name__ == "__main__":
    main()
