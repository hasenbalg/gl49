"""unpair(): BlueZ Device1.Remove() wiring, stubbed D-Bus."""
import pytest

from glucobridge.pairing import DEVICE_IFACE, unpair

DEV = "AA:BB:CC:DD:EE:FF"
DEV_PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


class _FakeIface:
    def __init__(self, log):
        self.log = log

    async def call_remove(self):
        self.log.append("remove")


class _FakeObj:
    def __init__(self, log):
        self.log = log

    def get_interface(self, name):
        assert name == DEVICE_IFACE
        return _FakeIface(self.log)


class _FakeManager:
    def __init__(self, objects):
        self.objects = objects

    async def call_get_managed_objects(self):
        return self.objects


class _FakeRoot(_FakeObj):
    def get_interface(self, name):
        return self.manager            # set by fixture


class _FakeBus:
    def __init__(self, objects, log):
        self.log = log

    async def connect(self):
        return self

    async def introspect(self, service, path):
        return object()

    def get_proxy_object(self, service, path, intro):
        if path == "/":
            return self.root
        assert path == DEV_PATH
        return _FakeObj(self.log)

    def disconnect(self):
        self.log.append("disconnect")


@pytest.fixture
def fake_bus(monkeypatch):
    import dbus_fast.aio

    def install(objects):
        log = []
        bus = _FakeBus(objects, log)
        root = _FakeRoot(log)
        root.manager = _FakeManager(objects)
        bus.root = root
        monkeypatch.setattr(dbus_fast.aio, "MessageBus",
                            lambda **kw: bus)
        return log
    return install


async def test_unpair_known_device(fake_bus):
    log = fake_bus({DEV_PATH: {DEVICE_IFACE: {"Paired": True}}})
    assert await unpair(DEV) is True
    assert log == ["remove", "disconnect"]


async def test_unpair_unknown_device_is_noop(fake_bus):
    log = fake_bus({})                 # BlueZ has no such device
    assert await unpair(DEV) is False  # idempotent
    assert log == ["disconnect"]       # Remove() never called


async def test_unpair_cached_but_not_a_device(fake_bus):
    # object exists but without Device1 (e.g. only the GATT service)
    log = fake_bus({DEV_PATH: {"org.bluez.GattService1": {}}})
    assert await unpair(DEV) is False
    assert log == ["disconnect"]
