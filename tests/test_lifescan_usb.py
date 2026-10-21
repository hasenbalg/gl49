"""LifeScan MSC USB protocol tests (framing, records — no hardware)."""
import struct

import pytest

from glucobridge.backends.lifescan_usb import (
    EPOCH_2000, UsbError, build_packet, parse_packet, parse_record)
from glucobridge.backends.verio import crc16_ccitt_false


def _resp(message: bytes) -> bytes:
    body = bytes([0x02, len(message) + 6, 0x00]) + message + b"\x03"
    return (body + struct.pack("<H", crc16_ccitt_false(body))).ljust(512, b"\x00")


def test_request_frames_match_spec():
    # spec examples: READ RTC len 9, READ RECORD COUNT len 9, READ RECORD len 12
    rtc = build_packet(bytes([0x03, 0x20, 0x02]))
    assert rtc[:3] == b"\x02\x09\x00" and rtc[3:6] == bytes([3, 0x20, 2])
    assert rtc[6] == 0x03
    cnt = build_packet(bytes([0x03, 0x27, 0x00]))
    assert cnt[1] == 9
    rec = build_packet(bytes([0x03, 0x31, 0x02, 0x00, 0x00, 0x00]))
    assert rec[1] == 12
    assert len(rtc) == 512


def test_packet_roundtrip():
    msg = bytes([0x03, 0x06, 0x08, 0x00])
    assert parse_packet(_resp(msg)) == msg[2:]


def test_parse_rejects_bad_crc():
    block = bytearray(_resp(bytes([0x03, 0x06, 0x01])))
    block[5] ^= 0xFF
    with pytest.raises(UsbError):
        parse_packet(bytes(block))


def test_parse_rejects_error_response():
    with pytest.raises(UsbError, match="error response"):
        parse_packet(_resp(bytes([0x03, 0x07, 0x01])))


def test_parse_record_fields():
    rec = struct.pack("<HBHIHB", 7, 0, 42, 829600000, 123, 1) + bytes(4)
    r = parse_record(rec)
    assert r.value == 123.0 and r.unit == "mg/dL"
    assert r.meal == "before"
    assert r.timestamp.year == 2026
    assert any("lifetime_counter_42" in s for s in r.status)


def test_parse_record_meal_flags():
    for flag, want in ((0, None), (1, "before"), (2, "after")):
        rec = struct.pack("<HBHIHB", 1, 0, 1, 0, 96, flag) + bytes(4)
        assert parse_record(rec).meal == want


def test_epoch():
    assert EPOCH_2000 == 946_684_800
