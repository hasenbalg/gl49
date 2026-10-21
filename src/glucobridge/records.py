"""Parsers for the Bluetooth Glucose Service record formats.

Primary layout follows the Bluetooth SIG GSS (GATT Specification Supplement)
definitions (org.bluetooth.characteristic.glucose_measurement /
glucose_measurement_context, SIG public repo, 2020+).  A lenient fallback
handles the legacy pre-GSS Glucose Service 1.0 layout that older certified
meters emit (4-byte time offset, unit bit inside the concentration field,
status gated by flag bit 1).
"""
from __future__ import annotations

import datetime as dt
import struct
from typing import Optional

from .models import (
    Reading, SAMPLE_TYPES, SAMPLE_LOCATIONS, MEALS, STATUS_BITS,
)
from .sfloat import decode_sfloat16, SFloatError


class RecordError(ValueError):
    pass


def _base_time(b: bytes) -> Optional[dt.datetime]:
    """7-byte Date Time struct: year u16le, month, day, hour, min, sec."""
    year, month, day, hour, minute, sec = struct.unpack_from("<H5B", b, 0)
    try:
        return dt.datetime(year, month, day, hour, minute, sec)
    except ValueError:
        return None


def _sfloat(b: bytes, off: int) -> Optional[float]:
    try:
        return decode_sfloat16(struct.unpack_from("<H", b, off)[0])
    except (SFloatError, struct.error):
        return None


# ---------------------------------------------------------------------------
# Glucose Measurement 0x2A18
# ---------------------------------------------------------------------------

def parse_glucose_measurement(data: bytes, lenient: bool = True) -> Reading:
    """Parse a Glucose Measurement (0x2A18) notification."""
    data = bytes(data)
    if len(data) < 10:
        raise RecordError(f"glucose measurement too short: {len(data)} bytes")

    flags = data[0]
    seq = struct.unpack_from("<H", data, 1)[0]
    ts = _base_time(data[3:10])

    reading = Reading(value=None, sequence=seq, timestamp=ts, raw=data)

    # --- GSS layout ----------------------------------------------------
    p = 10
    ok = True
    if flags & 0x01:  # time offset (int16, minutes)
        if len(data) < p + 2:
            ok = False
        else:
            off = struct.unpack_from("<h", data, p)[0]
            if ts is not None:
                reading.timestamp = ts + dt.timedelta(minutes=off)
            p += 2

    if ok and flags & 0x02:  # concentration + type/sample location
        if len(data) < p + 3:
            ok = False
        else:
            reading.unit = "mmol/L" if flags & 0x04 else "mg/dL"
            reading.value = _sfloat(data, p)
            tsl = data[p + 2]
            reading.sample_type = SAMPLE_TYPES.get(tsl & 0x0F)
            reading.sample_location = SAMPLE_LOCATIONS.get((tsl >> 4) & 0x0F)
            p += 3

    if ok and flags & 0x08:  # sensor status annunciation
        if len(data) < p + 2:
            ok = False
        else:
            status = struct.unpack_from("<H", data, p)[0]
            reading.status = [
                name for i, name in enumerate(STATUS_BITS) if status & (1 << i)
            ]
            p += 2

    if ok and len(data) == p:
        _postprocess(reading)
        return reading

    # --- legacy fallback ------------------------------------------------
    if lenient:
        legacy = _parse_legacy(data, flags, seq, ts)
        if legacy is not None:
            return legacy

    if not ok:
        raise RecordError(f"truncated glucose record ({len(data)} bytes): {data.hex()}")
    # GSS parse consumed too little of the record; keep raw for diagnostics
    reading.error = f"unconsumed_tail:{data[p:].hex()}"
    _postprocess(reading)
    return reading


