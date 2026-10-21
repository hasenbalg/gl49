"""Verio protocol tests: CRC vectors, framing, record decode, simulated download."""
import asyncio
import datetime as dt
import struct

import pytest

from glucobridge.backends import verio
from glucobridge.backends.verio import (
    crc16_ccitt_false, build_request, parse_response, decode_record,
    VerioMeter, VerioError, TIME_OFFSET, ACK,
)


def crc16_bytes(data: bytes) -> bytes:
    c = crc16_ccitt_false(data)
    return bytes([c & 0xFF, c >> 8])


def meter_response(payload: bytes, rsp_type: int = 0x06) -> bytes:
    """Build a meter->host frame the way the meter does."""
    body = bytes([0x02, 0, 0x00, 0x03, rsp_type]) + payload + bytes([0x03])
    body = bytearray(body)
    body[1] = len(body) + 2          # len field covers everything after 0x01
    frame = bytes([0x01]) + bytes(body) + crc16_bytes(bytes(body))
    assert frame[2] == len(frame) - 1
    return frame


# ------------------------------------------------------------------- CRC16
def test_crc_standard_check_value():
    # CRC-16/CCITT-FALSE check value for "123456789" is 0x29B1
    assert crc16_ccitt_false(b"123456789") == 0x29B1


# ------------------------------------------------------------------ framing
def test_build_request_time_cmd():
    f = build_request(bytes([0x20, 0x02]))
    assert f[0] == 0x01 and f[1] == 0x02
    assert f[2] == len(bytes([0x20, 0x02])) + 7 == 9
    assert f[3] == 0x00 and f[4] == 0x03
    assert f[5:7] == bytes([0x20, 0x02])
    assert f[7] == 0x03
    assert f[8:] == crc16_bytes(f[1:8])
    assert len(f) == 10


def test_parse_ack():
    assert parse_response(bytes([0x81])) == ("ack", b"")


def test_parse_data_frame():
    kind, data = parse_response(meter_response(b"\x2a\x00"))
    assert kind == "data"
    assert data == b"\x2a\x00"


def test_parse_error_frame():
    kind, data = parse_response(meter_response(b"\x03", rsp_type=0x07))
    assert kind == "error"
    assert data == b"command_not_allowed"


def test_parse_rejects_bad_crc():
    good = bytearray(meter_response(b"\x01\x02\x03\x04"))
    good[-1] ^= 0xFF
    with pytest.raises(VerioError):
        parse_response(bytes(good))


def test_parse_rejects_bad_length():
    good = bytearray(meter_response(b"\x01"))
    good[2] = 99
    with pytest.raises(VerioError):
        parse_response(bytes(good))


# ------------------------------------------------------------------ records
def test_decode_record_plain():
    secs = 840000000                       # some meter-clock seconds
    data = struct.pack("<Ih", secs, 118) + bytes(5)
    r = decode_record(data)
    assert r.value == 118.0
    assert r.unit == "mg/dL"
    assert r.timestamp == dt.datetime.fromtimestamp(
        secs + TIME_OFFSET, dt.timezone.utc).replace(tzinfo=None)
    assert not any(s.startswith("mentor_") for s in r.status)


def test_decode_record_reflect_metadata():
    # Reflect: bytes 6-10 carry non-zero Blood Sugar Mentor metadata
    secs = 840000000
    data = struct.pack("<Ih", secs, 96) + bytes([0x00, 0x02, 0x01, 0x00, 0x00])
    r = decode_record(data)
    assert r.value == 96.0
    assert any(s.startswith("mentor_metadata_") for s in r.status)


def test_live_reflect_error_slot_and_valid_vectors():
    # live capture, Ultra Plus Reflect fw SI_04.01.03
    valid = decode_record(bytes.fromhex("b8f660327b000001000000"))
    assert valid.value == 123.0 and valid.error is None
    err = decode_record(bytes.fromhex("45d9613200000000000203"))
    assert err.value is None and err.error == "E-3"


def test_decode_record_wrong_length():
    with pytest.raises(VerioError):
        decode_record(bytes(10))


# --------------------------------------------------------- simulated meter
class FakeVerioClient:
    """Simulates an Ultra Plus Reflect: 2 records, ack-then-data flow."""

    def __init__(self):
        self.is_connected = True
        self._cb = None
        self.meter_now = 1792500000          # unix seconds (~Oct 2026)
        self.records = {
            41: (845815201, 118, bytes([0, 0x02, 1, 0, 0])),   # mentor set
            42: (845818801, 96, bytes(5)),
        }

    async def start_notify(self, uuid, cb):
        assert uuid == verio.VERIO_NOTIFY
        self._cb = cb

    def _send(self, frame):
        asyncio.get_running_loop().call_soon(self._cb, 0, bytearray(frame))

    async def write_gatt_char(self, uuid, data, response=False):
        data = bytes(data)
        if uuid == verio.VERIO_WRITE:
            if data == bytes([ACK]):
                return                          # host ack of our data
            assert data[0] == 0x01 and data[1] == 0x02
            payload = data[5:-3]
            self._send(bytes([ACK]))            # command ack
            if payload[:2] == bytes([0x20, 0x02]):
                self._send(meter_response(
                    struct.pack("<I", self.meter_now - TIME_OFFSET)))
            elif payload[:2] == bytes([0x27, 0x00]):
                self._send(meter_response(struct.pack("<I", 42)))
            elif payload[:3] == bytes([0x0A, 0x02, 0x06]):
                self._send(meter_response(struct.pack("<h", 2)))
            elif payload[0] == 0xB3:
                num = payload[1] | payload[2] << 8
                secs, mgdl, meta = self.records[num]
                self._send(meter_response(
                    struct.pack("<Ih", secs, mgdl) + meta))
            else:
                self._send(meter_response(b"\x03", rsp_type=0x09))

    async def disconnect(self):
        self.is_connected = False


async def test_verio_download_flow():
    meter = VerioMeter("AA:BB:CC:DD:EE:FF", name="OneTouch Ultra Plus Reflect")
    meter._client = FakeVerioClient()
    await meter.connect()

    readings = await meter.download(timeout=15)
    assert len(readings) == 2
    assert readings[0].value == 118.0
    assert readings[0].sequence == 41
    assert readings[1].value == 96.0
    assert readings[1].sequence == 42
    assert readings[0].timestamp < readings[1].timestamp
    assert all(r.device == "OneTouch Ultra Plus Reflect" for r in readings)
    assert any(s.startswith("mentor_metadata_") for s in readings[0].status)


async def test_verio_time_query():
    meter = VerioMeter("AA:BB:CC:DD:EE:FF")
    meter._client = FakeVerioClient()
    await meter.connect()
    when = await meter.sync_time()
    assert when.year >= 2026
