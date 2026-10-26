"""GL49 USB (CP2110 / FORA frame) protocol tests — no hardware.

Record fixtures are the live-verified GL49 download of 2026-10-22
(values cross-checked against the meter's own display).
"""
import pytest

from glucobridge.backends.gl49_usb import (
    frame,
    parse_count,
    parse_device_id,
    parse_record,
    reply_valid,
    swap_pairs,
)


def _resp(cmd: int, payload: bytes) -> bytes:
    """Build a checksummed response: 51 cmd <payload> A5 CK."""
    body = bytes([0x51, cmd]) + payload + b"\xa5"
    return body + bytes([sum(body) & 0xFF])


# ---------------------------------------------------------------- framing
def test_frame_checksum():
    assert frame(0x2B) == bytes.fromhex("512b00000000a31f")


def test_frame_fetch():
    fr = frame(0x85, 1, 0)
    assert fr[:2] == b"\x51\x85" and fr[2] == 1
    assert fr[-2] == 0xA3 and fr[-1] == sum(fr[:-1]) & 0xFF


def test_swap_pairs():
    assert swap_pairs(bytes.fromhex("55350a16")) == bytes.fromhex("3555160a")


def test_reply_valid():
    r = _resp(0x2B, b"\x03\x00\x02\x00")
    assert reply_valid(r, 0x2B)
    assert not reply_valid(r, 0x85)
    bad = bytearray(r)
    bad[-1] ^= 0xFF
    assert not reply_valid(bytes(bad), 0x2B)


def test_parse_count():
    # live: 51 2b 03 00 02 00 a5 26 -> 3 records
    assert parse_count(bytes.fromhex("512b03000200a526")) == 3


def test_parse_device_id():
    # live: 51 24 49 41 02 00 a5 a6 -> FORA TD4149 ("IA")
    d = parse_device_id(bytes.fromhex("512449410200a5a6"))
    assert d["code"] == "0x4149"
    assert d["model"] == "FORA TD4149 (Beurer GL 49)"


# ---------------------------------------------------------------- records
LIVE_RECORDS = [
    ("5a35352ec288", (2026, 10, 26, 14, 53, 194), "preprandial"),  # apple once
    ("55350a167990", (2026, 10, 21, 22, 10, 121), None),
    ("55351308a080", (2026, 10, 21, 8, 19, 160), None),
    ("543500077a80", (2026, 10, 20, 7, 0, 122), None),
]


@pytest.mark.parametrize("hexrec,expect,meal", LIVE_RECORDS)
def test_parse_live_records(hexrec, expect, meal):
    r = parse_record(bytes.fromhex(hexrec))
    y, mo, d, h, mi, v = expect
    assert r is not None
    assert r.timestamp.year == y and r.timestamp.month == mo
    assert r.timestamp.day == d
    assert r.timestamp.hour == h and r.timestamp.minute == mi
    assert r.value == float(v) and r.unit == "mg/dL"
    assert r.meal == meal


def test_meal_marks():
    # mark bits = swapped r2 >> 5; craft hour 10 + mark m: r2 = 10 | (m<<5)
    # wire order: swapped [r0,r1,r2,r3,r4,r5] -> wire [r1,r0,r3,r2,r5,r4]
    r0, r1, r3, r4, r5 = 0x34, 0x45, 0x0B, 0x00, 0x50   # 2026-02-05 10:11
    for mark, want in ((0, None), (1, "preprandial"), (2, "postprandial"),
                       (3, "general")):
        r2 = 10 | (mark << 5)     # hour 10
        wire = bytes([r1, r0, r3, r2, r5, r4])
        r = parse_record(wire)
        assert r is not None and r.meal == want, mark


def test_parse_record_blank_and_invalid():
    assert parse_record(bytes(6)) is None
    assert parse_record(bytes.fromhex("000000000000")) is None
    # swapped r1=E5 -> day 5, month (7|8)=15: invalid month
    assert parse_record(bytes.fromhex("e50100000001")) is None
    assert parse_record(bytes.fromhex("5535")) is None           # too short


def test_parse_record_value_high_bits():
    # value = r5 | ((r4 & 3) << 8) on swapped bytes. Wire 2A 45 00 0C 05 01
    # swaps to r0=45 r1=2A r2=0C r3=00 r4=01 r5=05:
    #   value 0x105=261, year 2000+(0x45>>1)=2034, month (1|8)=9, day 10,
    #   12:00
    r = parse_record(bytes([0x2A, 0x45, 0x00, 0x0C, 0x05, 0x01]))
    assert r is not None and r.value == 261.0
    assert r.timestamp.year == 2034 and r.timestamp.month == 9
    assert r.timestamp.day == 10 and r.timestamp.hour == 12
