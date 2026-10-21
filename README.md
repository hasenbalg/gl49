# glucobridge

Read blood glucose meters over Bluetooth LE — no vendor cloud, no vendor app.

| Meter | Transport | Status |
|---|---|---|
| Beurer GL 49 / GL 40 | BLE, standard Glucose Service (0x181F) | supported |
| Accu-Chek Guide / Guide Me | BLE, standard Glucose Service | supported |
| TESTAmed GlucoCheck Advance | BLE, standard Glucose Service (0x181F) | supported (live-verified) |
| iFORA GH810 / HE211 / TD-Link | BLE, FORA proprietary protocol (FFF0/FFF1) | supported (live-test pending) |
| OneTouch Ultra Plus Reflect / Verio Reflect / Verio Flex / Ultra Plus Flex / Select Plus Flex | BLE, LifeScan "Verio" protocol (AF9DF7A1) | supported (live-verified) |

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

### Pairing (bonded meters)

The GL49, Accu-Chek Guide and OneTouch Reflect show a 6-digit passkey on
their display when pairing starts. `pair()` registers its own BlueZ
agent and asks for the code **in the terminal** — no GUI needed, so it
works over SSH or from a headless box:

```python
import asyncio
from glucobridge import GlucoBridge, pair

async def main():
    address = "EA:7B:85:08:A1:D9"          # OneTouch Ultra Plus Reflect
    # Interactive: prompts "passkey shown on device: _" in the terminal.
    await pair(address)
    # Scripted instead (e.g. code read from a photo of the display):
    # await pair(address, passkey="630802")

    # Pairing is one-time: BlueZ stores the keys in /var/lib/bluetooth,
    # every later connect is transparent and fully unattended.
    async with GlucoBridge() as gb:
        readings = await gb.download(address, backend="verio")
        print(f"{len(readings)} readings, newest: {readings[-1].timestamp}")

asyncio.run(main())
```

If a desktop agent (blueman) is running it may answer the prompt first;
`pair()` deliberately takes over as the default agent for its duration.

## Pairing notes

* **Beurer GL 49** — enable Bluetooth in the device menu first (SET
  until the BT symbol, toggle on, confirm with SET). The meter only
  advertises while it wants to transfer (BT symbol flashing): press the
  Apple/app key, pull out the test strip, call up the memory, or switch
  it off. It then shows a 6-digit code;
  `glucobridge pair <addr> --passkey NNNNNN` answers the BlueZ prompt.
  Keys are stored by BlueZ; later connects are transparent. Keep phones
  running the Beurer app away — a bonded phone hogs the connection.
* **Accu-Chek Guide** — pairs the same way (passkey shown on the meter).
* **TESTAmed GlucoCheck Advance** — standard Glucose Service, no pairing
  dance needed; connects directly. It also exposes a Nordic UART service
  (00001523-…) which is *not* used for glucose data. Its firmware
  reports the concentration in the formal GSS SI unit (kg/L), which the
  library converts to mg/dL automatically.
* **iFORA meters** — no pairing needed; the FORA backend connects
  directly (service FFF0). They never push live data, so `watch` polls
  the record count.
* **OneTouch Reflect / Flex family** — connect on service `AF9DF7A1…`
  (write F7A2, notify F7A3). The Reflect asks for pairing
  (live-verified): `glucobridge pair <addr>` prompts for the meter's
  6-digit code in the terminal — no GUI agent needed; `--passkey NNNNNN`
  for scripting. Like the FORA meters they never push: `watch` polls the
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
