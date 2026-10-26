"""TaiDoc TD-42xx meters over USB — GlucoCheck Advance (TD-4288) family.

Same physical layer as the GL49 backend: an on-board CP2110 HID-UART
bridge (10c4:ea80) at 19200 8N1, frames over AN434 HID reports.  The
firmware dialect is the TaiDoc TD-42xx family (also sold as GlucoRx
Nexus, GlucoMen Nexus, GlucoCheck XL): 6-byte frames
``51 CMD m1 m2 m3 m4 A3`` + checksum (sum of the first seven bytes),
opened with a 0x22 connect (valid replies: cmd 0x22/0x24/0x54).

Commands used (all read-only; 0x33 set-datetime and 0x52 clear-memory
exist but are deliberately not implemented on a medical device):

    0x22 connect            message echoed, cmd confirms
    0x24 model              u16le raw BCD-ish -> "TD-<hex>" (0x4288 = TD-4288)
    0x2B count              u16le count (+4 unknown bytes)
    0x25 select+datetime    req: u16le record id; msg: day u16le + min + hour
    0x26 select+value       req: u16le record id; msg: value u16le mg/dL,
                            unknown byte, meal flag

`day` is a bitfield (of the u16 *before* LE load order is applied):
``year:7 (offset 2000) | month:4 | day:5``.  Meal flags: 0x40 before
meal, 0x80 after meal.  Record id 0 is the NEWEST reading.

Live-verified 2026-10-26 on a TESTAmed GlucoCheck Advance: values and
timestamps match its BLE (standard GSS) history exactly; the unit test
fixtures are that capture.
"""
from __future__ import annotations

import struct
from datetime import datetime
from typing import Optional

from ..records import Reading
from .gl49_usb import Gl49UsbMeter, UsbError

CONNECT_VALID_CMDS = (0x22, 0x24, 0x54)
MEAL_BY_FLAG = {0x40: "preprandial", 0x80: "postprandial"}


def frame4(cmd: int, msg: bytes = b"\x00\x00\x00\x00") -> bytes:
    f = bytes([0x51, cmd]) + msg[:4] + bytes([0xA3])
    return f + bytes([sum(f) & 0xFF])


def parse_model(msg: bytes) -> str:
    raw = struct.unpack("<H", msg[:2])[0]
    return f"TD-{raw:04X}"


def parse_datetime(msg: bytes) -> Optional[datetime]:
    day_word = struct.unpack("<H", msg[:2])[0]
    minute = msg[2]
    hour = msg[3]
    year = 2000 + ((day_word >> 9) & 0x7F)
    month = (day_word >> 5) & 0x0F
    day = day_word & 0x1F
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def parse_value(msg: bytes) -> tuple[float, Optional[str]]:
    value = struct.unpack("<H", msg[:2])[0]
    meal = MEAL_BY_FLAG.get(msg[3])
    return float(value), meal


def parse_count(msg: bytes) -> int:
    return struct.unpack("<H", msg[:2])[0]


class Td42xxUsbMeter(Gl49UsbMeter):
    """GlucoCheck Advance et al. over the CP2110; read-only."""

    product = "TaiDoc TD-42xx (USB/CP2110) — e.g. GlucoCheck Advance"

    # -- session ----------------------------------------------------------
    def connect(self) -> None:
        super().connect()
        self._open_connect()

    def _open_connect(self) -> None:
        """0x22 connect with wake-retry (first frames only wake the MCU)."""
        fr = frame4(0x22)
        for _ in range(40):
            self.h.write(bytes([len(fr)]) + fr)
            r = self._drain(quiet=0.15)
            if r and len(r) >= 8 and r[1] in CONNECT_VALID_CMDS:
                return
        raise UsbError("meter did not answer 0x22 connect — "
                       "replug the cable and retry")

    def wait_for_session(self, timeout: float = 90.0) -> bool:
        import time as _t
        fr = frame4(0x22)
        t_end = _t.time() + timeout
        while _t.time() < t_end:
            self.h.write(bytes([len(fr)]) + fr)
            r = self._drain(quiet=0.15)
            if r and len(r) >= 8 and r[1] in CONNECT_VALID_CMDS:
                return True
        return False

    # -- data ---------------------------------------------------------------
    def device_info(self) -> dict:
        r = self.cmd(frame4(0x24), expect_cmd=0x24)
        if not r:
            raise UsbError("no model reply")
        info = {"model": parse_model(r[2:6])}
        d = self.cmd(frame4(0x23), expect_cmd=0x23)
        if d:
            ts = parse_datetime(d[2:6])
            if ts:
                info["meter_clock"] = ts.isoformat(timespec="minutes")
        return info

    def record_count(self) -> int:
        r = self.cmd(frame4(0x2B), expect_cmd=0x2B)
        if not r:
            raise UsbError("no record-count reply")
        return parse_count(r[2:6])

    def read_record(self, idx: int) -> Optional[Reading]:
        sel = struct.pack("<H", idx) + b"\x00\x00"
        rd = self.cmd(frame4(0x25, sel), expect_cmd=0x25)
        if not rd:
            return None
        ts = parse_datetime(rd[2:6])
        rv = self.cmd(frame4(0x26, sel), expect_cmd=0x26)
        if not rv:
            return None
        value, meal = parse_value(rv[2:6])
        return Reading(value=value, unit="mg/dL", timestamp=ts, meal=meal,
                       device="glucocheck-usb",
                       raw=(rd[2:6] + rv[2:6]).hex())

    def download(self) -> list[Reading]:
        """All stored records, oldest first. Call wait_for_session()
        first if the session may be stale."""
        total = self.record_count()
        readings = [r for i in range(total)
                    if (r := self.read_record(i)) is not None]
        readings.sort(key=lambda r: (r.timestamp is None, r.timestamp or 0))
        return readings