def _parse_legacy(data, flags, seq, ts) -> Optional[Reading]:
    """Pre-GSS Glucose Service 1.0 layout.

    flags: bit0 time offset present (sint32, 4 bytes); bit1 status present;
    bits2-3 sample location.  Glucose concentration always present:
    u16 with bit15 = unit (0 kg/L -> mg/dL, 1 kg/dL -> mmol/L in practice).
    """
    p = 10
    if flags & 0x01:
        if len(data) < p + 4:
            return None
        off = struct.unpack_from("<i", data, p)[0]
        if ts is not None:
            ts = ts + dt.timedelta(minutes=off)
        p += 4
    if len(data) < p + 2:
        return None
    conc = struct.unpack_from("<H", data, p)[0]
    p += 2
    reading = Reading(
        value=_sfloat_bytes(conc & 0x7FFF),
        unit="mmol/L" if conc & 0x8000 else "mg/dL",
        sequence=seq, timestamp=ts, raw=data,
    )
    loc = (flags >> 2) & 0x03
    reading.sample_location = {
        0x0: "not_available", 0x1: "arm", 0x2: "earlobe", 0x3: "finger",
    }.get(loc)
    if flags & 0x02 and len(data) >= p + 2:
        status = struct.unpack_from("<H", data, p)[0]
        p += 2
        reading.status = [
            name for i, name in enumerate(STATUS_BITS) if status & (1 << i)
        ]
    if len(data) != p:
        reading.error = f"legacy_tail:{data[p:].hex()}"
    _postprocess(reading)
    return reading


def _sfloat_bytes(raw16: int) -> Optional[float]:
    try:
        return decode_sfloat16(raw16)
    except SFloatError:
        return None


def _postprocess(r: Reading) -> None:
    """Map SFLOAT specials that decode to None onto lo/hi errors when the
    meter used the status bits instead."""
    if r.value is None and r.error is None:
        if "result_too_low" in r.status or "meter_result_too_low" in r.status:
            r.error = "lo"
        elif "result_too_high" in r.status or "meter_result_too_high" in r.status:
            r.error = "hi"
        else:
            r.error = "no_value"


# ---------------------------------------------------------------------------
# Glucose Measurement Context 0x2A34
# ---------------------------------------------------------------------------

def parse_glucose_context(data: bytes) -> Reading:
    """Parse a Glucose Measurement Context (0x2A34) notification.

    Returns a Reading carrying only context fields (value=None); merge it
    into the measurement with the same sequence number.
    """
    data = bytes(data)
    if len(data) < 3:
        raise RecordError(f"context record too short: {len(data)} bytes")
    flags = data[0]
    seq = struct.unpack_from("<H", data, 1)[0]
    r = Reading(value=None, sequence=seq, raw=data)
    p = 3
    if flags & 0x80:  # extended flags
        p += 1
    if flags & 0x01:  # carbohydrate id + carbohydrate
        if len(data) < p + 3:
            raise RecordError("truncated context (carbs)")
        p += 1  # carbohydrate ID
        carbs = _sfloat(data, p)
        p += 2
        # unit is kg per GSS unit table but meters report grams; keep as grams
        r.carbs_g = carbs
    if flags & 0x02:  # meal
        if len(data) < p + 1:
            raise RecordError("truncated context (meal)")
        r.meal = MEALS.get(data[p], f"unknown_{data[p]}")
        p += 1
    if flags & 0x04:  # tester-health
        p += 1
    if flags & 0x08:  # exercise duration + intensity
        if len(data) < p + 3:
            raise RecordError("truncated context (exercise)")
        r.exercise_duration_s = struct.unpack_from("<H", data, p)[0]
        r.exercise_intensity = data[p + 2]
        p += 3
    if flags & 0x10:  # medication id + medication (unit per flag bit 5)
        if len(data) < p + 3:
            raise RecordError("truncated context (medication)")
        p += 1
        r.medication = _sfloat(data, p)
        r.medication_unit = "L" if flags & 0x20 else "kg"
        p += 2
    if flags & 0x40:  # HbA1c
        if len(data) < p + 2:
            raise RecordError("truncated context (hbA1c)")
        r.hbA1c = _sfloat(data, p)
        p += 2
    if len(data) != p:
        r.error = f"context_tail:{data[p:].hex()}"
    return r


def merge_context(measurement: Reading, context: Optional[Reading]) -> Reading:
    """Copy context fields onto a measurement reading."""
    if context is None:
        return measurement
    for attr in ("meal", "carbs_g", "medication", "medication_unit",
                 "exercise_duration_s", "exercise_intensity", "hbA1c"):
        v = getattr(context, attr)
        if v is not None:
            setattr(measurement, attr, v)
    return measurement
