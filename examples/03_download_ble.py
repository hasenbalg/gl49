#!/usr/bin/env python3
"""Download a meter's full stored history over Bluetooth LE.

This is the everyday operation — pairing happened once, long ago, and
BlueZ handles reconnection transparently.  download() pulls the whole
memory through the meter's history protocol (RACP on standard meters,
vendor equivalents elsewhere), returns Readings oldest-first and
disconnects.

Run:  python 03_download_ble.py AA:BB:CC:DD:EE:FF
"""
import asyncio

from glucobridge import GlucoBridge


def show(reading) -> str:
    """Readings normalize every transport into one record type."""
    ts = reading.timestamp or "?"          # naive local datetime
    if reading.value is None:
        # Meters also store failed test attempts (E-1..E-9 codes,
        # "lo"/"hi"); they arrive as value=None with .error set.
        return f"{ts}  ERR({reading.error})"
    # .value is in .unit ("mg/dL" or "mmol/L"); .mgdl / .mmol_l convert
    # to the unit you want regardless of what the meter reported
    # (some GlucoCheck firmwares report the GSS SI unit kg/L — already
    # corrected by the backend).
    line = f"{ts}  {reading.mgdl:.0f} mg/dL"
    # Context records add meal markers when the user tagged the test on
    # the device (GL49: apple key cycles before/after/general):
    if reading.meal:                       # "preprandial" | "postprandial" | ...
        line += f"  [{reading.meal}]"
    # Also available when present: reading.carbs_g, .medication,
    # .exercise_duration_s, .hbA1c, .sample_type, .sample_location,
    # .status (list: e.g. "battery_low"), .sequence, .device.
    return line


async def main(address: str) -> None:
    async with GlucoBridge() as gb:
        # backend=None (default): classify the advertisement, try that
        # backend and transparently fall through to the others if one
        # connects but fails — important because the Reflect exposes a
        # DEAD standard Glucose Service as a decoy that must be skipped.
        readings = await gb.download(address)

        for r in readings:
            print(show(r))
        print(f"-- {len(readings)} readings")

        # Force a specific backend ("gls" | "verio" | "fora") to skip
        # the guessing:  await gb.download(address, backend="verio")

    # --- export: every Reading serializes losslessly -------------------
    # JSON (includes both unit conversions under mgdl/mmol_l):
    #     import json; json.dump([r.to_dict() for r in readings], ...)
    # CSV (stable header via Reading.write_csv_header):
    #     import csv
    #     with open("readings.csv", "w", newline="") as f:
    #         w = csv.writer(f)
    #         readings[0].write_csv_header(w)
    #         for r in readings:
    #             w.writerow(r.to_csv_row())


asyncio.run(main("AA:BB:CC:DD:EE:FF"))
