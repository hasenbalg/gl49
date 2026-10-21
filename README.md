# glucobridge

Read blood glucose meters over Bluetooth LE — no vendor cloud, no vendor app.

| Meter | Transport | Status |
|---|---|---|
| Beurer GL 49 / GL 40 | BLE, standard Glucose Service (0x181F) | supported |
| Accu-Chek Guide / Guide Me | BLE, standard Glucose Service | supported |
| TESTAmed GlucoCheck Advance | BLE, FORA proprietary protocol (FFF0/FFF1) | supported (live-test pending) |
| iFORA GH810 / HE211 / TD-Link | BLE, FORA proprietary protocol (FFF0/FFF1) | supported (live-test pending) |
| OneTouch Ultra Plus Reflect / Verio Reflect / Verio Flex / Ultra Plus Flex / Select Plus Flex | BLE, LifeScan "Verio" protocol (AF9DF7A1) | supported (live-test pending) |

The standard path follows the Bluetooth SIG GSS definitions for the Glucose
Service: records (`0x2A18`), context (`0x2A34`), and the Record Access
Control Point (`0x2A52`) for pulling the meter's full stored history.
A lenient fallback also parses the legacy pre-GSS record layout that older
certified meters emit.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

Requires BlueZ (Linux) with a Bluetooth adapter.

## CLI

```bash
glucobridge scan                      # find meters
glucobridge pair AA:BB:CC:DD:EE:FF --passkey 123456   # GL49 shows a 6-digit PIN
glucobridge dump AA:BB:CC:DD:EE:FF --csv readings.csv # full history
glucobridge watch AA:BB:CC:DD:EE:FF   # live readings (meter pushes on power-off)
glucobridge info AA:BB:CC:DD:EE:FF    # GATT dump + device info (diagnostics)
```

`dump --json` and `--since 2026-10-01` also work.

## Library

```python
import asyncio
from glucobridge import GlucoBridge

async def main():
    async with GlucoBridge() as gb:
        devices = await gb.scan()
        for d in devices:
            print(d)
        readings = await gb.download(devices[0].address)
        for r in readings:
            print(r.timestamp, f"{r.mgdl:.0f} mg/dL", r.meal or "")

asyncio.run(main())
```

Each `Reading` carries: value + unit (`mg/dL`/`mmol/L`, both conversions via
`.mgdl` / `.mmol_l`), timestamp, sequence number, sample type/location,
status bits (battery low, insufficient sample, ...), and merged context
(meal marker pre/postprandial/fasting/casual/bedtime, carbs, medication,
exercise, HbA1c) when the meter sends context records.

## Pairing notes

* **Beurer GL 49** — enable Bluetooth by entering the memory view, the
  device shows a 6-digit code; `glucobridge pair <addr> --passkey NNNNNN`
  answers the BlueZ prompt. Keys are stored by BlueZ; later connects are
  transparent. Transfers start when you power the meter off or pull up the
  memory.
* **Accu-Chek Guide** — pairs the same way (passkey shown on the meter).
* **TESTAmed GlucoCheck Advance / iFORA meters** — no pairing needed; the
  FORA backend connects directly (service FFF0). They never push live
  data, so `watch` polls the record count.
* **OneTouch Reflect / Flex family** — connect on service `AF9DF7A1…`
  (write F7A2, notify F7A3). Some models (Reflect) ask for pairing —
  accept the iOS-style prompt with `glucobridge pair <addr>` (just
  works). Like the FORA meters they never push: `watch` polls the
  record counter.
* Alternatively pair once with `bluetoothctl`.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests/ -q
```

The test suite includes spec-derived byte vectors for all three protocol
families (GSS RACP, FORA 8-byte frames, Verio framed commands) and
simulated meters exercising the full download flows — no hardware needed.
Protocol reverse-engineering writeups: `FINDINGS.md` (GL49/GSS),
`FINDINGS-fora.md` (FORA/TESTAmed), and the Verio section in `FINDINGS.md`.
