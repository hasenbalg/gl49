"""Accu-Chek Guide over USB — IEEE 11073-20601 APDUs on bulk endpoints.

The Guide (USB id 173a:21d5, product 929; sibling ids 21d7/21d8 speak the
same protocol) enumerates as a vendor-specific bulk device: one interface,
two bulk endpoints, no CDC/MSC class.  The exchange is the Continua
Health Alliance "optimized exchange" (ISO/IEEE 11073): an association
handshake, then a PM-store segment transfer of 12-byte sample records.

Sequence (all big-endian; every host PDU is a "Presentation-APDU" 0xE700
wrapping a data ADPU, except association begin/release):

    bulk IN   <- pairing request (device speaks first!)
    bulk OUT  -> Association-Response 0xE300 (proto 20601, fixed payload)
    bulk IN   <- Config-Report (objects; PM-store handle = class 61)
    bulk OUT  -> event-report response 0x0201 (extended-config-start,
                 accepted)
    bulk OUT  -> Invoke-Get 0x0103 on handle 0 (MDS attributes)
    bulk IN   <- Response-Get 0x0203
    bulk OUT  -> action SEG_GET_INFO 0x0C0D on the PM-store handle
    bulk IN   <- confirmed-action response
    bulk OUT  -> action SEG_TRIG_XFER 0x0C1C, segment 0
    bulk IN   <- segment header (result 0 = transfer starting)
    loop      : bulk IN data segment; ACK each with 0x0201/0x0D21
                echoing the segment ids; last segment has status & 0x40
    bulk OUT  -> Association-Release 0xE400; bulk IN response

Sample records (12 bytes, offsets from record base):

    [0:6]   fixed / counter
    [6:12]  date-time, 6 BCD-ish bytes: CC YY MM DD hh mm where each byte's
            decimal value equals its hex digits read as decimal
            (e.g. 0x26 -> 26), century+year = 2000s
    [14:16] value, big-endian u16, in the meter's configured display unit
            (mg/dL on our units; mmol/L = value / 18.0)
    [16:18] status, big-endian u16; 0 = normal, anything else is an error
            slot (E-x codes; exact enum not published)

Time sync (action 0x0C17) exists but is deliberately NOT implemented:
never write to a medical device we have not had to.

Needs root (or a udev rule) to claim the device, like the GL49 USB path.
"""
from __future__ import annotations

import logging
import struct
import time
from datetime import datetime
from typing import Optional

from ..records import Reading

log = logging.getLogger("glucobridge.guide_usb")

GUIDE_IDS = [(0x173A, 0x21D5), (0x173A, 0x21D7), (0x173A, 0x21D8)]

# 11073 APDU / action / event constants
APDU_ASSOC_RESPONSE = 0xE300
APDU_ASSOC_RELEASE = 0xE400
APDU_ASSOC_ABORT = 0xE600
APDU_PRESENTATION = 0xE700
ADPU_INVOKE_GET = 0x0103
ADPU_INVOKE_CONFIRMED_ACTION = 0x0107
ADPU_RESP_CONFIRMED_EVENT_REPORT = 0x0201
ADPU_RESP_GET = 0x0203
NOTI_CONFIG = 0x0D1C
NOTI_SEGMENT_DATA = 0x0D21
ACT_SEG_GET_INFO = 0x0C0D
ACT_SEG_TRIG_XFER = 0x0C1C
MDC_PMSTORE = 61
MDC_ATTR_NUM_SEG = 2385

TIMEOUT_IN = 20_000     # ms; segments can take a beat
TIMEOUT_OUT = 5_000


class UsbError(RuntimeError):
    pass


def _bcd(x: int) -> int:
    """Guide stores decimal digits in hex nibbles: 0x26 -> 26."""
    return (x >> 4) * 10 + (x & 0xF)


class _Writer:
    """Big-endian PDU builder, mirroring the be16/be32 cursor in the spec."""

    def __init__(self) -> None:
        self.b = bytearray()

    def u16(self, v: int) -> "_Writer":
        self.b += struct.pack(">H", v)
        return self

    def u32(self, v: int) -> "_Writer":
        self.b += struct.pack(">I", v)
        return self


class _Reader:
    def __init__(self, buf: bytes, offset: int = 0) -> None:
        self.buf = buf
        self.o = offset

    def u16(self) -> int:
        v = struct.unpack_from(">H", self.buf, self.o)[0]
        self.o += 2
        return v

    def u32(self) -> int:
        v = struct.unpack_from(">I", self.buf, self.o)[0]
        self.o += 4
        return v


