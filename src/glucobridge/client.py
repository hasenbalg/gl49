"""Top-level API: scan for glucose meters and open the right backend."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Optional, Union

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTServiceCollection  # noqa: F401

from . import gatt
from .backends.gls import GlucoseMeter
from .backends.fora import ForaMeter
from .backends.verio import VerioMeter

# name patterns -> backend.  Matched case-insensitively against the BLE
# advertisement name.
NAME_PATTERNS = [
    ("gl?4[5-9]", "gls"),            # Beurer GL 45/46/48/49
    ("beurer*", "gls"),
    ("acu?chek?guide*", "gls"),      # Accu-Chek Guide / Guide Me
    ("acucheck*", "gls"),
    ("guide*", "gls"),
    ("glucocheck*", "fora"),         # TESTAmed GlucoCheck Advance
    ("fora*", "fora"),
    ("td?link*", "fora"),            # FORA TD-Link series
    ("td[0-9][0-9]*", "fora"),       # FORA meters advertise their TD code
    ("one?touch*", "verio"),         # OneTouch Ultra/Verio Reflect & Flex
    ("verio*", "verio"),
    ("ultra?plus*", "verio"),
]

# service UUIDs seen in advertisements -> backend
SERVICE_HINTS = {
    gatt.GLUCOSE_SERVICE: "gls",
    "0000fff0-0000-1000-8000-00805f9b34fb": "fora",
    "af9df7a1-e595-11e3-96b4-0002a5d5c51b": "verio",
}


@dataclass
class Advertisement:
    address: str
    name: Optional[str]
    rssi: Optional[int]
    has_glucose_service: bool
    backend: str                      # "gls" | "fora" | "unknown"
    service_uuids: list = field(default_factory=list)

    def __str__(self) -> str:
        name = self.name or "(unnamed)"
        return (f"{self.address}  {name:<28} rssi={self.rssi} "
                f"backend={self.backend}")


def classify(name: Optional[str], service_uuids: list[str]) -> str:
    for u in service_uuids or []:
        backend = SERVICE_HINTS.get(u.lower())
        if backend:
            return backend
    lname = (name or "").lower().strip()
    if not lname:
        return "unknown"
    for pattern, backend in NAME_PATTERNS:
        if fnmatch.fnmatch(lname, pattern):
            return backend
    return "unknown"


async def scan(timeout: float = 10.0, all_devices: bool = False) -> list[Advertisement]:
    """Scan for BLE glucose meters.

    By default only devices that advertise the Glucose Service or match a
    known meter name are returned; pass all_devices=True for everything.
    """
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results = []
    for dev, adv in found.values():
        uuids = list(adv.service_uuids or [])
        backend = classify(adv.local_name or dev.name, uuids)
        if not all_devices and backend == "unknown":
            continue
        results.append(Advertisement(
            address=dev.address,
            name=adv.local_name or dev.name,
            rssi=adv.rssi,
            has_glucose_service=any(
                u.lower() == gatt.GLUCOSE_SERVICE for u in uuids),
            backend=backend,
            service_uuids=uuids,
        ))
    results.sort(key=lambda a: -(a.rssi or -999))
    return results


class GlucoBridge:
    """Entry point tying scan + backend selection together.

    Usage::

        async with GlucoBridge() as gb:
            devices = await gb.scan()
            readings = await gb.download(devices[0])
    """

    def __init__(self, adapter=None):
        self.adapter = adapter
        self._meter = None

    async def scan(self, timeout: float = 10.0,
                   all_devices: bool = False) -> list[Advertisement]:
        return await scan(timeout, all_devices)

    async def connect(self, target: Union[str, BLEDevice, Advertisement],
                      backend: Optional[str] = None):
        """Open a connection, picking the backend by advertisement/name."""
        if isinstance(target, Advertisement):
            backend = backend or target.backend
            target = target.address
        if backend in (None, "unknown"):
            backend = "gls"  # GLS is the sane default; it will fail loudly
        cls = {"gls": GlucoseMeter, "fora": ForaMeter, "verio": VerioMeter}[backend]
        self._meter = cls(target)
        await self._meter.connect()
        return self._meter

    async def download(self, target, timeout: float = 60.0):
        meter = await self.connect(target)
        try:
            return await meter.download(timeout=timeout)
        finally:
            await meter.disconnect()

    async def close(self):
        if self._meter:
            await self._meter.disconnect()
            self._meter = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
