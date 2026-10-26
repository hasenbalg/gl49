"""Backend for meters implementing the standard Bluetooth Glucose Service
(0x1808): Beurer GL 49, Accu-Chek Guide, and other GLS-certified meters."""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Callable, Optional

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from .. import gatt, racp
from ..models import Reading
from ..records import (
    RecordError,
    merge_context,
    parse_glucose_context,
    parse_glucose_measurement,
)

log = logging.getLogger("glucobridge.gls")


class GlucoseMeter:
    """One connection to a GLS meter."""

    def __init__(self, target: "str | BLEDevice", name: Optional[str] = None,
                 lenient: bool = True):
        self.target = target
        self.name = name or (target if isinstance(target, str) else getattr(target, "name", None))
        self.lenient = lenient
        self._client: Optional[BleakClient] = None
        self._measurements: dict[int, Reading] = {}   # seq -> Reading (download)
        self._contexts: dict[int, Reading] = {}
        self._live_cb: Optional[Callable[[Reading], None]] = None
        self._racp_done: Optional[asyncio.Event] = None
        self._racp_last: Optional[racp.RACPResponse] = None
        self._num_stored: Optional[int] = None
        self._errors: list[str] = []

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if self._client is None:
            self._client = BleakClient(self.target)
            await self._client.connect()
        log.info("connected to %s", self.name)

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

    # ------------------------------------------------------------- internals
    @staticmethod
    def _norm_uuid(u: str) -> str:
        h = u.replace("-", "").lower()
        if len(h) == 4:  # 16-bit short form -> base UUID
            h = "0000" + h + "00001000800000805f9b34fb"
        return h

    def _char(self, uuid: str):
        """Resolve a characteristic object, preferring the Glucose Service.

        Some meters (live-observed on Accu-Chek Guide) expose the same
        characteristic UUID under more than one service; bleak then rejects
        bare UUID strings with "Multiple Characteristics with this UUID".
        Passing the characteristic object from the standard Glucose Service
        (0x1808) keeps the standard profile authoritative.
        """
        c = self._require()
        want = self._norm_uuid(uuid)
        want_svc = self._norm_uuid(gatt.GLUCOSE_SERVICE)
        fallback = None
        for svc in c.services:
            for ch in svc.characteristics:
                if self._norm_uuid(ch.uuid) != want:
                    continue
                if self._norm_uuid(svc.uuid) == want_svc:
                    return ch
                fallback = fallback or ch
        if fallback is not None:
            return fallback
        raise KeyError(uuid)

    def _require(self):
        if not self.connected:
            raise RuntimeError("not connected — call connect() first")
        return self._client

    def _on_measurement(self, _handle: int, data: bytearray) -> None:
        try:
            r = parse_glucose_measurement(bytes(data), lenient=self.lenient)
        except RecordError as e:
            log.warning("bad measurement record: %s (%s)", e, bytes(data).hex())
            self._errors.append(str(e))
            return
        r.device = self.name
        if self._racp_done is not None:
            self._measurements[r.sequence] = r
        if self._live_cb:
            self._live_cb(r)

    def _on_context(self, _handle: int, data: bytearray) -> None:
        try:
            c = parse_glucose_context(bytes(data))
        except RecordError as e:
            log.warning("bad context record: %s (%s)", e, bytes(data).hex())
            return
        c.device = self.name
        if self._racp_done is not None:
            self._contexts[c.sequence] = c
        if self._live_cb and c.sequence in self._measurements:
            merge_context(self._measurements[c.sequence], c)

    def _on_racp(self, _handle: int, data: bytearray) -> None:
        try:
            resp = racp.parse_response(bytes(data))
        except ValueError as e:
            log.warning("bad RACP response: %s (%s)", e, bytes(data).hex())
            return
        self._racp_last = resp
        log.debug("RACP: %s", resp)
        if resp.op_code == racp.OP_NUM_STORED_RESPONSE:
            self._num_stored = resp.count
        if resp.is_done and self._racp_done is not None:
            self._racp_done.set()

    async def _subscribe_all(self):
        c = self._require()
        await c.start_notify(self._char(gatt.GLUCOSE_MEASUREMENT),
                             self._on_measurement)
        try:
            await c.start_notify(self._char(gatt.GLUCOSE_MEASUREMENT_CONTEXT),
                                 self._on_context)
        except Exception as e:  # context char is optional in practice
            log.debug("no context characteristic notifications: %s", e)
        await c.start_notify(self._char(gatt.RECORD_ACCESS_CONTROL_POINT),
                             self._on_racp)

    # ---------------------------------------------------------------- public
    async def record_count(self) -> Optional[int]:
        """Ask the meter how many records it stores (0x04/0x05 exchange)."""
        c = self._require()
        await self._subscribe_all()
        self._racp_done = asyncio.Event()
        self._num_stored = None
        try:
            await c.write_gatt_char(self._char(gatt.RECORD_ACCESS_CONTROL_POINT),
                                    racp.report_num_stored_all(),
                                    response=True)
            try:
                await asyncio.wait_for(self._racp_done.wait(), timeout=10)
            except asyncio.TimeoutError:
                # some meters answer 0x05 but never send the 0x06 terminator
                pass
            return self._num_stored
        finally:
            self._racp_done = None

    async def download(self, timeout: float = 60.0) -> list[Reading]:
        """Pull the full stored history via RACP 'report stored records'."""
        c = self._require()
        self._measurements.clear()
        self._contexts.clear()
        self._errors.clear()
        await self._subscribe_all()

        self._racp_done = asyncio.Event()
        try:
            await c.write_gatt_char(self._char(gatt.RECORD_ACCESS_CONTROL_POINT),
                                    racp.report_stored_all(),
                                    response=True)
            try:
                await asyncio.wait_for(self._racp_done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("RACP transfer did not terminate cleanly after "
                            "%.0fs; returning %d records received",
                            timeout, len(self._measurements))
        finally:
            self._racp_done = None

        readings = []
        for seq, r in self._measurements.items():
            merge_context(r, self._contexts.get(seq))
            readings.append(r)
        readings.sort(key=lambda r: (r.timestamp is None, r.timestamp, r.sequence or 0))
        return readings

    async def watch(self) -> AsyncIterator[Reading]:
        """Yield live measurements as the meter sends them.

        The meter typically pushes its record when you power it off, pull up
        the memory, or eject the strip (device-dependent).  Context records
        arriving shortly after are merged into the same Reading object.
        """
        self._require()
        await self._subscribe_all()
        queue: asyncio.Queue[Reading] = asyncio.Queue()

        def cb(r: Reading) -> None:
            queue.put_nowait(r)

        self._live_cb = cb
        try:
            while True:
                yield await queue.get()
        finally:
            self._live_cb = None

    async def device_info(self) -> dict:
        """Read the Device Information service if present."""
        c = self._require()
        info = {}
        for uuid, key in ((gatt.MANUFACTURER_NAME, "manufacturer"),
                          (gatt.MODEL_NUMBER, "model"),
                          (gatt.FIRMWARE_REVISION, "firmware")):
            try:
                v = await c.read_gatt_char(self._char(uuid))
                info[key] = v.decode("utf-8", "replace")
            except Exception:
                pass
        return info

    async def gatt_dump(self) -> str:
        """Human-readable GATT tree — diagnostic aid for unknown meters."""
        c = self._require()
        lines = []
        for svc in c.services:
            lines.append(f"service {svc.uuid}  ({svc.description or 'primary'})")
            for ch in svc.characteristics:
                props = ",".join(ch.properties)
                lines.append(f"  char {ch.uuid}  [{props}]")
                for d in ch.descriptors:
                    lines.append(f"    desc {d.uuid}")
        return "\n".join(lines)
