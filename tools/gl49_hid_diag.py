#!/usr/bin/env python3
"""GL49 raw-HID diagnostic per AN434 (CP2110 report IDs 0x40-0x66).

v3 — correct wire encoding (two bugs fixed vs v2):
  * Baud rate in report 0x50 is BIG-ENDIAN (explicit spec exception).
  * Stop bits: 0x00 = short (1 bit time); 0x01 = long (2 bit times).

For each baud rate: set config (verified by readback), purge FIFOs,
idle-listen (the meter may broadcast a preamble), then send a 0x2B
record-count frame and listen. INPUT reports print raw AND decoded
(byte 0 of each input report is the payload length).

Keep the meter showing the USB symbol while this runs.
"""

import struct
import sys
import time

import hid

VID, PID = 0x10C4, 0xEA80
FRAME = bytes([0x51, 0x2B, 0, 0, 0, 0, 0xA3, 0x1F])  # record count
BAUDS = [19200, 9600, 115200, 38400, 57600, 4800, 2400]


def get(h, rid, n=64):
    try:
        return bytes(h.get_feature_report(rid, n))
    except Exception:
        return None


def drain(h, seconds):
    """Collect INPUT reports for `seconds`; return list of payload bytes."""
    out = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        r = h.read(64, timeout_ms=50)
        if r:
            r = bytes(r)
            n = r[0]  # AN434: first byte = payload length
            out.append(r[1:1 + n] if n and len(r) > 1 else r)
    return out


def poll_mode(h):
    """Mimic the vendor app's retry loop: hammer 0x2B at 19200.

    The app re-sends the connect frame every ~50 ms while the UI
    spinner runs; a sleeping MCU UART may answer only the frame AFTER
    it wakes. Also try wake preambles (line break, 0x00s, 0x55s).
    Expect an 18-byte reply: 51 2B <cnt lo> <cnt hi> ... A3? CK.
    """
    cfg = bytes([0x50]) + struct.pack(">IBBBB", 19200, 0, 0, 3, 0)
    h.send_feature_report(cfg)
    h.send_feature_report(bytes([0x41, 0x01]))
    h.send_feature_report(bytes([0x43, 0x03]))
    print("polling 0x2B at 19200 for 30 s (replug meter now: OFF -> cable "
          "while OFF -> wait for USB symbol) ...")
    t_end = time.time() + 30
    i = 0
    while time.time() < t_end:
        i += 1
        if i % 12 == 5:      # every ~4 s: line-break wake, then poll
            h.send_feature_report(bytes([0x51, 0x01]))   # transmit break
            time.sleep(0.1)
            h.send_feature_report(bytes([0x52, 0x01]))   # stop break
        if i % 12 == 11:     # byte preambles
            h.write(bytes([4]) + b"\x00\x00\x00\x00")
        h.write(bytes([len(FRAME)]) + FRAME)
        r = drain(h, 0.25)
        for p in r:
            print(f"  <<< {p.hex(' ')}")
    st = get(h, 0x42, 7)
    if st and len(st) >= 5:
        print(f"final: TX fifo {st[1] | st[2] << 8}, RX fifo {st[3] | st[4] << 8}")


def main():
    devs = hid.enumerate(VID, PID)
    if not devs:
        sys.exit(f"no CP2110 {VID:04x}:{PID:04x} found (replug; meter OFF first)")
    d = devs[0]
    print(f"opening {d['path'].decode()}")
    h = hid.device()
    h.open_path(d["path"])
    h.set_nonblocking(0)

    v = get(h, 0x46, 3)
    print(f"version: {v.hex(' ') if v else 'FAILED'} (want part 0x0A)\n")
    h.send_feature_report(bytes([0x41, 0x01]))  # UART enable once

    if "--poll" in sys.argv:
        poll_mode(h)
        h.close()
        return

    for baud in BAUDS:
        # AN434 6.3: baud BIG-endian, parity, flow, data bits, stop bits
        cfg = bytes([0x50]) + struct.pack(">IBBBB", baud, 0x00, 0x00, 0x03, 0x00)
        h.send_feature_report(cfg)
        rb = get(h, 0x50, 9)
        ok = rb and len(rb) >= 5 and struct.unpack(">I", rb[1:5])[0] == baud
        print(f"=== {baud} baud === config readback: "
              f"{rb.hex(' ') if rb else 'FAILED'} {'OK' if ok else '<- NOT APPLIED'}")
        if not ok:
            continue
        h.send_feature_report(bytes([0x43, 0x03]))  # purge both FIFOs

        idle = drain(h, 1.0)
        print(f"  idle 1 s   : {[' '.join(map('{:02x}'.format, p)) for p in idle] or '(nothing)'}")

        h.write(bytes([len(FRAME)]) + FRAME)
        replies = drain(h, 1.2)
        st = get(h, 0x42, 7)
        fifo = ""
        if st and len(st) >= 5:
            fifo = f"  [TX fifo {st[1] | st[2] << 8}, RX fifo {st[3] | st[4] << 8}]"
        print(f"  2B -> {FRAME.hex(' ')}")
        if replies:
            for p in replies:
                print(f"  <<< {p.hex(' ')}")
        else:
            print(f"  resp: (silent){fifo}")
    h.close()
    print("\ndone. '<<<' lines are the meter talking.")


if __name__ == "__main__":
    main()
