"""Backend for TESTAmed GlucoCheck Advance (FORA Care rebrand).

Protocol reverse-engineered and live-verified; full writeup in
FINDINGS-fora.md.

Summary: BLE GATT service FFF0, one write+notify characteristic FFF1, no
bonding.  All exchanges are 8-byte frames ``51 CMD p2 p3 p4 p5 A3 CK``
with CK = sum of the first 7 bytes mod 256; the meter echoes the CMD byte.
Records are pulled one at a time: CMD 0x2B count, then per index CMD 0x25
(date) + CMD 0x26 (value).  The meter self-identifies with a FORA TD
project code (CMD 0x24); brand names never appear on the wire.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Optional

from bleak import BleakClient

from ..models import Reading

log = logging.getLogger("glucobridge.fora")

FORA_SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
FORA_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"

HDR = 0x51
TRL = 0xA3

CMD_DEVICE_ID = 0x24
CMD_RECORD_DATE = 0x25
CMD_RECORD_VALUE = 0x26
CMD_STORAGE = 0x27
CMD_LOT = 0x28
CMD_DEVINFO = 0x29
CMD_RECORD_COUNT = 0x2B
CMD_SETTINGS = 0x2F
CMD_TIME_SYNC = 0x33
CMD_RESET = 0x50

# models whose value field carries one decimal place (parser "Y" in the app)
DECIMAL_MODELS = {"TD4240", "TD4230", "TD3132", "TD4127", "TD4282", "TD4283"}

# meal/type marker, bits 6-7 of the 0x26 flag byte.
# Verified against the app's own log string:
#   "Type (0:General , 1:AC , 2:PC , 3:QC)"   (AC=before meal, PC=after)
MEAL_CODES = {0: None, 1: "preprandial", 2: "postprandial", 3: "qc"}


class ForaError(RuntimeError):
    pass


def frame(cmd: int, p2: int = 0, p3: int = 0, p4: int = 0, p5: int = 0) -> bytes:
    body = bytes([HDR, cmd & 0xFF, p2 & 0xFF, p3 & 0xFF, p4 & 0xFF, p5 & 0xFF, TRL])
    return body + bytes([sum(body) & 0xFF])


def checksum_ok(data: bytes) -> bool:
    return len(data) >= 8 and data[0] == HDR and sum(data[:7]) & 0xFF == data[7]


def parse_device_id(b: bytes) -> dict:
    code = "%02X%02X" % (b[3], b[2])
    return {
        "model": "TD" + code,
        "revision": chr(b[4] + 64),
        "user_mode": b[5],
    }


def parse_count(b: bytes) -> tuple[int, int]:
    return (b[3] << 8 | b[2]), (b[5] << 8 | b[4])   # total, transmitted


def parse_date(b: bytes) -> tuple[Optional[dt.datetime], bool]:
    day = b[2] & 0x1F
    month = ((b[2] >> 5) & 0x07) | ((b[3] & 0x01) << 3)
    year = 2000 + ((b[3] >> 1) & 0x1F)
    minute = b[4] & 0x3F
    hour = b[5] & 0x1F
    transmitted = bool(b[5] & 0x40)
    try:
        return dt.datetime(year, month, day, hour, minute), transmitted
    except ValueError:
        return None, transmitted


def encode_time(when: dt.datetime) -> bytes:
    """CMD 0x33 payload (exact inverse of parse_date; seconds dropped)."""
    m = when.month
    b2 = ((m & 0x07) << 5) | when.day
    b3 = (((when.year - 2000) & 0x1F) << 1) | (m >> 3)
    return bytes([b2, b3, when.minute, when.hour])


def parse_value(b: bytes, decimal: bool) -> Reading:
    raw = b[3] << 8 | b[2]
    value = raw * 0.1 if decimal else float(raw)
    r = Reading(value=value, unit="mg/dL", raw=bytes(b))
    code = b[5] & 0x3F
    if code:
        r.error = f"meter_code_{code}"
    meal = MEAL_CODES.get((b[5] >> 6) & 0x03)
    if meal == "qc":
        r.sample_type = "control_solution"
    else:
        r.meal = meal
    r.status = [f"ambient_temp_{b[4] & 0x7F}C"]
    return r


class ForaMeter:
    """One connection to a FORA-protocol meter (TESTAmed GlucoCheck etc.)."""

    implemented = True

    def __init__(self, target, name: Optional[str] = None,
                 user: int = 0, timeout: float = 5.0):
        self.target = target
        self.name = name or (target if isinstance(target, str) else
                             getattr(target, "name", None))
        self.user = user
        self.timeout = timeout
        self._client: Optional[BleakClient] = None
        self._buf = bytearray()
        self._pending: Optional[asyncio.Future] = None
        self.model: Optional[str] = None
        self.decimal = False

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if self._client is None:
            self._client = BleakClient(self.target)
            await self._client.connect()
        await self._client.start_notify(FORA_CHAR, self._on_notify)
        log.info("connected to FORA meter %s", self.name)

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
        self._buf.extend(data)
        # resync on 0x51 like the app's reader
        while self._buf and self._buf[0] != HDR:
            self._buf.pop(0)
        if len(self._buf) >= 8 and self._pending and not self._pending.done():
            resp = bytes(self._buf[:8])
            del self._buf[:8]
            self._pending.set_result(resp)

    async def _request(self, cmd: int, *params: int, retries: int = 1) -> bytes:
        if not self.connected:
            raise ForaError("not connected")
        cmd_frame = frame(cmd, *params)
        for attempt in range(retries + 1):
            self._buf.clear()  # like the app: flush RX before every write
            self._pending = asyncio.get_running_loop().create_future()
            try:
                await self._client.write_gatt_char(FORA_CHAR, cmd_frame,
                                                   response=True)
                resp = await asyncio.wait_for(self._pending, self.timeout)
            except asyncio.TimeoutError:
                await self._reset()
                if attempt < retries:
                    continue
                raise ForaError(f"timeout waiting for CMD 0x{cmd:02X} response")
            finally:
                self._pending = None
            if not checksum_ok(resp):
                log.warning("bad checksum from meter: %s", resp.hex())
                await self._reset()
                if attempt < retries:
                    continue
                raise ForaError(f"checksum error on response {resp.hex()}")
            if resp[1] != cmd:
                log.warning("cmd echo mismatch: want %02X got %02X", cmd, resp[1])
                await self._reset()
                if attempt < retries:
                    continue
                raise ForaError(f"unexpected echo 0x{resp[1]:02X} for CMD 0x{cmd:02X}")
            return resp
        raise ForaError("unreachable")

    async def _reset(self) -> None:
        """CMD 0x50: fire-and-forget protocol reset (app sends it on errors)."""
        try:
            await self._client.write_gatt_char(FORA_CHAR, frame(CMD_RESET),
                                               response=True)
        except Exception:
            pass

    # ---------------------------------------------------------------- public
    async def identify(self) -> dict:
        resp = await self._request(CMD_DEVICE_ID)
        info = parse_device_id(resp)
        self.model = info["model"]
        self.decimal = self.model in DECIMAL_MODELS
        return info

    async def sync_time(self, when: Optional[dt.datetime] = None) -> None:
        when = when or dt.datetime.now()
        await self._request(CMD_TIME_SYNC, *encode_time(when))

    async def record_count(self, user: Optional[int] = None) -> tuple[int, int]:
        resp = await self._request(CMD_RECORD_COUNT,
                                   user if user is not None else self.user)
        return parse_count(resp)

    async def download(self, timeout: float = 120.0,
                       users: Optional[list[int]] = None) -> list[Reading]:
        """Pull all stored records for the given user profile(s)."""
        loop_deadline = asyncio.get_running_loop().time() + timeout
        if self.model is None:
            await self.identify()
        readings: list[Reading] = []
        for user in (users or [self.user]):
            total, _sent = await self.record_count(user)
            log.info("%s user %d: %d records", self.model, user, total)
            consecutive_fail = 0
            for idx in range(total):
                if asyncio.get_running_loop().time() > loop_deadline:
                    log.warning("download timeout after %d records", len(readings))
                    break
                try:
                    d = await self._request(CMD_RECORD_DATE,
                                            idx & 0xFF, (idx >> 8) & 0xFF, 0, user)
                    ts, transmitted = parse_date(d)
                    v = await self._request(CMD_RECORD_VALUE,
                                            idx & 0xFF, (idx >> 8) & 0xFF, 0, user)
                    r = parse_value(v, self.decimal)
                except ForaError:
                    consecutive_fail += 1
                    if consecutive_fail >= 3:
                        log.warning("3 consecutive record failures at idx %d; "
                                    "stopping user %d", idx, user)
                        break
                    continue
                consecutive_fail = 0
                r.timestamp = ts
                r.sequence = idx
                r.device = self.name or self.model
                if transmitted:
                    r.status.append("transmitted")
                readings.append(r)
        readings.sort(key=lambda r: (r.timestamp is None, r.timestamp,
                                     r.sequence or 0))
        return readings

    async def watch(self, poll_interval: float = 10.0):
        """Poll-based live feed: FORA meters never push, so we poll the
        record count and fetch anything new."""
        if self.model is None:
            await self.identify()
        seen, _ = await self.record_count()
        while True:
            total, _ = await self.record_count()
            while seen < total:
                idx = seen
                d = await self._request(CMD_RECORD_DATE,
                                        idx & 0xFF, (idx >> 8) & 0xFF, 0, self.user)
                ts, _t = parse_date(d)
                v = await self._request(CMD_RECORD_VALUE,
                                        idx & 0xFF, (idx >> 8) & 0xFF, 0, self.user)
                r = parse_value(v, self.decimal)
                r.timestamp = ts
                r.sequence = idx
                r.device = self.name or self.model
                seen += 1
                yield r
            await asyncio.sleep(poll_interval)

    async def gatt_dump(self) -> str:
        c = self._client
        lines = []
        for svc in c.services:
            lines.append(f"service {svc.uuid}")
            for ch in svc.characteristics:
                lines.append(f"  char {ch.uuid}  [{','.join(ch.properties)}]")
        return "\n".join(lines)

    async def device_info(self) -> dict:
        info = await self.identify()
        return {"manufacturer": "FORA Care", "model": info["model"],
                "revision": info["revision"]}
