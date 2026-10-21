"""FORA protocol tests: frame codec + simulated meter download.

Vectors derived from the reverse-engineered spec (FINDINGS-fora.md).
"""
import asyncio
import datetime as dt

import pytest

from glucobridge.backends import fora
from glucobridge.backends.fora import (
    frame, checksum_ok, parse_date, encode_time, parse_value,
    parse_device_id, parse_count, ForaMeter,
)


def resp(cmd, b2=0, b3=0, b4=0, b5=0):
    return frame(cmd, b2, b3, b4, b5)


# ------------------------------------------------------------------- codec
def test_frame_shape_and_checksum():
    f = frame(0x24)
    assert f[:7] == bytes([0x51, 0x24, 0, 0, 0, 0, 0xA3])
    assert f[7] == sum(f[:7]) & 0xFF
    assert checksum_ok(f)


def test_checksum_rejects_corruption():
    f = bytearray(frame(0x24))
    f[3] ^= 0xFF
    assert not checksum_ok(bytes(f))


def test_time_sync_roundtrip():
    when = dt.datetime(2026, 10, 20, 20, 15)
    f = frame(0x33, *encode_time(when))
    parsed, transmitted = parse_date(f)
    assert parsed == when
    assert not transmitted


def test_date_decode_transmitted_flag():
    when = dt.datetime(2026, 1, 1, 0, 0)
    f = frame(0x33, *encode_time(when))
    f = bytearray(f)
    f[5] |= 0x40                      # transmitted bit
    parsed, transmitted = parse_date(bytes(f))
    assert transmitted
    assert parsed == when


def test_device_id():
    b = resp(0x24, 0x40, 0x42, 0, 0)  # b2=0x40 b3=0x42 -> "4240"
    info = parse_device_id(b)
    assert info["model"] == "TD4240"
    assert info["revision"] == chr(64)


def test_count():
    b = resp(0x2B, 0x02, 0x01, 0x00, 0x00)
    assert parse_count(b) == (0x0102, 0)


def test_value_integer_model():
    b = resp(0x26, 105, 0, 25, 0x40)  # 105 mg/dL, AC marker
    r = parse_value(b, decimal=False)
    assert r.value == 105.0
    assert r.unit == "mg/dL"
    assert r.meal == "preprandial"
    assert r.error is None


def test_value_decimal_model():
    b = resp(0x26, 0x1A, 0x04, 25, 0x00)   # 1050 * 0.1 = 105.0
    r = parse_value(b, decimal=True)
    assert r.value == pytest.approx(105.0)
    assert r.meal is None


def test_value_qc_and_error():
    b = resp(0x26, 0x62, 0x00, 25, 0xC0)   # QC marker
    r = parse_value(b, decimal=False)
    assert r.sample_type == "control_solution"
    assert r.meal is None
    b = resp(0x26, 0, 0, 25, 0x05)         # error code 5
    r = parse_value(b, decimal=False)
    assert r.error == "meter_code_5"


# --------------------------------------------------------- simulated meter
class FakeForaClient:
    """Simulates a TD4240 (decimal model) with two stored records."""

    def __init__(self):
        self.is_connected = True
        self._cb = None

    async def start_notify(self, uuid, cb):
        assert uuid == fora.FORA_CHAR
        self._cb = cb

    def _reply(self, data):
        asyncio.get_running_loop().call_soon(self._cb, 0, bytearray(data))

    async def write_gatt_char(self, uuid, data, response=False):
        f = bytes(data)
        assert checksum_ok(f), f"bad frame from library: {f.hex()}"
        cmd = f[1]
        if cmd == 0x24:
            self._reply(resp(0x24, 0x40, 0x42, 0, 0))       # TD4240
        elif cmd == 0x2B:
            self._reply(resp(0x2B, 2, 0, 0, 0))             # 2 records
        elif cmd == 0x25:
            idx = f[2]
            when = dt.datetime(2026, 10, 20, 8, 30) if idx == 0 \
                else dt.datetime(2026, 10, 20, 20, 15)
            self._reply(frame(0x25, *encode_time(when)))
        elif cmd == 0x26:
            idx = f[2]
            raw = 1050 if idx == 0 else 1410
            flags = 0x40 if idx == 0 else 0x00              # AC / general
            self._reply(resp(0x26, raw & 0xFF, raw >> 8, 25, flags))
        elif cmd == 0x50:
            pass                                             # reset, no reply
        else:
            pytest.fail(f"unexpected CMD 0x{cmd:02X}")

    async def disconnect(self):
        self.is_connected = False


async def test_fora_download_flow():
    meter = ForaMeter("AA:BB:CC:DD:EE:FF", name="GlucoCheck Advance")
    meter._client = FakeForaClient()
    await meter.connect()          # subscribes notifications on the fake
    info = await meter.identify()
    assert info["model"] == "TD4240"
    assert meter.decimal

    readings = await meter.download(timeout=10)
    assert len(readings) == 2
    assert readings[0].value == pytest.approx(105.0)
    assert readings[0].meal == "preprandial"
    assert readings[0].timestamp == dt.datetime(2026, 10, 20, 8, 30)
    assert readings[1].value == pytest.approx(141.0)
    assert readings[1].meal is None
    assert readings[1].timestamp == dt.datetime(2026, 10, 20, 20, 15)
    assert all(r.device == "GlucoCheck Advance" for r in readings)
