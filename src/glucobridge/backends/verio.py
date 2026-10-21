"""Backend for OneTouch meters using LifeScan's proprietary "Verio" protocol.

Known-good models: Verio Flex, Verio Reflect, Ultra Plus Reflect,
Ultra Plus Flex, OneTouch Select Plus Flex (all advertise the AF9DF7A1
GATT service).

Protocol sources (cross-validated):
  * xDrip's VerioHelper (byte-exact reference implementation)
  * Glucera release notes: Reflect records carry non-zero "Blood Sugar
    Mentor" metadata in result bytes 6-10 (Verio Flex: zero), and 4-byte
    responses must be interpreted by pending command kind, not magnitude.

Framing:
  request  ``01 02 <size> 00 03 <payload...> 03 <crc16 lo> <hi>``
           size = len(payload) + 7 (covers everything after 0x01)
  response ``01 02 <len-1> 00 03 <type> <data...> 03 <crc lo> <hi>``
  CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over the bytes from 0x02
  through the trailing 0x03, output little-endian.
  ACKs are a single 0x81 byte in both directions.

Commands (payload): time ``20 02``, highest record number ``27 00``,
stored record count ``0A 02 06``, record N ``B3 <lo> <hi>``.
Data responses (type 0x06) carry: 4 bytes = meter time (unix = LE32 +
946684799) or record counter (per pending command); 2 bytes = record
count (int16 LE); 11 bytes = record (LE32 seconds offset, int16 mg/dL,
5 metadata bytes).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import struct
from typing import Optional

from bleak import BleakClient

from ..models import Reading

log = logging.getLogger("glucobridge.verio")

VERIO_SERVICE = "af9df7a1-e595-11e3-96b4-0002a5d5c51b"
VERIO_WRITE = "af9df7a2-e595-11e3-96b4-0002a5d5c51b"
VERIO_NOTIFY = "af9df7a3-e595-11e3-96b4-0002a5d5c51b"

ACK = 0x81
DELIM = 0x03
TIME_OFFSET = 946684799  # meter epoch: value + TIME_OFFSET = unix seconds

RSP_DATA = 0x06
RSP_ERRORS = {0x07: "command_not_allowed", 0x08: "command_not_supported",
              0x09: "command_not_understood"}

# pending-command kinds (4-byte results are ambiguous otherwise)
_KIND_TIME = "time"
_KIND_Rcounter = "rcounter"
_KIND_Tcounter = "tcounter"
_KIND_RECORD = "record"


class VerioError(RuntimeError):
    pass


# ------------------------------------------------------------------- CRC16
def crc16_ccitt_false(data: bytes) -> int:
    """Standard CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflect."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# ------------------------------------------------------------------ framing
def build_request(payload: bytes) -> bytes:
    size = len(payload) + 7
    body = bytes([0x02, size, 0x00, DELIM]) + bytes(payload) + bytes([DELIM])
    crc = crc16_ccitt_false(body)
    return bytes([0x01]) + body + bytes([crc & 0xFF, crc >> 8])


def parse_response(msg: bytes) -> tuple[str, bytes]:
    """Return (kind, data). kind in {'ack','data','error'}; raises on bad CRC.

    'data' payloads are the raw result bytes (4/2/11 typical).
    """
    msg = bytes(msg)
    if len(msg) == 1 and msg[0] & 0x81 == 0x81:
        return ("ack", b"")
    if len(msg) < 9 or msg[0] != 0x01 or msg[1] != 0x02:
        raise VerioError(f"invalid protocol header: {msg.hex()}")
    if msg[2] != len(msg) - 1:
        raise VerioError(f"length field mismatch ({msg[2]} vs {len(msg)-1}): {msg.hex()}")
    crc = crc16_ccitt_false(msg[1:-2])
    if bytes([crc & 0xFF, crc >> 8]) != msg[-2:]:
        raise VerioError(f"checksum failure: {msg.hex()}")
    rsp_type = msg[5]
    if rsp_type == RSP_DATA:
        return ("data", msg[6:-3])
    if rsp_type in RSP_ERRORS:
        return ("error", RSP_ERRORS[rsp_type].encode())
    raise VerioError(f"unknown response type 0x{rsp_type:02X}: {msg.hex()}")


