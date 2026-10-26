"""TD-42xx decode fixtures — live capture from a GlucoCheck Advance
(TD-4288) on 2026-10-26; readings cross-checked against its BLE GSS
history (6 records, identical timestamps/values)."""
import struct
from datetime import datetime

from glucobridge.backends.glucocheck_usb import (
    Td42xxUsbMeter, frame4, parse_count, parse_datetime, parse_model,
    parse_value)


def test_frame_checksum():
    fr = frame4(0x22)
    assert fr[:2] == bytes([0x51, 0x22])
    assert fr[6] == 0xA3
    assert fr[7] == sum(fr[:7]) & 0xFF


def test_parse_model():
    assert parse_model(bytes.fromhex("88420600")) == "TD-4288"


def test_parse_datetime_bitfield():
    # live record 0: day u16le 0x343b -> 2026-01-27, 12:12
    assert parse_datetime(bytes.fromhex("3b340c0c")) == \
        datetime(2026, 1, 27, 12, 12)
    assert parse_datetime(bytes.fromhex("3a34180d")) == \
        datetime(2026, 1, 26, 13, 24)


def test_parse_value_and_meal_flags():
    assert parse_value(bytes.fromhex("88000600")) == (136.0, None)
    assert parse_value(bytes.fromhex("7c000640")) == (124.0, "preprandial")
    assert parse_value(bytes.fromhex("7c000680")) == (124.0, "postprandial")


def test_parse_count():
    assert parse_count(bytes.fromhex("06000500")) == 6


CAPTURE = {
    (0x2B, 0): "512b06000500a52c",
    (0x25, 0): "51253b340c0ca5a2",
    (0x26, 0): "512688000600a5aa",
    (0x25, 1): "51253a34180da5ae",
    (0x26, 1): "51267c000600a59e",
}


class _Stub(Td42xxUsbMeter):
    def __init__(self):
        self.h = None

    def cmd(self, fr, expect_cmd, min_len=8, retries=1):
        idx = struct.unpack("<H", fr[2:4])[0]
        canned = CAPTURE.get((expect_cmd, idx if expect_cmd in (0x25, 0x26)
                              else 0))
        return bytes.fromhex(canned) if canned else None


def test_download_live_capture():
    readings = _Stub().download()
    assert [r.value for r in readings] == [124.0, 136.0]      # oldest first
    assert readings[0].timestamp == datetime(2026, 1, 26, 13, 24)
    assert readings[1].timestamp == datetime(2026, 1, 27, 12, 12)
    assert all(r.unit == "mg/dL" and r.meal is None for r in readings)
