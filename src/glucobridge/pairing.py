"""BLE pairing helpers for BlueZ.

Meters like the Beurer GL 49 and Accu-Chek Guide display a 6-digit passkey
that the central must echo ("passkey display" on the peripheral side).
BlueZ routes that through an agent; bleak does not ship one, so we register
a temporary agent over D-Bus (dbus-fast is a bleak dependency on Linux).

Typical use from the CLI:

    glucobridge pair AA:BB:CC:DD:EE:FF --passkey 123456

After one successful pair the OS stores the keys and later connects are
transparent.
"""
from __future__ import annotations

import asyncio
from typing import Optional

BLUEZ_SERVICE = "org.bluez"
AGENT_IFACE = "org.bluez.Agent1"
DEVICE_IFACE = "org.bluez.Device1"
ADAPTER_IFACE = "org.bluez.Adapter1"


async def pair(address: str, passkey: Optional[str] = None,
               adapter: str = "hci0", timeout: float = 60.0) -> None:
    """Pair with a device, answering the passkey prompt with `passkey`.

    If the meter shows a 6-digit code, pass it as `passkey`.  If pairing
    uses "just works" confirmation, the agent auto-confirms.
    """
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method
    from dbus_fast import BusType

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    class Agent1(ServiceInterface):
        def __init__(self):
            super().__init__(AGENT_IFACE)
            self._passkey = int(passkey) if passkey else None

        @method()
        def Release(self):
            pass

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
            pass

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):
            pass

        @method()
        def RequestPinCode(self, device: "o") -> "s":
            if self._passkey is None:
                raise NotImplementedError("no pincode available")
            return str(self._passkey)

        @method()
        def RequestPasskey(self, device: "o") -> "u":
            if self._passkey is None:
                raise NotImplementedError(
                    "device requires a passkey; rerun with --passkey NNNNNN")
            return self._passkey

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):
            pass  # auto-confirm "just works"

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):
            pass

        @method()
        def Cancel(self):
            pass

    obj = bus.get_interface(BLUEZ_SERVICE, "/org/bluez")
    agent_path = "/org/bluez/glucobridge_agent"
    iface = Agent1()
    bus.export(agent_path, iface)

    await obj.call_introspectable()
    agent_mgr = bus.get_interface(BLUEZ_SERVICE, "/org/bluez")
    await agent_mgr.call_method(AGENT_IFACE, "RegisterAgent",
                                "ob", agent_path, "KeyboardDisplay")
    await agent_mgr.call_method(AGENT_IFACE, "RequestDefault", ())

    dev_path = f"/org/bluez/{adapter}/dev_{address.replace(':', '_')}"
    dev = bus.get_interface(BLUEZ_SERVICE, dev_path)
    try:
        await asyncio.wait_for(dev.call_method(DEVICE_IFACE, "Pair", ()),
                               timeout=timeout)
    finally:
        try:
            await agent_mgr.call_method(AGENT_IFACE, "UnregisterAgent",
                                        "o", agent_path)
        except Exception:
            pass
        bus.disconnect()
