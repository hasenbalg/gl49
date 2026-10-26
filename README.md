# glucobridge

Read blood glucose meters over Bluetooth LE and USB — no vendor cloud, no vendor app.

## Tested on real hardware

| Meter | Transport | Backend class | Verified |
|---|---|---|---|
| TESTAmed GlucoCheck Advance (fw V1.2.29) | BLE, standard Glucose Service (`0x1808`) | `GlucoseMeter` | full history download |
| OneTouch Ultra Plus Reflect (fw SI_04.01.03) | BLE, LifeScan "Verio" protocol (`AF9DF7A1`) | `VerioMeter` | pairing, full history, meal tags, error slots |
| OneTouch Ultra Plus Reflect | USB, LifeScan MSC SCSI registers | `LifeScanUSBMeter` | full history, meal tags (`usb-dump`) |
| Beurer GL 49 (FORA TD4149 rebrand) | USB, CP2110 HID-UART + FORA frames | `Gl49UsbMeter` | full history, meal tags (`usb-dump --backend gl49`) |
| Beurer GL 49 (FORA TD4149 rebrand) | BLE, standard Glucose Service (`0x1808`) | `GlucoseMeter` | pairing (passkey), full history, meal context |

## Implemented, not yet tested on hardware

These share code paths with the verified meters or published specs and are
covered by simulated-meter unit tests, but no radio/USB capture exists yet:

* **Beurer GL 40** — BLE, standard Glucose Service (the GL 49 radio is now
  tested, see table above)
* **Accu-Chek Guide / Guide Me** — BLE, standard Glucose Service
* **iFORA GH810 / HE211 / TD-Link** — BLE, FORA proprietary protocol (`FFF0`)
* **Verio Reflect / Verio Flex / Ultra Plus Flex / Select Plus Flex** — BLE,
  same Verio protocol as the tested Ultra Plus Reflect
* **Verio 2015 / Select Plus (+Flex)** — USB, same MSC register protocol as
  the tested Reflect

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

**Dependencies.** The only runtime dependencies are
[`bleak`](https://github.com/hbbluetoothtools/bleak) (BLE scanning, GATT
connections, notifications) and its companion
[`dbus-fast`](https://github.com/bluetooth-dev-enterprise/python-dbus-fast)
(pure-Python D-Bus, used only for the BlueZ pairing agent; it ships with
bleak on Linux anyway). The GL 49 USB backend additionally needs
[`hidapi`](https://pypi.org/project/hidapi/) — install it with
`pip install -e ".[usb]"` — and talks to the meter's CP2110 HID-UART
bridge directly via HID reports, so no serial driver stack is involved.
Everything else is the standard library. The package targets Python ≥ 3.10
on Linux: the BlueZ agent paths and the USB port lock use `fcntl`, and
while the pure-BLE code imports fine on other OSes, pairing and USB are
Linux-only. Tests run with `pip install -e ".[dev]"` (pytest).

## CLI

```bash
glucobridge scan                      # find meters
glucobridge pair AA:BB:CC:DD:EE:FF --passkey 123456   # GL49 shows a 6-digit PIN
glucobridge dump AA:BB:CC:DD:EE:FF --csv readings.csv # full history
glucobridge watch AA:BB:CC:DD:EE:FF   # live readings (meter pushes on power-off)
glucobridge info AA:BB:CC:DD:EE:FF    # GATT dump + device info (diagnostics)
glucobridge usb-dump                  # LifeScan meters over USB (keep the
                                      # LIFESCAN volume mounted; needs root)
glucobridge usb-dump --backend gl49   # Beurer GL49 over USB: switch the
                                      # meter OFF, plug the cable, wait for
                                      # the USB symbol (needs root, hidapi)
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

### USB meters (no Bluetooth needed)

Both USB backends are plain synchronous classes (no asyncio) and work
as context managers. They need root (or a udev rule for the device
node) and `pip install "glucobridge[usb]"` for the GL49.

**Beurer GL 49** — switch the meter OFF, plug the USB cable, the LCD
shows a USB symbol. The meter leaves transfer mode shortly after
plug-in, so call `wait_for_session()` (it polls until the meter
answers — replug while it runs) and download immediately after:

```python
from glucobridge import Gl49UsbMeter
from glucobridge.backends.gl49_usb import SessionLost, UsbError

try:
    with Gl49UsbMeter() as m:            # auto-detects the CP2110 (10c4:ea80)
        if not m.wait_for_session(timeout=90):
            raise SystemExit("no session — meter OFF -> plug USB -> "
                             "wait for the USB symbol, then retry")
        print(m.device_info())
        # {'code': '0x4149', 'model': 'FORA TD4149 (Beurer GL 49)', ...}

        readings = m.download()          # oldest first, .sequence assigned
        for r in readings:
            print(r.timestamp, f"{r.value:.0f} mg/dL", r.raw.hex())
except UsbError as e:
    raise SystemExit(str(e))             # no CP2110 found / no permission
except SessionLost:
    raise SystemExit("unplugged mid-download — replug and retry")
```

**OneTouch Ultra Plus Reflect / Verio 2015 family** — enumerates as
USB mass storage; the data path is SCSI registers, so the LIFESCAN
volume must stay MOUNTED (unmounting ejects the medium):

```python
from glucobridge import LifeScanUSBMeter
from glucobridge.backends.lifescan_usb import detect_device

device = detect_device()                 # /dev/sgN by SCSI vendor "LifeScan"
if device is None:
    raise SystemExit("no LifeScan meter on USB")

with LifeScanUSBMeter(device) as m:
    print(m.device_info())               # serial, model, firmware
    for r in m.download():
        print(r.timestamp, f"{r.value:.0f} mg/dL", r.meal or "")
```

Every `Reading` exports the same way regardless of transport —
`r.mgdl` / `r.mmol_l` for unit conversion, `r.to_dict()` for JSON,
`r.to_csv_row()` for CSV:

```python
import json
from glucobridge import Gl49UsbMeter

with Gl49UsbMeter() as m:
    m.wait_for_session()
    data = [r.to_dict() for r in m.download()]
print(json.dumps(data, indent=2))
```

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

The test suite includes spec-derived byte vectors for all four protocol
families (GSS RACP, FORA 8-byte frames, Verio framed commands, LifeScan
MSC USB packets) and simulated meters exercising the full download flows
— no hardware needed.
Protocol reverse-engineering writeups: `FINDINGS.md` (GL49/GSS),
`FINDINGS-fora.md` (FORA/TESTAmed), and the Verio section in `FINDINGS.md`.
