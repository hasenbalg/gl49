"""LifeScan "MSC" USB backend (OneTouch Verio 2015 family).

When connected over micro-USB, meters such as the OneTouch Ultra Plus
Reflect (USB ID 2766:0010), Verio 2015 (2766:0000) and Select Plus
(2766:1000/1004) enumerate as USB mass storage. The tiny FAT volume
(label LIFESCAN, one startup.htm doorbell) is not the data path: three
512-byte sectors (LBA 3, 4, 5) act as command registers carrying
LifeScan's shared binary protocol (same CRC-16/CCITT-FALSE as the BLE
Verio protocol, different framing).

Protocol: https://protocols.glucometers.tech/lifescan/onetouch-verio-2015

We talk raw SCSI (INQUIRY / READ(10) / WRITE(10)) to the sg node via the
SG_IO ioctl — pure stdlib, no pyscsi/construct. SAFETY: writing to LBA
3-5 of an ordinary disk would destroy its partition table, so connect()
refuses any device whose SCSI INQUIRY vendor is not "LifeScan".

Packet on the wire (one 512-byte block, zero-padded)::

    02 <len> 00 <message...> 03 <crc16-le>
    len = len(message) + 6 ; CRC-16/CCITT-FALSE over 02..03 inclusive
    message = <prefix 03> <command bytes...> ; responses start 03 06

This transport is blocking by nature; the class below is a plain
(non-async) API.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import fcntl
import glob
import os
import struct
from typing import Optional

from ..models import Reading
from .verio import crc16_ccitt_false

# --------------------------------------------------------------------- SCSI
SG_IO = 0x2285
SG_DXFER_NONE = -1
SG_DXFER_TO_DEV = -2
SG_DXFER_FROM_DEV = -3
_TIMEOUT_MS = 5000


class UsbError(RuntimeError):
    pass


class _SgioHdr(ctypes.Structure):
    """struct sg_io_hdr from <scsi/sg.h> (Linux)."""
    _fields_ = [
        ("interface_id", ctypes.c_int),
        ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte),
        ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort),
        ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p),
        ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p),
        ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p),
        ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte),
        ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte),
        ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort),
        ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint),
        ("info", ctypes.c_uint),
    ]


# ----------------------------------------------------------------- protocol
EPOCH_2000 = 946_684_800  # device timestamps: seconds since 2000-01-01 UTC
LBA_CMD = 3               # command register (most commands)
LBA_PARAM = 4             # parameter register (READ PARAMETER)
BLOCK = 512

MEAL_BY_FLAG = {0x00: None, 0x01: "before", 0x02: "after"}


def build_packet(message: bytes) -> bytes:
    """Frame one LifeScan MSC message into a 512-byte SCSI WRITE(10) block."""
    if not 1 <= len(message) + 6 <= 255:
        raise UsbError(f"message length {len(message)} out of range")
    body = bytes([0x02, len(message) + 6, 0x00]) + message + b"\x03"
    pkt = body + struct.pack("<H", crc16_ccitt_false(body))
    return pkt.ljust(BLOCK, b"\x00")


def parse_packet(block: bytes) -> bytes:
    """Validate one response block; return the message body after 03 06."""
    if block[0] != 0x02:
        raise UsbError(f"bad STX: {block[:8].hex()}")
    length = block[1]
    if length < 7 or length > BLOCK:
        raise UsbError(f"bad length byte {length}")
    body = block[:length - 2]
    crc = struct.unpack_from("<H", block, length - 2)[0]
    if body[-1] != 0x03:
        raise UsbError(f"bad ETX: {block[:8].hex()}")
    if crc16_ccitt_false(body) != crc:
        raise UsbError(f"bad checksum (got {crc:04x})")
    msg = body[3:-1]
    if not msg.startswith(b"\x03\x06"):
        raise UsbError(f"error response: {msg.hex()}")
    return msg[2:]


def parse_record(payload: bytes) -> Reading:
    """READ RECORD response body -> Reading.

    Layout after the 03 06 prefix:
    inverse(2) pad(1) lifetime(2) timestamp(4) value(2) meal(1) pad(4)
    """
    inverse, _, lifetime, secs, value, meal = struct.unpack_from(
        "<HBHIHB", payload, 0)
    r = Reading(
        value=float(value), unit="mg/dL",
        timestamp=dt.datetime.fromtimestamp(
            secs + EPOCH_2000, dt.timezone.utc).replace(tzinfo=None),
        raw=bytes(payload[:14]),
    )
    r.meal = MEAL_BY_FLAG.get(meal)
    r.status.append(f"lifetime_counter_{lifetime}")
    return r


# ------------------------------------------------------------------ device
def detect_device() -> Optional[str]:
    """Find the /dev/sgN node of a LifeScan meter via sysfs."""
    for dev in sorted(glob.glob("/sys/class/scsi_generic/sg*/device")):
        try:
            with open(os.path.join(dev, "vendor")) as f:
                if f.read().strip() == "LifeScan":
                    return "/dev/" + os.path.basename(os.path.dirname(dev))
        except OSError:
            continue
    return None


class LifeScanUSBMeter:
    """Synchronous meter API over the USB SCSI register protocol.

    device: an sg node (/dev/sg4 — preferred, bypasses the block cache)
    or a block device (/dev/sde).
    """

    product: str = ""

    def __init__(self, device: str):
        self.device = device
        self._fd: Optional[int] = None

    # -- low level ---------------------------------------------------------
    def _sg(self, cmd: bytes, direction: int, data: bytes = b"") -> bytes:
        assert self._fd is not None, "not connected"
        cmdb = ctypes.create_string_buffer(bytes(cmd), len(cmd))
        buf = ctypes.create_string_buffer(max(len(data), 1))
        if data:
            buf.raw = data[:len(buf)]
        sb = ctypes.create_string_buffer(18)
        hdr = _SgioHdr()
        hdr.interface_id = ord("S")
        hdr.dxfer_direction = direction
        hdr.cmd_len = len(cmd)
        hdr.mx_sb_len = 18
        hdr.dxfer_len = len(data)
        hdr.dxferp = ctypes.addressof(buf)
        hdr.cmdp = ctypes.addressof(cmdb)
        hdr.sbp = ctypes.addressof(sb)
        hdr.timeout = _TIMEOUT_MS
        try:
            fcntl.ioctl(self._fd, SG_IO, hdr)
        except OSError as e:
            raise UsbError(f"SG_IO failed on {self.device}: {e}") from e
        if hdr.status or hdr.host_status or hdr.driver_status:
            raise UsbError(
                f"SCSI error status={hdr.status} host={hdr.host_status} "
                f"driver={hdr.driver_status} "
                f"sense={sb.raw[:hdr.sb_len_wr].hex()}")
        if direction == SG_DXFER_FROM_DEV:
            return buf.raw[:hdr.dxfer_len]
        return b""

    def _read10(self, lba: int) -> bytes:
        cdb = (bytes([0x28, 0x00]) + struct.pack(">I", lba) + b"\x00"
               + struct.pack(">H", 1) + b"\x00")
        return self._sg(cdb, SG_DXFER_FROM_DEV, bytes(BLOCK))

    def _write10(self, lba: int, data: bytes) -> None:
        cdb = (bytes([0x2A, 0x00]) + struct.pack(">I", lba) + b"\x00"
               + struct.pack(">H", 1) + b"\x00")
        self._sg(cdb, SG_DXFER_TO_DEV, data)

    def _command(self, message: bytes, lba: int = LBA_CMD) -> bytes:
        self._write10(lba, build_packet(message))
        return parse_packet(self._read10(lba))

    # -- lifecycle -----------------------------------------------------------
    def connect(self) -> None:
        self._fd = os.open(self.device, os.O_RDWR)
        try:
            inq = self._sg(bytes([0x12, 0, 0, 0, 36, 0]),
                           SG_DXFER_FROM_DEV, bytes(36))
        except UsbError:
            self.close()
            raise
        vendor = inq[8:16].split(b"\x00")[0].decode(errors="replace").strip()
        self.product = inq[16:32].split(b"\x00")[0].decode(
            errors="replace").strip()
        if vendor != "LifeScan":
            self.close()
            raise UsbError(
                f"{self.device} is not a LifeScan meter (vendor="
                f"{vendor!r}) — refusing to write command registers")

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "LifeScanUSBMeter":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- commands --------------------------------------------------------------
    _QUERY = {0x00: "serial", 0x01: "model", 0x02: "software"}

    def query(self, selector: int) -> str:
        payload = self._command(bytes([0x03, 0xE6, 0x02, selector]))
        return payload.decode("utf-16-le", errors="replace").strip("\x00")

    def device_info(self) -> dict:
        return {name: self.query(sel) for sel, name in self._QUERY.items()}

    def rtc(self) -> dt.datetime:
        payload = self._command(bytes([0x03, 0x20, 0x02]))
        secs = struct.unpack("<I", payload[:4])[0]
        return dt.datetime.fromtimestamp(secs + EPOCH_2000,
                                         dt.timezone.utc).replace(tzinfo=None)

    def record_count(self) -> int:
        payload = self._command(bytes([0x03, 0x27, 0x00]))
        return struct.unpack("<H", payload[:2])[0]

    def read_record(self, number: int) -> Reading:
        msg = bytes([0x03, 0x31, 0x02]) + struct.pack("<H", number) + b"\x00"
        return parse_record(self._command(msg))

    def download(self) -> list[Reading]:
        """All stored records, oldest first (record 0 is the newest)."""
        n = self.record_count()
        readings = [self.read_record(i) for i in range(n)]
        readings.reverse()
        for seq, r in enumerate(readings, 1):
            r.sequence = seq
        return readings
