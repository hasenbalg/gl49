"""Beurer GL 49 USB backend (Silicon Labs CP2110 HID-UART bridge).

The GL 49 is a rebranded FORA TD4149 (device-ID reply ``49 41 02 00``
= "IA" rev B), and over USB it speaks FORA's binary frame protocol at
19200 baud 8N1 through an on-board CP2110 USB-to-UART bridge
(USB ID 10c4:ea80, exposed as a raw HID node — no COM port).

Transfer mode: connect USB while the meter is switched OFF; the LCD
shows a USB symbol. The meter leaves PC-transfer mode shortly after
plugging in, so open the session with :meth:`Gl49UsbMeter.wait_for_session`
(replug while it runs) and download promptly.

Framing (8 bytes both directions)::

    51 CMD p2 p3 00 00 A3 CK        request   CK = sum(first 7) & 0xFF
    51 CMD d...              A5 CK  response  CK = sum(all preceding) & 0xFF

Commands: ``0x24`` device ID, ``0x29`` device info, ``0x2B`` record
count (u16 LE at offset 2), ``0x85 idxL idxH`` record fetch — idx N
returns records 2N and 2N+1 as twelve data bytes (zero-padded past the
count).

WAKE-RETRY: the meter MCU ignores the first frame it receives after
power-up; every command must be re-sent (~250 ms apart) until a valid
reply arrives. One-shot probes see total silence.

Record format (6 wire bytes): the PC first byte-swaps every 16-bit
word, then the fields are read off the swapped bytes ``r0..r5``::

    value  = r5 | ((r4 & 3) << 8)      mg/dL
    year   = 2000 + (r0 >> 1)
    month  = (r1 >> 5) | ((r0 & 1) << 3)   (1 = January)
    day    = r1 & 0x1F
    hour   = r2 & 0x1F
    minute = r3 & 0x3F

There is no temperature field. The bits the vendor app ignores
(``r4>>2``, ``r2>>5``, ``r3>>6``) likely hold the meal marks but are
not decoded.

CP2110 notes (AN434): the UART-config feature report (0x50) carries
the baud rate BIG-ENDIAN (a silent 9600 fallback otherwise); the
OUTPUT report's byte 0 is the payload length; GET on report 0x01
stalls (it is the UART data report).

This transport is blocking; the class below is a plain (non-async)
API. Requires ``hidapi`` and permission for the hidraw node (root or
a udev rule).
"""
from __future__ import annotations

import datetime as dt
import struct
import time
from typing import Optional

from ..models import Reading

VID, PID = 0x10C4, 0xEA80          # Silicon Labs CP2110
BAUD = 19200

MODEL_BY_CODE = {0x4149: "FORA TD4149 (Beurer GL 49)"}


class UsbError(RuntimeError):
    pass


class SessionLost(UsbError):
    """USB device vanished mid-session (unplug / meter left transfer mode)."""


# ----------------------------------------------------------------- protocol
def frame(cmd: int, p2: int = 0, p3: int = 0) -> bytes:
    """Build one 8-byte request frame with trailing checksum."""
    body = bytes([0x51, cmd, p2, p3, 0, 0, 0xA3])
    return body + bytes([sum(body) & 0xFF])


def swap_pairs(b: bytes) -> bytes:
    """Byte-swap every 16-bit word (what the vendor PC does before use)."""
    return bytes(x for i in range(0, len(b) - 1, 2) for x in (b[i + 1], b[i]))


def reply_valid(r: bytes, expect_cmd: int, min_len: int = 8) -> bool:
    """True if r is a checksummed reply to expect_cmd."""
    return (len(r) >= min_len and r[0] == 0x51 and r[1] == expect_cmd
            and sum(r[:-1]) & 0xFF == r[-1])


def parse_count(r: bytes) -> int:
    """0x2B reply -> stored record count."""
    return r[2] | r[3] << 8


def parse_device_id(r: bytes) -> dict:
    """0x24 reply -> {'code': int, 'model': str|None, 'raw': hex}."""
    code = r[2] | r[3] << 8
    return {"code": f"0x{code:04x}",
            "model": MODEL_BY_CODE.get(code),
            "raw": r[2:6].hex()}


def parse_record(rec: bytes) -> Optional[Reading]:
    """One 6-byte wire record -> Reading, or None if blank/invalid."""
    if len(rec) != 6 or rec == b"\x00" * 6:
        return None
    r0, r1, r2, r3, r4, r5 = swap_pairs(rec)
    value = r5 | ((r4 & 3) << 8)
    year = 2000 + (r0 >> 1)
    month = (r1 >> 5) | ((r0 & 1) << 3)
    day = r1 & 0x1F
    hour = r2 & 0x1F
    minute = r3 & 0x3F
    if not (day and 1 <= month <= 12 and hour < 24 and minute < 60
            and value):
        return None
    try:
        ts = dt.datetime(year, month, day, hour, minute)
    except ValueError:
        return None
    return Reading(value=float(value), unit="mg/dL", timestamp=ts,
                   raw=bytes(rec))


