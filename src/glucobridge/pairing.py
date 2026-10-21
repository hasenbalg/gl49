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


async def _ask(prompt: str) -> int:
    """Read a code from stdin without blocking the D-Bus event loop."""
    loop = asyncio.get_running_loop()
    text = await loop.run_in_executor(None, input, prompt)
    return int(text.strip())


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
            print(f"host passkey: {passkey:06d}")

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):
            print(f"host pincode: {pincode}")

        @method()
        async def RequestPinCode(self, device: "o") -> "s":
            if self._passkey is None:
                self._passkey = await _ask("PIN code shown on device: ")
            return str(self._passkey)

        @method()
        async def RequestPasskey(self, device: "o") -> "u":
            if self._passkey is None:
                self._passkey = await _ask("passkey shown on device: ")
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

    agent_path = "/org/bluez/glucobridge_agent"
    iface = Agent1()
    bus.export(agent_path, iface)

    intro = await bus.introspect(BLUEZ_SERVICE, "/org/bluez")
    bluez = bus.get_proxy_object(BLUEZ_SERVICE, "/org/bluez", intro)
    agent_mgr = bluez.get_interface("org.bluez.AgentManager1")
    await agent_mgr.call_register_agent(agent_path, "KeyboardDisplay")
    await agent_mgr.call_request_default()

    dev_path = f"/org/bluez/{adapter}/dev_{address.replace(':', '_')}"
    dev_intro = await bus.introspect(BLUEZ_SERVICE, dev_path)
    dev_obj = bus.get_proxy_object(BLUEZ_SERVICE, dev_path, dev_intro)
    dev = dev_obj.get_interface(DEVICE_IFACE)
    try:
        await asyncio.wait_for(dev.call_pair(), timeout=timeout)
    finally:
        try:
            await agent_mgr.call_unregister_agent(agent_path)
        except Exception:
            pass
        bus.disconnect()
