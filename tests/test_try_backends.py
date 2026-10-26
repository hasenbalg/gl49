"""try_backends must survive decoy services: a backend that CONNECTS but
whose operation fails (Reflect's dead Glucose Service -> GATT error 14)
falls through to the next candidate."""
import pytest

from glucobridge import client as C


class FakeMeter:
    fail_cls = ()          # class names whose download raises
    constructed = []

    def __init__(self, target):
        self.target = target
        FakeMeter.constructed.append(type(self).__name__)

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def download(self, timeout=60.0):
        if type(self).__name__ in FakeMeter.fail_cls:
            raise RuntimeError("GATT Protocol Error: Unlikely Error")
        return ["ok"]


@pytest.fixture
def fakes(monkeypatch):
    FakeMeter.constructed = []
    FakeMeter.fail_cls = ()
    for name in ("GlucoseMeter", "ForaMeter", "VerioMeter"):
        cls = type(name, (FakeMeter,), {})
        monkeypatch.setattr(C, name, cls)
    return


async def test_decoy_backend_falls_through(monkeypatch, fakes):
    FakeMeter.fail_cls = ("GlucoseMeter",)
    monkeypatch.setattr(C.GlucoBridge, "_resolve_backend",
                        lambda self, a: _ret("gls"))
    async with C.GlucoBridge() as gb:
        got = await gb.download("AA:BB:CC:DD:EE:FF")
    assert got == ["ok"]
    assert FakeMeter.constructed[:2] == ["GlucoseMeter", "ForaMeter"]


async def test_explicit_backend_no_fallback(monkeypatch, fakes):
    FakeMeter.fail_cls = ("GlucoseMeter",)
    async with C.GlucoBridge() as gb:
        with pytest.raises(RuntimeError):
            await gb.download("AA:BB:CC:DD:EE:FF", backend="gls")
    assert FakeMeter.constructed == ["GlucoseMeter"]


async def test_classified_order_first(monkeypatch, fakes):
    monkeypatch.setattr(C.GlucoBridge, "_resolve_backend",
                        lambda self, a: _ret("verio"))
    async with C.GlucoBridge() as gb:
        got = await gb.download("AA:BB:CC:DD:EE:FF")
    assert got == ["ok"]
    assert FakeMeter.constructed == ["VerioMeter"]


def _ret(v):
    async def coro(*a, **k):
        return v
    return coro()
