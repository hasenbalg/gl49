"""End-to-end backend test against a simulated GLS meter (no hardware).

The fake client mimics a Beurer GL 49: on RACP 'report stored records / all'
it streams two measurements, one context record, then the success response.
"""
import asyncio
import struct
from types import SimpleNamespace

from glucobridge import gatt, racp
from glucobridge.backends.gls import GlucoseMeter
from glucobridge.records import parse_glucose_measurement  # noqa: F401


def base_time(y, mo, d, h, mi, s=0):
    return struct.pack("<H5B", y, mo, d, h, mi, s)


def measurement(seq, sfloat, flags=0x12, ts=None):
    return (bytes([flags]) + struct.pack("<H", seq)
            + (ts or base_time(2026, 10, 20, 8, 0, 0))
            + struct.pack("<H", sfloat) + bytes([0x11]))


def context(seq, meal):
    return bytes([0x02]) + struct.pack("<H", seq) + bytes([meal])


SF_98 = 0x0062
SF_141 = 0x008D


class _Svc:
    def __init__(self, uuid, char_uuids):
        self.uuid = uuid
        self.characteristics = [SimpleNamespace(uuid=u, handle=i)
                                for i, u in enumerate(char_uuids)]


class FakeClient:
    def __init__(self):
        self.is_connected = True
        self._cbs = {}
        self.services = [_Svc(gatt.GLUCOSE_SERVICE, [
            gatt.GLUCOSE_MEASUREMENT, gatt.GLUCOSE_MEASUREMENT_CONTEXT,
            gatt.RECORD_ACCESS_CONTROL_POINT])]

    async def start_notify(self, char, cb):
        self._cbs[char.uuid.lower()] = cb

    async def write_gatt_char(self, char, data, response=False):
        assert char.uuid == gatt.RECORD_ACCESS_CONTROL_POINT
        assert bytes(data) == racp.report_stored_all()
        notify = self._cbs[gatt.GLUCOSE_MEASUREMENT.lower()]
        ctx = self._cbs[gatt.GLUCOSE_MEASUREMENT_CONTEXT.lower()]
        racp_cb = self._cbs[gatt.RECORD_ACCESS_CONTROL_POINT.lower()]
        # records arrive asynchronously, like a real transfer
        asyncio.get_running_loop().call_soon(
            notify, 0, bytearray(measurement(1, SF_98)))
        asyncio.get_running_loop().call_soon(
            notify, 0, bytearray(measurement(2, SF_141)))
        asyncio.get_running_loop().call_soon(
            ctx, 0, bytearray(context(2, 0x01)))          # preprandial
        asyncio.get_running_loop().call_soon(
            racp_cb, 0, bytearray([0x06, 0x00, 0x01, 0x01]))  # success

    async def disconnect(self):
        self.is_connected = False


async def test_download_flow():
    meter = GlucoseMeter("AA:BB:CC:DD:EE:FF", name="GL 49")
    meter._client = FakeClient()
    readings = await meter.download(timeout=5)
    assert len(readings) == 2
    assert readings[0].value == 98.0
    assert readings[0].unit == "mg/dL"
    assert readings[1].value == 141.0
    assert readings[1].meal == "preprandial"   # merged from context
    assert readings[0].meal is None
    assert readings[0].device == "GL 49"
    assert all(r.timestamp is not None for r in readings)


async def test_download_empty_meter():
    meter = GlucoseMeter("AA:BB:CC:DD:EE:FF")

    class EmptyClient(FakeClient):
        async def write_gatt_char(self, uuid, data, response=False):
            racp_cb = self._cbs[gatt.RECORD_ACCESS_CONTROL_POINT.lower()]
            asyncio.get_running_loop().call_soon(
                racp_cb, 0, bytearray([0x06, 0x00, 0x01, 0x06]))  # no records

    meter._client = EmptyClient()
    assert await meter.download(timeout=5) == []
