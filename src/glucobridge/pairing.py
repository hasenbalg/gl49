"""BLE pairing helpers for BlueZ.

Meters like the Beurer GL 49 and Accu-Chek Guide display a 6-digit passkey
that the central must echo ("passkey display" on the peripheral side).
BlueZ routes that through an agent; bleak does not ship one, so we register
a temporary agent over D-Bus (dbus-fast is a bleak dependency on Linux).

Every agent callback is traced to stderr with a timestamp: when pairing
still fails, the trace shows exactly where the handshake died (e.g. BlueZ
calling Cancel() right after our reply, or a stale agent stealing the
prompt via Release()).

Typical use from the CLI:

    glucobridge pair AA:BB:CC:DD:EE:FF

The passkey prompt appears while the meter shows the code; type it and
press Enter.  After one successful pair the OS stores the keys and later
connects are transparent.  Gotchas: the code is generated per attempt (a
pre-passed --passkey from an earlier screen is always stale), only ONE
pairing attempt may run at a time, and every other D-Bus agent (leftover
bluetoothctl, blueman, desktop GUI) must be gone first -- competing
agents produce misleading "Authentication Canceled" failures.
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Optional

BLUEZ_SERVICE = "org.bluez"
AGENT_IFACE = "org.bluez.Agent1"
DEVICE_IFACE = "org.bluez.Device1"
ADAPTER_IFACE = "org.bluez.Adapter1"

HINTS = {
    "AuthenticationCanceled":
        "pairing was aborted on OUR side: input took too long, or another "
        "D-Bus agent (leftover bluetoothctl, blueman, desktop bluetooth) "
        "stole the prompt -- `pkill bluetoothctl`, close bluetooth GUIs, "
        "retry and answer the prompt immediately",
    "AuthenticationRejected":
        "the meter rejected the pairing -- the code is generated per "
        "attempt, so re-read it fresh from the screen; the meter must sit "
        "in its pairing screen (shows PA)",
    "AuthenticationFailed":
        "SMP confirm-value mismatch -- stale/wrong passkey, or two "
        "pairing attempts overlapped; make exactly ONE attempt with a "
        "fresh code",
    "InProgress":
        "another pairing attempt is still running -- wait ~5 s and make a "
        "single attempt",
    "NotReady":
        "meter fell asleep mid-handshake -- wake it (BT/transfer mode) "
        "and retry promptly",
    "Timeout":
        "the meter did not answer -- it leaves the pairing window "
        "quickly; re-enter pairing mode and retry immediately",
}


def _hint(err: Exception) -> str:
    text = str(err)
    for name, hint in HINTS.items():
        if name in text:
            return f"hint: {hint}"
    return ""


def _trace(msg: str, tag: str = "pair") -> None:
    print(f"[{tag} {time.monotonic() % 3600:7.2f}s] {msg}", file=sys.stderr)


async def _ask(prompt: str) -> int:
    """Read a code from stdin without blocking the D-Bus event loop."""
    loop = asyncio.get_running_loop()
    text = await loop.run_in_executor(None, input, prompt)
    return int(text.strip())


async def pair(address: str, passkey: Optional[str] = None,
               adapter: str = "hci0", timeout: float = 120.0,
               capability: Optional[str] = None,
               trust: bool = True, renew: bool = False) -> None:
    """Pair with a device, answering the passkey prompt interactively or
    with a pre-supplied `passkey`.

    If the device is already bonded this returns without touching the
    keys (idempotent) unless renew=True, which first removes the BlueZ
    bond and pairs from scratch.  Some firmwares -- definitively the
    Accu-Chek Guide, while the GL49 and Reflect do not care -- REFUSE
    re-pairing while they still hold their side of an old bond, so a
    renew may also require a hard power-cycle of the meter; removing
    the host side is always automatic here.

    Without an explicit `capability`, try passkey entry first and fall
    back to Just Works: some firmwares refuse passkey-entry pairing even
    while displaying a code.  On success the device is marked trusted
    (unless trust=False) so it reconnects inside its transfer windows.
    """
    if capability:
        return await _pair_once(address, passkey, adapter, timeout,
                                capability, trust, renew)
    try:
        return await _pair_once(address, passkey, adapter, timeout,
                                "KeyboardDisplay", trust, renew)
    except Exception as first:
        hint = _hint(first)
        _trace(f"passkey-entry pairing failed: {first}")
        if hint:
            _trace(hint[len("hint: "):])
        _trace("retrying as Just Works (NoInputNoOutput)...")
        try:
            return await _pair_once(address, passkey, adapter, timeout,
                                    "NoDisplayNoKeyboard", trust, renew)
        except Exception:
            if hint:
                print(hint, file=sys.stderr)
            raise first from None


async def unpair(address: str, adapter: str = "hci0") -> bool:
    """Remove the BlueZ-side bond for a device (``Device1.Remove()``).

    Returns True if a bond was removed, False if BlueZ had nothing to
    remove (idempotent).  What happens on the METER side afterwards
    differs per model: the Beurer GL49 and OneTouch Reflect re-pair
    without any further step, while the Accu-Chek Guide keeps its
    half of the keys (no unpair UI) and silently ignores fresh
    pairings until it is hard power-cycled (batteries out ~10 s) --
    assume Guide-like behavior unless a meter is known otherwise.
    Only use this to hand the meter to another host; to keep using
    it here, just leave the bond alone.
    """
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        dev_path = f"/org/bluez/{adapter}/dev_{address.replace(':', '_')}"
        root_intro = await bus.introspect(BLUEZ_SERVICE, "/")
        root_obj = bus.get_proxy_object(BLUEZ_SERVICE, "/", root_intro)
        manager = root_obj.get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await manager.call_get_managed_objects()
        if dev_path not in objects or DEVICE_IFACE not in objects[dev_path]:
            _trace(f"{address} is not known to BlueZ -- nothing to remove",
                   "unpair")
            return False
        dev_intro = await bus.introspect(BLUEZ_SERVICE, dev_path)
        dev_obj = bus.get_proxy_object(BLUEZ_SERVICE, dev_path, dev_intro)
        dev_iface = dev_obj.get_interface(DEVICE_IFACE)
        call_remove = getattr(dev_iface, "call_remove", None)
        if call_remove is not None:
            await call_remove()
        else:
            # Some BlueZ/dbus-fast combinations expose Device1 without
            # the Remove method in their generated proxy (seen live on
            # BlueZ 5.x + dbus-fast 5.2.0).  Adapter1.RemoveDevice()
            # removes the same bond and device object.
            _trace("Device1.Remove not exposed; using "
                   f"Adapter1.RemoveDevice({dev_path})", "unpair")
            adp_intro = await bus.introspect(BLUEZ_SERVICE,
                                             f"/org/bluez/{adapter}")
            adp_obj = bus.get_proxy_object(BLUEZ_SERVICE,
                                           f"/org/bluez/{adapter}",
                                           adp_intro)
            await adp_obj.get_interface(ADAPTER_IFACE) \
                        .call_remove_device(dev_path)
        _trace(f"removed BlueZ bond for {address}", "unpair")
        return True
    finally:
        bus.disconnect()


async def _pair_once(address: str, passkey: Optional[str],
                     adapter: str, timeout: float,
                     capability: str, trust: bool, renew: bool) -> None:
    from dbus_fast import BusType, Variant
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    class Agent1(ServiceInterface):
        def __init__(self):
            super().__init__(AGENT_IFACE)
            self._passkey = int(passkey) if passkey else None

        @method()
        def Release(self):
            _trace("agent Release() -- another agent took default status")

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
            _trace(f"host must display passkey {passkey:06d}")

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):
            _trace(f"host must display pincode {pincode}")

        @method()
        async def RequestPinCode(self, device: "o") -> "s":
            _trace("BlueZ -> RequestPinCode")
            if self._passkey is None:
                self._passkey = await _ask("PIN code shown on device: ")
            _trace(f"sending PIN {self._passkey}")
            return str(self._passkey)

        @method()
        async def RequestPasskey(self, device: "o") -> "u":
            _trace("BlueZ -> RequestPasskey")
            if self._passkey is None:
                self._passkey = await _ask("passkey shown on device: ")
            _trace(f"sending passkey {self._passkey:06d}")
            return self._passkey

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):
            _trace(f"BlueZ -> RequestConfirmation({passkey:06d}) "
                   "auto-confirmed")

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):
            _trace(f"BlueZ -> AuthorizeService({uuid})")

        @method()
        def Cancel(self):
            _trace("agent Cancel() -- BlueZ aborted this pairing attempt")

    agent_path = "/org/bluez/glucobridge_agent"
    dev_path = f"/org/bluez/{adapter}/dev_{address.replace(':', '_')}"

    def _unwrap(x):
        return x.value if hasattr(x, "value") and hasattr(x, "signature") \
            else x

    root_intro = await bus.introspect(BLUEZ_SERVICE, "/")
    root_obj = bus.get_proxy_object(BLUEZ_SERVICE, "/", root_intro)
    manager = root_obj.get_interface("org.freedesktop.DBus.ObjectManager")
    objects = await manager.call_get_managed_objects()

    # Already bonded?  Don't touch the keys -- firmware on many meters
    # refuses re-pairing while it holds its side of the old bond, and a
    # fresh Pair() attempt can destroy a working bond on BOTH sides.
    if dev_path in objects:
        paired = _unwrap(objects[dev_path].get(DEVICE_IFACE, {})
                         .get("Paired"))
        if paired and not renew:
            dev_intro = await bus.introspect(BLUEZ_SERVICE, dev_path)
            dev_obj = bus.get_proxy_object(BLUEZ_SERVICE, dev_path,
                                           dev_intro)
            props = dev_obj.get_interface(
                "org.freedesktop.DBus.Properties")
            trusted = _unwrap(await props.call_get(DEVICE_IFACE,
                                                   "Trusted"))
            if trust and not trusted:
                await props.call_set(DEVICE_IFACE, "Trusted",
                                     Variant("b", True))
            bus.disconnect()
            _trace("already paired with BlueZ -- nothing to do "
                   "(use --renew to force a fresh pairing)")
            return
        if renew:
            _trace("--renew: removing existing BlueZ bond first")
            adp_intro = await bus.introspect(BLUEZ_SERVICE,
                                             f"/org/bluez/{adapter}")
            adp = bus.get_proxy_object(BLUEZ_SERVICE, f"/org/bluez/{adapter}",
                                       adp_intro)
            try:
                await adp.get_interface(ADAPTER_IFACE).call_remove_device(
                    dev_path)
            except Exception as e:
                _trace(f"RemoveDevice: {e}")
            objects = await manager.call_get_managed_objects()

    iface = Agent1()
    bus.export(agent_path, iface)

    intro = await bus.introspect(BLUEZ_SERVICE, "/org/bluez")
    bluez = bus.get_proxy_object(BLUEZ_SERVICE, "/org/bluez", intro)
    agent_mgr = bluez.get_interface("org.bluez.AgentManager1")
    await agent_mgr.call_register_agent(agent_path, capability)
    await agent_mgr.call_request_default_agent(agent_path)
    _trace(f"agent registered as {capability}, calling Pair...")

    # A forgotten device has no Device1 object until re-discovered.
    # NOTE: BleakScanner.discover() always runs the FULL timeout before
    # returning -- meters sit in a pairing window of roughly 30 s, so a
    # single long scan reliably expires it (observed as
    # "Authentication Canceled" ~30 s into SMP).  Scan in short bursts
    # and return the instant the target shows up.
    if dev_path not in objects:
        from bleak import BleakScanner
        _trace(f"{dev_path} not cached by BlueZ, discovering...")
        deadline = time.monotonic() + min(timeout, 60.0)
        found = False
        while time.monotonic() < deadline and not found:
            for d in await BleakScanner.discover(timeout=3.0):
                if d.address == address:
                    found = True
                    break
        if not found:
            bus.disconnect()
            raise RuntimeError(
                f"{address} not discoverable — make the meter "
                f"advertising and retry")

    dev_intro = await bus.introspect(BLUEZ_SERVICE, dev_path)
    dev_obj = bus.get_proxy_object(BLUEZ_SERVICE, dev_path, dev_intro)
    dev = dev_obj.get_interface(DEVICE_IFACE)
    try:
        await asyncio.wait_for(dev.call_pair(), timeout=timeout)
        if trust:
            props = dev_obj.get_interface("org.freedesktop.DBus.Properties")
            await props.call_set(DEVICE_IFACE, "Trusted", Variant("b", True))
            _trace("device marked trusted")
        _trace("paired")
    finally:
        try:
            await agent_mgr.call_unregister_agent(agent_path)
        except Exception:
            pass
        bus.disconnect()