def decode_record(data: bytes) -> Reading:
    """11-byte record result -> Reading (Reflect metadata kept, not zero-checked)."""
    if len(data) != 11:
        raise VerioError(f"record must be 11 bytes, got {len(data)}")
    secs, mgdl = struct.unpack_from("<Ih", data, 0)
    metadata = data[6:11]
    r = Reading(
        value=float(mgdl), unit="mg/dL",
        timestamp=dt.datetime.fromtimestamp(
            secs + TIME_OFFSET, dt.timezone.utc).replace(tzinfo=None),
        raw=bytes(data),
    )
    # Reflect metadata (live capture, Ultra Plus Reflect fw SI_04.01.03):
    #   valid reading : 00 01 00 00 00   (byte 7 = 0x01 "valid" flag)
    #   error slot    : 00 00 00 02 0N   (bytes 9-10: class + Er code,
    #                                     observed 02 03 == meter "E-03")
    # Meal/mentor tags have not been observed yet; keep metadata raw.
    if any(metadata):
        r.status.append(f"mentor_metadata_{metadata.hex()}")
    if mgdl == 0 and (metadata[3] or metadata[4]):
        r.value = None
        r.error = f"E-{metadata[4]}" if metadata[4] else \
            f"meter_error_{metadata.hex()}"
    return r


