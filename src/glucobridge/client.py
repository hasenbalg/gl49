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
    ("gl?4[5-9]*", "gls"),           # Beurer GL 45/46/48/49 (spaced)
    ("gl4[5-9]*", "gls"),            # GL49 advertises WITHOUT space (live)
    ("beurer*", "gls"),
    ("acu?chek?guide*", "gls"),      # Accu-Chek Guide / Guide Me
    ("acucheck*", "gls"),
    ("guide*", "gls"),
    ("glucocheck*", "gls"),          # TESTAmed GlucoCheck Advance (live-verified: standard GLS)
    ("fora*", "fora"),
    ("td?link*", "fora"),            # FORA TD-Link series
    ("td[0-9][0-9]*", "fora"),       # FORA meters advertise their TD code
    ("onetouch*", "verio"),          # OneTouch Ultra/Verio Reflect & Flex
    ("one touch*", "verio"),
    ("verio*", "verio"),
    ("ultra?plus*", "verio"),
]

# service UUIDs seen in advertisements -> backend.  Order matters when a
# device advertises several hints: LifeScan Reflect-family meters advertise
# the standard Glucose Service as a decoy next to their real AF9DF7A1
# service (2A18 only appears after bonding, and is empty even then).
SERVICE_HINTS = {
    "af9df7a1-e595-11e3-96b4-0002a5d5c51b": "verio",
    gatt.GLUCOSE_SERVICE: "gls",
    "0000fff0-0000-1000-8000-00805f9b34fb": "fora",
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
    # Name patterns win: the Reflect family advertises the standard
    # Glucose Service as a decoy, and its name is the reliable signal.
    lname = (name or "").lower().strip()
    if lname:
        for pattern, backend in NAME_PATTERNS:
            if fnmatch.fnmatch(lname, pattern):
                return backend
    uuids = {u.lower() for u in service_uuids or []}
    for uuid, backend in SERVICE_HINTS.items():   # dict order = priority
        if uuid in uuids:
            return backend
    return "unknown"


async def _cached_devices() -> list[Advertisement]:
    """Devices BlueZ already knows from earlier discoveries.

    bleak's discover() only reports devices that *appear* during the scan
    window; a meter discovered moments earlier (e.g. by bluetoothctl) sits
    in BlueZ's object cache and would otherwise make scans look empty.
    """
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast import BusType
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        intro = await bus.introspect("org.bluez", "/")
        obj = bus.get_proxy_object("org.bluez", "/", intro)
        manager = obj.get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await manager.call_get_managed_objects()
        bus.disconnect()
    except Exception:
        return []
    return _bus_devices_to_advs(objects)


def _dbus_val(x):
    """dbus-fast wraps 'v'-signature fields in Variant objects; unwrap.

    Without this, iterating e.g. org.bluez.Device1.UUIDs raises
    "'dbus_fast.signature.Variant' object is not iterable" (crashes for
    any cached device with discovered GATT UUIDs), and str(Variant)
    corrupts Address/Name into repr garbage.
    """
    return x.value if hasattr(x, "value") and hasattr(x, "signature") else x


def _bus_devices_to_advs(objects) -> list[Advertisement]:
    """org.bluez GetManagedObjects payload -> Advertisement list."""
    out = []
    for path, ifaces in objects.items():
        dev = ifaces.get("org.bluez.Device1")
        if not dev:
            continue
        addr = _dbus_val(dev.get("Address"))
        if not addr:
            continue
        name = _dbus_val(dev.get("Name")) or _dbus_val(dev.get("Alias"))
        uuids = [str(u).lower()
                 for u in (_dbus_val(dev.get("UUIDs")) or [])]
        rssi = _dbus_val(dev.get("RSSI"))
        out.append(Advertisement(
            address=str(addr),
            name=str(name) if name else None,
            rssi=int(rssi) if rssi is not None else None,
            has_glucose_service=any(
                u.lower() == gatt.GLUCOSE_SERVICE for u in uuids),
            backend=classify(str(name) if name else None, uuids),
            service_uuids=uuids,
        ))
    return out


async def scan(timeout: float = 10.0, all_devices: bool = False) -> list[Advertisement]:
    """Scan for BLE glucose meters.

    By default only devices that advertise the Glucose Service or match a
    known meter name are returned; pass all_devices=True for everything.
    Devices cached from earlier discoveries are included as well (marked
    with rssi=None when their last RSSI is unknown).
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
    seen = {a.address for a in results}
    for cached in await _cached_devices():
        if cached.address in seen:
            continue
        if not all_devices and cached.backend == "unknown":
            continue
        seen.add(cached.address)
        results.append(cached)
    results.sort(key=lambda a: -(a.rssi if a.rssi is not None else -999))
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
        if backend in (None, "unknown") and isinstance(target, str):
            backend = await self._resolve_backend(target)
        if backend in (None, "unknown"):
            backend = "gls"  # GLS is the sane default; it will fail loudly
        cls = {"gls": GlucoseMeter, "fora": ForaMeter, "verio": VerioMeter}[backend]
        self._meter = cls(target)
        await self._meter.connect()
        return self._meter

    async def _resolve_backend(self, address: str) -> Optional[str]:
        """Classify a bare MAC address: BlueZ cache first, then a short scan."""
        for adv in await _cached_devices():
            if adv.address == address and adv.backend != "unknown":
                return adv.backend
        try:
            found = await BleakScanner.discover(timeout=5.0, return_adv=True)
            for dev, adv in found.values():
                if dev.address == address:
                    b = classify(adv.local_name or dev.name,
                                 list(adv.service_uuids or []))
                    if b != "unknown":
                        return b
        except Exception:
            pass
        return None

    async def download(self, target, timeout: float = 60.0,
                       backend: Optional[str] = None):
        meter = await self.connect(target, backend=backend)
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
