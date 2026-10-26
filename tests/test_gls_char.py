"""Regression: duplicate characteristic UUIDs across services.

Live Accu-Chek Guide exposes the same characteristic UUID under more
than one service; bleak rejects bare UUID strings in that case
("Multiple Characteristics with this UUID"), so every lookup must go
through GlucoseMeter._char which prefers the standard Glucose Service.
"""
from types import SimpleNamespace

import pytest

from glucobridge import gatt
from glucobridge.backends.gls import GlucoseMeter

VENDOR = "0000ffe0-0000-1000-8000-00805f9b34fb"
RACP = "00002a52-0000-1000-8000-00805f9b34fb"
MEAS = "00002a18-0000-1000-8000-00805f9b34fb"


def _svc(uuid, char_uuids):
    chars = [SimpleNamespace(uuid=u, handle=i)
             for i, u in enumerate(char_uuids)]
    return SimpleNamespace(uuid=uuid, characteristics=chars)


def _meter(services):
    m = GlucoseMeter("AA:BB:CC:DD:EE:FF")
    m._client = SimpleNamespace(is_connected=True, services=services)
    return m


def test_prefers_glucose_service_when_uuid_is_duplicated():
    vendor = _svc(VENDOR, [RACP, MEAS])          # decoy, listed first
    glu = _svc(gatt.GLUCOSE_SERVICE, [MEAS, RACP])
    m = _meter([vendor, glu])
    assert m._char(gatt.RECORD_ACCESS_CONTROL_POINT) is glu.characteristics[1]
    assert m._char(gatt.GLUCOSE_MEASUREMENT) is glu.characteristics[0]


def test_falls_back_to_other_service_when_glucose_lacks_it():
    only = _svc(VENDOR, [RACP])
    m = _meter([only])
    assert m._char(gatt.RECORD_ACCESS_CONTROL_POINT) is only.characteristics[0]


def test_missing_char_raises_keyerror():
    m = _meter([_svc(gatt.GLUCOSE_SERVICE, [MEAS])])
    with pytest.raises(KeyError):
        m._char(gatt.RECORD_ACCESS_CONTROL_POINT)


def test_short_form_uuid_matches_long_form():
    glu = _svc(gatt.GLUCOSE_SERVICE, [RACP])
    m = _meter([glu])
    assert m._char("2a52") is glu.characteristics[0]