# ------------------------------------------------------------------ device
def detect_device() -> Optional[bytes]:
    """hidraw path of a connected CP2110 (GL49 in transfer mode), or None."""
    import hid
    devs = hid.enumerate(VID, PID)
    return devs[0]["path"] if devs else None


class Gl49UsbMeter:
    """Synchronous meter API over the CP2110 HID-UART bridge.

    path: hidraw device path (default: auto-detect the first CP2110).
    """

    product: str = "Beurer GL 49 (USB/CP2110)"

    def __init__(self, path: Optional[bytes] = None):
        self._path = path
        self.h = None

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        import hid
        if self._path is None:
            devs = hid.enumerate(VID, PID)
            if not devs:
                raise UsbError(
                    f"no CP2110 {VID:04x}:{PID:04x} found — meter OFF, "
                    "plug USB, wait for the USB symbol")
            self._path = devs[0]["path"]
        self.h = hid.device()
        try:
            self.h.open_path(self._path)
        except OSError as e:
            raise UsbError(f"cannot open CP2110: {e} (needs root?)") from e
        self.h.set_nonblocking(0)
        # AN434: report 0x50 = UART config, baud BIG-ENDIAN; then enable
        # the UART (0x41) and purge both FIFOs (0x43).
        self.h.send_feature_report(
            bytes([0x50]) + struct.pack(">IBBBB", BAUD, 0, 0, 3, 0))
        self.h.send_feature_report(bytes([0x41, 0x01]))
        self.h.send_feature_report(bytes([0x43, 0x03]))

    def close(self) -> None:
        if self.h is not None:
            try:
                self.h.close()
            except Exception:
                pass
            self.h = None

    def __enter__(self) -> "Gl49UsbMeter":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport -----------------------------------------------------------
    def _drain(self, quiet: float = 0.25, limit: int = 64) -> bytes:
        """Collect UART bytes until the line goes quiet."""
        buf = b""
        t0 = time.time()
        while time.time() - t0 < quiet and len(buf) < limit:
            try:
                r = self.h.read(64, timeout_ms=50)
            except OSError:
                raise SessionLost("device unplugged / meter left "
                                  "transfer mode") from None
            if r:
                r = bytes(r)
                n = r[0]                       # AN434: byte 0 = length
                buf += r[1:1 + n] if n and len(r) > 1 else r
                t0 = time.time()
        return buf

    def cmd(self, fr: bytes, expect_cmd: int, min_len: int = 8,
            retries: int = 40) -> Optional[bytes]:
        """Send a frame, retrying every 250 ms until a valid reply.

        The first frame only wakes the meter MCU; replies arrive on
        later retries (wake-retry is mandatory).
        """
        for _ in range(retries):
            self.h.write(bytes([len(fr)]) + fr)   # AN434: byte 0 = length
            r = self._drain()
            if reply_valid(r, expect_cmd, min_len):
                return r
        return None

    def wait_for_session(self, timeout: float = 90.0) -> bool:
        """Poll 0x2B until the meter answers. Plug the meter in
        (OFF -> cable -> USB symbol) while this runs."""
        fr = frame(0x2B)
        t_end = time.time() + timeout
        while time.time() < t_end:
            self.h.write(bytes([len(fr)]) + fr)
            r = self._drain(quiet=0.2)
            if reply_valid(r, 0x2B):
                return True
        return False

    # -- commands --------------------------------------------------------------
    def device_info(self) -> dict:
        r = self.cmd(frame(0x24), expect_cmd=0x24)
        if not r:
            raise UsbError("no device-ID reply")
        return parse_device_id(r)

    def record_count(self) -> int:
        r = self.cmd(frame(0x2B), expect_cmd=0x2B)
        if not r:
            raise UsbError("no record-count reply")
        return parse_count(r)

    def download(self) -> list[Reading]:
        """All stored records, oldest first. Call wait_for_session()
        first if the session may be stale."""
        total = self.record_count()
        readings: list[Reading] = []
        for idx in range((total + 1) // 2):
            r = self.cmd(frame(0x85, idx & 0xFF, (idx >> 8) & 0xFF),
                         expect_cmd=0x85, min_len=16)
            if not r:
                raise UsbError(f"no reply for record pair {idx}")
            data = r[2:14]
            for half in range(2):
                rec = parse_record(data[half * 6:half * 6 + 6])
                if rec:
                    readings.append(rec)
        readings.reverse()
        for seq, r in enumerate(readings, 1):
            r.sequence = seq
        return readings
