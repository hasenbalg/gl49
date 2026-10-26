"""Advertisement classification tests (backend routing)."""
from glucobridge.client import classify

GSS = "00001808-0000-1000-8000-00805f9b34fb"
FORA = "0000fff0-0000-1000-8000-00805f9b34fb"
VERIO = "af9df7a1-e595-11e3-96b4-0002a5d5c51b"


def test_gl49_advertises_without_space():
    # live capture 2026-10-26: BlueZ shows plain "GL49"
    assert classify("GL49", []) == "gls"
    assert classify("GL 49", []) == "gls"


def test_name_patterns():
    assert classify("OneTouch CC91", [GSS]) == "verio"   # decoy GSS + name
    assert classify("GlucoCheck ADVANCE", [GSS]) == "gls"
    assert classify("TD4149", []) == "fora"
    assert classify("Beurer GL 60", []) == "gls"


def test_service_hints_when_name_unknown():
    assert classify("xyz", [GSS]) == "gls"
    assert classify("xyz", [FORA]) == "fora"
    assert classify("xyz", [VERIO, GSS]) == "verio"      # verio beats decoy
    assert classify("", []) == "unknown"


class _Variant:  # mimics dbus_fast.signature.Variant
    __slots__ = ("signature", "value")

    def __init__(self, signature, value):
        self.signature = signature
        self.value = value


def test_bus_cache_payload_unwraps_variants():
    from glucobridge.client import _bus_devices_to_advs
    objects = {
        "/org/bluez/hci0/dev_C0_26_DA_31_E6_1D": {
            "org.bluez.Device1": {
                "Address": _Variant("s", "C0:26:DA:31:E6:1D"),
                "Name": _Variant("s", "GL49"),
                "UUIDs": _Variant("as", ["00001808-0000-1000-8000-00805f9b34fb"]),
                "RSSI": _Variant("n", -60),
            },
        },
        "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
    }
    advs = _bus_devices_to_advs(objects)
    assert len(advs) == 1
    a = advs[0]
    assert a.address == "C0:26:DA:31:E6:1D"     # not Variant repr garbage
    assert a.name == "GL49"
    assert a.rssi == -60
    assert a.has_glucose_service is True
    assert a.backend == "gls"
