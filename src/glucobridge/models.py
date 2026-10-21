"""Data model for glucose readings."""
from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

MMOL_PER_MGDL = 1 / 18.0182  # glucose molar mass conversion

SAMPLE_TYPES = {
    0x1: "capillary_whole_blood",
    0x2: "capillary_plasma",
    0x3: "venous_whole_blood",
    0x4: "venous_plasma",
    0x5: "arterial_whole_blood",
    0x6: "arterial_plasma",
    0x7: "undetermined_whole_blood",
    0x8: "undetermined_plasma",
    0x9: "interstitial_fluid",
    0xA: "control_solution",
}

SAMPLE_LOCATIONS = {
    0x1: "finger",
    0x2: "alternate_site",
    0x3: "earlobe",
    0x4: "control_solution",
    0xF: "not_available",
}

MEALS = {
    0x1: "preprandial",
    0x2: "postprandial",
    0x3: "fasting",
    0x4: "casual",
    0x5: "bedtime",
}

STATUS_BITS = [
    "battery_low",
    "sensor_malfunction",
    "sample_insufficient",
    "strip_insertion_error",
    "meter_result_too_high",
    "meter_result_too_low",
    "strip_type_incorrect",
    "result_broken_line",
    "sensor_time_mismatch",
    "sensor_temperature_out_of_range",
    "sensor_result_older_than_15min",
]


@dataclass
class Reading:
    """One glucose measurement, optionally merged with its context record."""

    value: Optional[float]          # numeric value in `unit`, None for error records
    unit: str = "mg/dL"             # "mg/dL" or "mmol/L"
    timestamp: Optional[dt.datetime] = None
    sequence: Optional[int] = None
    sample_type: Optional[str] = None
    sample_location: Optional[str] = None
    status: list = field(default_factory=list)   # list of STATUS_BITS names set
    error: Optional[str] = None                  # e.g. "lo", "hi", "parse_error"
    # context (0x2A34) fields, present when the meter sends context records
    meal: Optional[str] = None
    carbs_g: Optional[float] = None
    medication: Optional[float] = None
    medication_unit: Optional[str] = None
    exercise_duration_s: Optional[int] = None
    exercise_intensity: Optional[int] = None
    hbA1c: Optional[float] = None
    device: Optional[str] = None    # address or name of the source meter
    raw: Optional[bytes] = None     # raw measurement notification, for debugging

    # ------------------------------------------------------------------ units
    @property
    def mgdl(self) -> Optional[float]:
        if self.value is None:
            return None
        return self.value if self.unit == "mg/dL" else self.value / MMOL_PER_MGDL

    @property
    def mmol_l(self) -> Optional[float]:
        if self.value is None:
            return None
        return self.value if self.unit == "mmol/L" else self.value * MMOL_PER_MGDL

    # ---------------------------------------------------------------- export
    def to_dict(self) -> dict:
        d = asdict(self)
        raw = d.pop("raw", None)
        if raw is not None:
            d["raw"] = bytes(raw).hex()
        d["mgdl"] = self.mgdl
        d["mmol_l"] = self.mmol_l
        if self.timestamp is not None:
            d["timestamp"] = self.timestamp.isoformat()
        return d

    _CSV_FIELDS = [
        "timestamp", "sequence", "mgdl", "mmol_l", "unit", "meal",
        "sample_type", "sample_location", "carbs_g", "status", "error", "device",
    ]

    def write_csv_header(self, w: csv.writer) -> None:
        w.writerow(self._CSV_FIELDS)

    def to_csv_row(self) -> list:
        d = self.to_dict()
        row = []
        for k in self._CSV_FIELDS:
            v = d.get(k)
            if isinstance(v, list):
                v = "|".join(map(str, v))
            row.append("" if v is None else v)
        return row

    def __str__(self) -> str:  # concise one-liner for CLI
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M") if self.timestamp else "?"
        val = f"{self.value:g} {self.unit}" if self.value is not None else f"ERR({self.error})"
        extra = f" [{self.meal}]" if self.meal else ""
        return f"{ts}  {val}{extra}"
