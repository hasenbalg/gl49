"""unpair(): BlueZ bond removal wiring, stubbed D-Bus.

Covers both transports of the removal: Device1.Remove() when the
generated proxy exposes it, and the Adapter1.RemoveDevice() fallback
needed by some BlueZ/dbus-fast combos (seen live: 'ProxyInterface'
object has no attribute 'call_remove')."""
import pytest

from glucobridge.pairing import DEVICE_IFACE, unpair

DEV = "AA:BB:CC:DD:EE:FF"
DEV_PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
ADAPTER_PATH = "/org/bluez/hci0"


class _DevIface:
    def __init__(self, log, with_remove):
        self.log = log
        if with_remove:                    # emulate the two variants
            async def call_remove():
                log.append("remove")
            self.call_remove = call_remove

    def __getattr__(self, name):           # variant without Remove()
        raise AttributeError(name)


class _AdapterIface:
    def __init__(self, log):
        self.log = log

    async def call_remove_device(self, path):
        assert path == DEV_PATH
        self.log.append("remove_device")


class _Obj:
    def __init__(self, iface_name, iface):
        self.iface_name = iface_name
        self.iface = iface

    def get_interface(self, name):
        assert name == self.iface_name
        return self.iface


class _Manager:
    def __init__(self, objects):
        self.objects = objects

    async def call_get_managed_objects(self):
        return self.objects


class _Root:
    def get_interface(self, name):
        return self.manager                # set by fixture


class _Bus:
    def __init__(self, objects, log, with_remove):
        self.log = log
        self.with_remove = with_remove
        self.root = None

    async def connect(self):
        return self

    async def introspect(self, service, path):
        return object()

    def get_proxy_object(self, service, path, intro):
        if path == "/":
            return self.root
        if path == ADAPTER_PATH:
            return _Obj("org.bluez.Adapter1", _AdapterIface(self.log))
        assert path == DEV_PATH
        return _Obj(DEVICE_IFACE, _DevIface(self.log, self.with_remove))

    def disconnect(self):
        self.log.append("disconnect")


@pytest.fixture
def fake_bus(monkeypatch):
    import dbus_fast.aio

    def install(objects, with_remove=True):
        log = []
        bus = _Bus(objects, log, with_remove)
        root = _Root()
        root.manager = _Manager(objects)
        bus.root = root
        monkeypatch.setattr(dbus_fast.aio, "MessageBus",
                            lambda **kw: bus)
        return log
    return install


async def test_unpair_known_device(fake_bus):
    log = fake_bus({DEV_PATH: {DEVICE_IFACE: {"Paired": True}}})
    assert await unpair(DEV) is True
    assert log == ["remove", "disconnect"]


async def test_unpair_falls_back_to_remove_device(fake_bus):
    # the live failure mode: Device1 proxy without call_remove
    log = fake_bus({DEV_PATH: {DEVICE_IFACE: {"Paired": True}}},
                   with_remove=False)
    assert await unpair(DEV) is True
    assert log == ["remove_device", "disconnect"]


async def test_unpair_unknown_device_is_noop(fake_bus):
    log = fake_bus({})                 # BlueZ has no such device
    assert await unpair(DEV) is False  # idempotent
    assert log == ["disconnect"]       # no removal attempted


async def test_unpair_cached_but_not_a_device(fake_bus):
    # object exists but without Device1 (e.g. only the GATT service)
    log = fake_bus({DEV_PATH: {"org.bluez.GattService1": {}}})
    assert await unpair(DEV) is False
    assert log == ["disconnect"]
