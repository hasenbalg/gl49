"""Record parsing tests with vectors built from the Bluetooth SIG GSS YAML
(org.bluetooth.characteristic.glucose_measurement / _context)."""
import datetime as dt
import struct

import pytest

from glucobridge.records import (
    parse_glucose_measurement, parse_glucose_context, merge_context,
    RecordError,
)


def base_time(year=2026, month=10, day=20, hour=21, minute=30, sec=0):
    return struct.pack("<H5B", year, month, day, hour, minute, sec)


def sfloat(raw):
    return struct.pack("<H", raw)


SF_105 = 0x0069   # 105 * 10^0
SF_5_5 = 0xF037   # 55 * 10^-1
SF_45 = 0x002D    # 45 * 10^0


# ---------------------------------------------------------------- 0x2A18
def test_measurement_mgdl():
    # flags: bit1 conc+type present, mg/dL
    data = bytes([0x02]) + struct.pack("<H", 42) + base_time() \
        + sfloat(SF_105) + bytes([0x11])   # type=1 capillary whole blood, loc=1 finger
    r = parse_glucose_measurement(data)
    assert r.value == 105.0
    assert r.unit == "mg/dL"
    assert r.mgdl == 105.0
    assert r.sequence == 42
    assert r.timestamp == dt.datetime(2026, 10, 20, 21, 30, 0)
    assert r.sample_type == "capillary_whole_blood"
    assert r.sample_location == "finger"
    assert r.error is None


def test_measurement_mmoll():
    # flags: bit1 + bit2 (mmol/L)
    data = bytes([0x06]) + struct.pack("<H", 7) + base_time() \
        + sfloat(SF_5_5) + bytes([0xF1])
    r = parse_glucose_measurement(data)
    assert r.unit == "mmol/L"
    assert r.value == pytest.approx(5.5)
    assert r.mgdl == pytest.approx(5.5 * 18.0182, rel=1e-3)


def test_measurement_with_time_offset():
    # flags bit0 (offset present, int16 minutes) + bit1
    data = bytes([0x03]) + struct.pack("<H", 1) + base_time() \
        + struct.pack("<h", -60) + sfloat(SF_105) + bytes([0x11])
    r = parse_glucose_measurement(data)
    assert r.timestamp == dt.datetime(2026, 10, 20, 20, 30, 0)
    assert r.value == 105.0


def test_measurement_with_status():
    # flags bit1 + bit3 (status present); battery_low set
    data = bytes([0x0A]) + struct.pack("<H", 2) + base_time() \
        + sfloat(SF_105) + bytes([0x11]) + struct.pack("<H", 0x0001)
    r = parse_glucose_measurement(data)
    assert "battery_low" in r.status
    assert r.value == 105.0


def test_measurement_context_flag():
    data = bytes([0x12]) + struct.pack("<H", 3) + base_time() \
        + sfloat(SF_105) + bytes([0x11])
    r = parse_glucose_measurement(data)
    assert r.value == 105.0  # bit4 = context available; parses fine


def test_measurement_too_short():
    with pytest.raises(RecordError):
        parse_glucose_measurement(b"\x02\x00")


def test_legacy_layout_fallback():
    # Pre-GSS: flags bit0 = 4-byte sint32 offset, concentration always
    # present with unit bit15.  flags=0x01 -> offset present, no status.
    data = bytes([0x01]) + struct.pack("<H", 9) + base_time() \
        + struct.pack("<i", 0) + sfloat(SF_105)
    r = parse_glucose_measurement(data)
    assert r.value == 105.0
    assert r.unit == "mg/dL"
    assert r.timestamp == dt.datetime(2026, 10, 20, 21, 30, 0)


def test_legacy_with_status_and_location():
    # flags: bit0 offset(4B), bit1 status, bits2-3 location=3 (finger)
    data = bytes([0x0F]) + struct.pack("<H", 9) + base_time() \
        + struct.pack("<i", 0) + sfloat(SF_105) + struct.pack("<H", 0x0002)
    r = parse_glucose_measurement(data)
    assert r.value == 105.0
    assert r.sample_location == "finger"
    assert "sensor_malfunction" in r.status


# ---------------------------------------------------------------- 0x2A34
def test_context_meal_and_carbs():
    # flags: bit0 carbs, bit1 meal
    data = bytes([0x03]) + struct.pack("<H", 42) \
        + bytes([0x00]) + sfloat(SF_45) + bytes([0x01])  # carb id 0, 45 g, preprandial
    c = parse_glucose_context(data)
    assert c.sequence == 42
    assert c.carbs_g == pytest.approx(45.0)
    assert c.meal == "preprandial"


def test_context_meal_only():
    data = bytes([0x02]) + struct.pack("<H", 8) + bytes([0x02])
    c = parse_glucose_context(data)
    assert c.meal == "postprandial"


def test_merge_context():
    mdata = bytes([0x12]) + struct.pack("<H", 42) + base_time() \
        + sfloat(SF_105) + bytes([0x11])
    m = parse_glucose_measurement(mdata)
    c = parse_glucose_context(bytes([0x02]) + struct.pack("<H", 42) + bytes([0x03]))
    merge_context(m, c)
    assert m.meal == "fasting"
    assert m.value == 105.0


def test_context_truncated():
    with pytest.raises(RecordError):
        parse_glucose_context(bytes([0x01, 0x00, 0x00]))  # carbs promised, absent


def test_live_glucocheck_advance_si_units():
    """Captured live from a TESTAmed GlucoCheck Advance (2026-10-21).

    The meter encodes the formal GSS SI unit (kg/L): SFLOAT 0xb07c =
    124e-5 kg/L = 124 mg/dL.  Must not be reported as 0.00124 mg/dL.
    """
    rec = bytes.fromhex("030000ea0701140f000000007cb011")
    r = parse_glucose_measurement(rec)
    assert r.value == pytest.approx(124.0)
    assert r.unit == "mg/dL"
    assert r.timestamp == dt.datetime(2026, 1, 20, 15, 0, 0)
    assert r.sequence == 0
    assert r.sample_type == "capillary_whole_blood"
    assert r.sample_location == "finger"
    assert "si_units_kg_per_l" in r.status
    assert r.error is None


def test_si_units_mol_per_l():
    """mmol/L variant: 5.5e-3 mol/L (SFLOAT exp -4, mantissa 55) -> 5.5 mmol/L."""
    rec = bytes.fromhex("070000ea0701140f0000000037c011")
    r = parse_glucose_measurement(rec)
    assert r.value == pytest.approx(5.5)
    assert r.unit == "mmol/L"
    assert "si_units_mol_per_l" in r.status