def parse_sample(rec: bytes, device: str) -> Reading:
    """Decode one segment entry; rec starts AT the 6 BCD date-time bytes
    (entry stride is 12; value at +8, status at +10)."""
    cc = _bcd(rec[0])
    yy = _bcd(rec[1])
    mm = _bcd(rec[2])
    dd = _bcd(rec[3])
    hh = _bcd(rec[4])
    mi = _bcd(rec[5])
    value = struct.unpack_from(">H", rec, 8)[0]
    status = struct.unpack_from(">H", rec, 10)[0]
    ts = None
    try:
        ts = datetime(cc * 100 + yy, mm, dd, hh, mi)
    except ValueError:
        pass
    return Reading(
        value=float(value),
        unit="mg/dL",
        timestamp=ts,
        error=None if status == 0 else f"status_0x{status:04x}",
        device=device,
        raw=rec[:12].hex(),
    )


def detect_device() -> Optional[tuple]:
    import usb.core
    for vid, pid in GUIDE_IDS:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is not None:
            return (vid, pid, dev)
    return None


class GuideUsbMeter:
    """One USB session with an Accu-Chek Guide."""

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self._dev = None
        self._ep_in = None
        self._ep_out = None
        self._iface = 0
        self._alt = 0
        self.id = "usb guide"

    # ------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        import usb.core
        import usb.util
        found = detect_device()
        if not found:
            raise UsbError(
                "no ACCU-CHEK Guide on USB (need %s) — plug in and put the "
                "meter in its 'data transfer' screen"
                % ", ".join(f"{v:04x}:{p:04x}" for v, p in GUIDE_IDS))
        vid, pid, dev = found
        self.id = f"{vid:04x}:{pid:04x}"
        try:
            dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        dev.set_configuration()
        cfg = dev.get_active_configuration()
        # pyusb semantics: iterating a Configuration yields one Interface
        # object per (interface, alt-setting); iterating an Interface
        # yields its Endpoint objects directly.
        for intf in cfg:
            eps = list(intf)
            ep_in = next((e.bEndpointAddress for e in eps
                          if e.bEndpointAddress & 0x80), None)
            ep_out = next((e.bEndpointAddress for e in eps
                           if not e.bEndpointAddress & 0x80), None)
            if ep_in and ep_out:
                self._iface = intf.bInterfaceNumber
                self._alt = intf.bAlternateSetting
                self._ep_in = ep_in
                self._ep_out = ep_out
                usb.util.claim_interface(dev, self._iface)
                try:
                    usb.util.set_interface_alt_setting(
                        dev, self._iface, self._alt)
                except usb.core.USBError:
                    pass
                break
        else:
            raise UsbError("no bulk IN+OUT endpoint pair found")
        self._dev = dev
        # standard GET_STATUS nudges the peripheral into the exchange
        dev.ctrl_transfer(0x80, 0x00, 0, 0, 2, 5000)

    def close(self) -> None:
        if self._dev is not None:
            import usb.util
            try:
                usb.util.release_interface(self._dev, self._iface)
            except Exception:
                pass
            self._dev = None

    def __enter__(self) -> "GuideUsbMeter":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------- transport
    def _out(self, pdu: bytes, name: str) -> None:
        log.debug("-> %-28s %s", name, pdu.hex())
        n = self._dev.write(self._ep_out, bytes(pdu), TIMEOUT_OUT)
        if n != len(pdu):
            raise UsbError(f"short write on {name}: {n}/{len(pdu)}")

    def _in(self, name: str, size: int = 1024) -> bytes:
        buf = self._dev.read(self._ep_in, size, int(self.timeout * 1000))
        data = bytes(buf)
        log.debug("<- %-28s %s", name, data.hex())
        return data

    @staticmethod
    def _invoke_id(buf: bytes) -> int:
        return struct.unpack_from(">H", buf, 6)[0]

    # -------------------------------------------------------------- session
    def _assoc(self) -> int:
        # the device sends its pairing request first
        self._in("pairing request", 64)
        w = _Writer()
        w.u16(APDU_ASSOC_RESPONSE).u16(44).u16(0x0003)
        w.u16(20601).u16(38)
        w.u32(0x80000002).u16(0x8000).u32(0).u32(0)
        w.u32(0x80000000).u16(8)
        w.u32(0x12345678).u32(0).u32(0).u32(0).u16(0)
        self._out(bytes(w.b), "association response")
        cfgbuf = self._in("config report")
        return self._invoke_id(cfgbuf), cfgbuf

    def _pmstore_handle(self, cfgbuf: bytes) -> int:
        r = _Reader(cfgbuf, 24)
        count = r.u16()
        for _ in range(count):
            obj_class = r.u16()
            handle = r.u16()
            r.u16()          # attribute count
            size = r.u16()
            if obj_class == MDC_PMSTORE:
                return handle
            r.o += size
        raise UsbError("no PM segment store in config report")

    def download(self) -> list[Reading]:
        invoke_id, cfgbuf = self._assoc()
        pm_handle = self._pmstore_handle(cfgbuf)

        w = _Writer().u16(APDU_PRESENTATION).u16(22).u16(20)
        w.u16(invoke_id).u16(ADPU_RESP_CONFIRMED_EVENT_REPORT).u16(14)
        w.u16(0).u32(0).u16(NOTI_CONFIG).u16(4).u16(0x4000).u16(0)
        self._out(bytes(w.b), "config accepted")

        w = _Writer().u16(APDU_PRESENTATION).u16(14).u16(12)
        w.u16(invoke_id + 1).u16(ADPU_INVOKE_GET).u16(6).u16(0).u32(0)
        self._out(bytes(w.b), "MDS attribute get")
        buf = self._in("MDS attribute resp")
        if struct.unpack_from(">H", buf, 0)[0] == APDU_ASSOC_ABORT:
            raise UsbError("device aborted the association")
        invoke_id = self._invoke_id(buf)

        w = _Writer().u16(APDU_PRESENTATION).u16(20).u16(18)
        w.u16(invoke_id + 1).u16(ADPU_INVOKE_CONFIRMED_ACTION).u16(12)
        w.u16(pm_handle).u16(ACT_SEG_GET_INFO).u16(6).u16(1).u16(2).u16(0)
        self._out(bytes(w.b), "segment get info")
        buf = self._in("segment get info resp")
        invoke_id = self._invoke_id(buf)

        w = _Writer().u16(APDU_PRESENTATION).u16(16).u16(14)
        w.u16(invoke_id + 1).u16(ADPU_INVOKE_CONFIRMED_ACTION).u16(8)
        w.u16(pm_handle).u16(ACT_SEG_TRIG_XFER).u16(2).u16(0)
        self._out(bytes(w.b), "segment trig transfer")
        buf = self._in("segment header resp")
        if len(buf) >= 22 and struct.unpack_from(">H", buf, 14)[0] != \
                ACT_SEG_TRIG_XFER:
            raise UsbError("unexpected segment header")
        if len(buf) == 22 and struct.unpack_from(">H", buf, 20)[0] != 0:
            code = struct.unpack_from(">H", buf, 20)[0]
            raise UsbError("meter refused transfer"
                           + (" (memory empty)" if code == 3 else
                              f", result code {code}"))
        invoke_id = self._invoke_id(buf)

        readings: list[Reading] = []
        last = False
        while not last:
            buf = self._in("data segment")
            if len(buf) < 40:
                raise UsbError(f"short segment ({len(buf)} bytes)")
            status = buf[32]
            r = _Reader(buf, 22)
            u0, u1, u2 = r.u32(), r.u32(), r.u16()   # u2 == entry count
            n_entries = struct.unpack_from(">H", buf, 30)[0]
            for i in range(n_entries):
                base = 36 + i * 12
                rec = buf[base:base + 12]
                if len(rec) < 12:
                    break
                rd = parse_sample(bytes(rec), self.id)
                readings.append(rd)
            last = bool(status & 0x40)

            w = _Writer().u16(APDU_PRESENTATION).u16(30).u16(28)
            w.u16(invoke_id).u16(ADPU_RESP_CONFIRMED_EVENT_REPORT).u16(22)
            w.u16(pm_handle).u32(0xFFFFFFFF).u16(NOTI_SEGMENT_DATA).u16(12)
            w.u32(u0).u32(u1).u16(u2).u16(0x0080)
            self._out(bytes(w.b), "segment ack")
            invoke_id = self._invoke_id(buf)

        w = _Writer().u16(APDU_ASSOC_RELEASE).u16(2).u16(0)
        self._out(bytes(w.b), "association release")
        try:
            self._in("release response", 64)
        except Exception:
            pass
        readings.sort(key=lambda r: (r.timestamp is None, r.timestamp or 0))
        return readings

    # ------------------------------------------------- misc (API symmetry)
    def device_info(self) -> dict:
        return {"model": "Accu-Chek Guide", "usb_id": self.id}

    def gatt_dump(self) -> str:   # pragma: no cover - not a BLE device
        return "(USB device — no GATT)"