# ------------------------------------------------------------------ client
class VerioMeter:
    """One connection to an OneTouch AF9DF7A1-protocol meter."""

    implemented = True

    def __init__(self, target, name: Optional[str] = None,
                 timeout: float = 10.0, drift_correction: bool = True):
        self.target = target
        self.name = name or (target if isinstance(target, str) else
                             getattr(target, "name", None))
        self.timeout = timeout
        self.drift_correction = drift_correction
        self._client: Optional[BleakClient] = None
        self._queue: Optional[asyncio.Queue] = None
        self._pending_kind: Optional[str] = None
        self._drift_ms: Optional[float] = None   # phone_now - meter_now

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if self._client is None:
            self._client = BleakClient(self.target)
            await self._client.connect()
        self._queue = asyncio.Queue()
        await self._client.start_notify(VERIO_NOTIFY, self._on_notify)

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()
            self._client = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    # ------------------------------------------------------------- transport
    def _on_notify(self, _handle: int, data: bytearray) -> None:
        if self._queue is not None:
            self._queue.put_nowait(bytes(data))

    async def _next_frame(self) -> tuple[str, bytes]:
        while True:
            raw = await self._queue.get()
            kind, payload = parse_response(raw)
            if kind == "ack":
                continue                      # command ack; data frame follows
            return kind, payload

    async def _command(self, payload: bytes, kind: str) -> bytes:
        """Write one command, wait for its data frame, ack it."""
        if not self.connected:
            raise VerioError("not connected")
        self._pending_kind = kind
        await self._client.write_gatt_char(VERIO_WRITE, build_request(payload),
                                           response=True)
        try:
            rsp_kind, data = await asyncio.wait_for(self._next_frame(),
                                                    self.timeout)
        except asyncio.TimeoutError:
            raise VerioError(f"timeout waiting for response to {payload.hex()}")
        if rsp_kind == "error":
            raise VerioError(f"meter rejected {payload.hex()}: {data.decode()}")
        await self._client.write_gatt_char(VERIO_WRITE, bytes([ACK]),
                                           response=True)
        return data

    # ---------------------------------------------------------------- public
    @staticmethod
    def _le_int(data: bytes, expect: tuple[int, ...] = (2, 4)) -> int:
        """Little-endian unsigned int from a 2- or 4-byte result field.
        (Verio Flex uses 4-byte counters; Ultra Plus Reflect was observed
        answering the record-counter command with 2 bytes.)"""
        if len(data) not in expect:
            raise VerioError(
                f"response wrong length {len(data)} "
                f"(want {'/'.join(map(str, expect))}): {data.hex()}")
        fmt = {2: "<H", 4: "<I"}[len(data)]
        return struct.unpack(fmt, data)[0]

    async def sync_time(self) -> dt.datetime:
        """Query the meter clock and record the phone-vs-meter drift."""
        data = await self._command(bytes([0x20, 0x02]), _KIND_TIME)
        meter_unix = self._le_int(data, (4,)) + TIME_OFFSET
        meter_now = dt.datetime.fromtimestamp(meter_unix, dt.timezone.utc)
        if self.drift_correction:
            self._drift_ms = (dt.datetime.now(dt.timezone.utc)
                              - meter_now).total_seconds() * 1000.0
        return meter_now.replace(tzinfo=None)

    async def highest_record_number(self) -> int:
        data = await self._command(bytes([0x27, 0x00]), _KIND_Rcounter)
        return self._le_int(data)

    async def record_count(self) -> int:
        data = await self._command(bytes([0x0A, 0x02, 0x06]), _KIND_Tcounter)
        return self._le_int(data)

    async def read_record(self, number: int) -> Reading:
        data = await self._command(
            bytes([0xB3, number & 0xFF, (number >> 8) & 0xFF]), _KIND_RECORD)
        if len(data) != 11:
            raise VerioError(f"record {number} wrong length: {data.hex()}")
        r = decode_record(data)
        if self._drift_ms is not None:
            r.timestamp += dt.timedelta(milliseconds=self._drift_ms)
        r.sequence = number
        r.device = self.name
        return r

    async def download(self, timeout: float = 120.0,
                       max_records: int = 500) -> list[Reading]:
        """Full history: time, counters, then each record newest-first.

        The `27 00` watermark can lag the real memory (live Ultra Plus
        Reflect: counter said 8 while USB showed 10 slots — readings
        taken while Bluetooth was off do not bump it), so keep probing
        past it until the meter rejects records.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        await self.sync_time()
        highest = await self.highest_record_number()
        count = await self.record_count()
        log.info("verio: highest=%d count=%d", highest, count)
        readings: list[Reading] = []
        lo = max(highest - count, highest - max_records, 0)
        for number in range(highest, lo, -1):
            if asyncio.get_running_loop().time() > deadline:
                log.warning("verio download timeout after %d records",
                            len(readings))
                break
            try:
                readings.append(await self.read_record(number))
            except VerioError as e:
                log.warning("record %d failed: %s", number, e)
        # probe beyond the watermark (newest-first, stop after 2 rejects)
        number, fails = highest + 1, 0
        while fails < 2 and len(readings) < max_records:
            if asyncio.get_running_loop().time() > deadline:
                break
            try:
                readings.append(await self.read_record(number))
                number += 1
            except VerioError as e:
                log.debug("record %d beyond watermark rejected: %s",
                          number, e)
                fails += 1
                number += 1
        readings.sort(key=lambda r: (r.timestamp is None, r.timestamp,
                                     r.sequence or 0))
        return readings

    async def watch(self, poll_interval: float = 15.0):
        """Poll-based: these meters never push; new readings appear as the
        highest record number increases."""
        await self.sync_time()
        seen = await self.highest_record_number()
        while True:
            highest = await self.highest_record_number()
            while seen < highest:
                seen += 1
                yield await self.read_record(seen)
            await asyncio.sleep(poll_interval)

    async def gatt_dump(self) -> str:
        lines = []
        for svc in self._client.services:
            lines.append(f"service {svc.uuid}")
            for ch in svc.characteristics:
                lines.append(f"  char {ch.uuid}  [{','.join(ch.properties)}]")
        return "\n".join(lines)

    async def device_info(self) -> dict:
        return {"manufacturer": "LifeScan / OneTouch",
                "protocol": "verio (AF9DF7A1)"}
