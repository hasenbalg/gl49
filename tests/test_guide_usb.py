"""Accu-Chek Guide USB: BCD date decode + full session state machine
against canned PDUs (no hardware)."""
import struct
from datetime import datetime

import pytest

from glucobridge.backends import guide_usb as G


def _entry(dt=(0x20, 0x26, 0x10, 0x26, 0x09, 0x30), value=97, status=0):
    return bytes(dt) + b"\x00\x00" + struct.pack(">HH", value, status)


def test_bcd_decode():
    assert G._bcd(0x26) == 26
    assert G._bcd(0x09) == 9
    assert G._bcd(0x99) == 99


def test_parse_sample_normal():
    rd = G.parse_sample(_entry(), "173a:21d5")
    assert rd.timestamp == datetime(2026, 10, 26, 9, 30)
    assert rd.value == 97.0
    assert rd.unit == "mg/dL"
    assert rd.error is None


def test_parse_sample_error_status():
    rd = G.parse_sample(_entry(status=0x02), "x")
    assert rd.error == "status_0x0002"


def _cfg_report(invoke=1, handle=9):
    hdr = bytearray(24)
    hdr[6:8] = struct.pack(">H", invoke)
    objs = struct.pack(">HHHH", 1, G.MDC_PMSTORE, 0, 0) + bytes(8)
    return bytes(hdr) + objs


def _resp(invoke):
    buf = bytearray(8)
    buf[0:2] = struct.pack(">H", G.APDU_PRESENTATION)
    buf[6:8] = struct.pack(">H", invoke)
    return bytes(buf)


def _seg_header(invoke=4):
    buf = bytearray(24)
    buf[6:8] = struct.pack(">H", invoke)
    buf[14:16] = struct.pack(">H", G.ACT_SEG_TRIG_XFER)
    buf[20:22] = struct.pack(">H", 0)
    return bytes(buf)


def _data_segment(entries, last=True, invoke=5):
    n = len(entries)
    buf = bytearray(36 + 12 * n)
    buf[6:8] = struct.pack(">H", invoke)
    buf[22:26] = struct.pack(">I", 0xAABBCCDD)
    buf[26:30] = struct.pack(">I", 0x11223344)
    buf[30:32] = struct.pack(">H", n)
    buf[32] = 0x40 if last else 0x00
    for i, e in enumerate(entries):
        buf[36 + 12 * i: 48 + 12 * i] = e[:12]
    return bytes(buf)


def _meter(monkeypatch, queue):
    m = G.GuideUsbMeter()
    m.id = "test"
    sent = []
    m._in = lambda name, size=1024: queue.pop(0)
    m._out = lambda pdu, name: sent.append(bytes(pdu))
    return m, sent


def test_download_state_machine():
    queue = [bytes(8),               # pairing request
             _cfg_report(),
             _resp(2),               # MDS attr answer
             _resp(3),               # seg get info answer
             _seg_header(),
             _data_segment([_entry(), _entry(value=141)]),
             bytes(8)]               # release confirmation
    m, sent = _meter(None, queue)
    readings = m.download()
    assert [r.value for r in readings] == [97.0, 141.0]
    assert all(r.timestamp == datetime(2026, 10, 26, 9, 30)
               for r in readings)
    # association response must name proto 20601
    assert struct.unpack(">H", sent[0][6:8])[0] == 20601
    # segment ACK must echo the segment identifiers we received
    ack = sent[5]
    assert ack[0:2] == struct.pack(">H", G.APDU_PRESENTATION)
    assert struct.unpack(">I", ack[14:18])[0] == 0xFFFFFFFF  # rel time
    assert struct.unpack(">I", ack[22:26])[0] == 0xAABBCCDD
    assert struct.unpack(">I", ack[26:30])[0] == 0x11223344
    # clean release at the end
    assert sent[-1][0:2] == struct.pack(">H", G.APDU_ASSOC_RELEASE)


def test_download_multi_segment():
    queue = [bytes(8), _cfg_report(handle=7), _resp(2), _resp(3),
             _seg_header(),
             _data_segment([_entry(value=97)], last=False),
             _data_segment([_entry(value=141)], last=True),
             bytes(8)]
    m, _ = _meter(None, queue)
    readings = m.download()
    assert [r.value for r in readings] == [97.0, 141.0]


def test_refused_transfer():
    buf = bytearray(22)
    buf[6:8] = struct.pack(">H", 4)
    buf[14:16] = struct.pack(">H", G.ACT_SEG_TRIG_XFER)
    buf[20:22] = struct.pack(">H", 3)     # memory empty
    queue = [bytes(8), _cfg_report(), _resp(2), _resp(3), bytes(buf)]
    m, _ = _meter(None, queue)
    with pytest.raises(G.UsbError, match="memory empty"):
        m.download()
