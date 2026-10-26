# glucobridge

Read blood glucose meters over Bluetooth LE and USB — no vendor cloud, no vendor app.

## Tested on real hardware

| Meter | Transport | Backend class | Verified |
|---|---|---|---|
| TESTAmed GlucoCheck Advance (fw V1.2.29) | BLE, standard Glucose Service (`0x1808`) | `GlucoseMeter` | full history download |
| TESTAmed GlucoCheck Advance (TaiDoc TD-4288) | USB, CP2110 HID-UART + TaiDoc frames | `Td42xxUsbMeter` | full history, model/clock (`usb-dump --backend glucocheck`, needs root; values match BLE exactly) |
| OneTouch Ultra Plus Reflect (fw SI_04.01.03) | BLE, LifeScan "Verio" protocol (`AF9DF7A1`) | `VerioMeter` | pairing, full history, meal tags, error slots |
| OneTouch Ultra Plus Reflect | USB, LifeScan MSC SCSI registers | `LifeScanUSBMeter` | full history, meal tags (`usb-dump`) |
| Beurer GL 49 (FORA TD4149 rebrand) | BLE, standard Glucose Service (`0x1808`) | `GlucoseMeter` | pairing (passkey), full history, meal tags |
| Beurer GL 49 (FORA TD4149 rebrand) | USB, CP2110 HID-UART + FORA frames | `Gl49UsbMeter` | full history, meal tags (`usb-dump --backend gl49`) |
| Accu-Chek Guide | BLE, standard Glucose Service (`0x1808`) | `GlucoseMeter` | pairing (passkey), history download, meal tags (advertises as `meter+<serial>`; exposes duplicate characteristic UUIDs) |
| Accu-Chek Guide | USB, IEEE 11073-20601 bulk APDUs (`173a:21d5`) | `GuideUsbMeter` | full history download (`usb-dump --backend guide`, needs root; values match BLE exactly, but no meal tags over USB) |

## Implemented, not yet tested on hardware

These share code paths with the verified meters or published specs and are
covered by simulated-meter unit tests, but no radio/USB capture exists yet:

* **Beurer GL 40** — BLE, standard Glucose Service (the GL 49 radio is now
  tested, see table above)
* **Accu-Chek Guide Me** — BLE, standard Glucose Service (the Guide is now
  tested, see table above)
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
connections, notifications) and
[`dbus-fast`](https://github.com/bluetooth-dev-enterprise/python-dbus-fast)
(pure-Python D-Bus, used only for the BlueZ pairing agent). The USB
backends additionally need the `[usb]` extra — `pip install -e ".[usb]"`
— which pulls in `hidapi` (the CP2110-based GL 49 / GlucoCheck backends
talk to the bridge directly via HID reports, no serial driver stack) and
`pyusb` (the Accu-Chek Guide backend). Everything else is the standard
library; the USB libraries are imported lazily, so a BLE-only install
never touches them. The package targets Python ≥ 3.10 on Linux: the
BlueZ agent paths and the USB port lock use `fcntl`, and while the
pure-BLE code imports fine on other OSes, pairing and USB are
Linux-only.

## CLI

```bash
glucobridge scan                      # find meters
glucobridge pair AA:BB:CC:DD:EE:FF    # answer the on-screen passkey in the
                                      # terminal (pair once; see Pairing notes)
glucobridge dump AA:BB:CC:DD:EE:FF --csv readings.csv # full history
glucobridge watch AA:BB:CC:DD:EE:FF   # live readings (push-capable meters;
                                      # poll-based backends poll automatically)
glucobridge info AA:BB:CC:DD:EE:FF    # GATT dump + device info (diagnostics)
glucobridge usb-dump                  # LifeScan meters over USB (keep the
                                      # LIFESCAN volume mounted; needs root)
glucobridge usb-dump --backend gl49   # Beurer GL49 over USB: switch the
                                      # meter OFF, plug the cable, wait for
                                      # the USB symbol (needs root, hidapi)
glucobridge usb-dump --backend glucocheck  # TaiDoc TD-42xx meters (CP2110,
                                      # same plug-in dance as the GL49)
glucobridge usb-dump --backend guide  # Accu-Chek Guide over USB: start the
                                      # transfer on the meter so it shows
                                      # "Transferring data" (needs root, pyusb)
```

`dump --json` and `--since 2026-10-01` also work.  `dump`/`watch`/`info`
auto-pick the backend from the advertisement and fall through to the
next candidate if one fails mid-handshake; pass `--backend` to force
one.

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

All four USB backends are plain synchronous classes (no asyncio) and
work as context managers. They need root (or a udev rule for the device
node) and `pip install "glucobridge[usb]"`.

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

**TESTAmed GlucoCheck Advance & other TaiDoc TD-42xx rebadges** — same
CP2110 bridge and plug-in dance as the GL 49, different frame dialect:

```python
from glucobridge import Td42xxUsbMeter
from glucobridge.backends.gl49_usb import SessionLost, UsbError

with Td42xxUsbMeter() as m:              # meter awake (USB frames wake it)
    if not m.wait_for_session(timeout=90):
        raise SystemExit("no session — replug and retry")
    print(m.device_info())               # {'model': 'TD-4288', ...}
    for r in m.download():
        print(r.timestamp, f"{r.value:.0f} mg/dL")
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

**Accu-Chek Guide** — enumerates as a vendor-specific bulk device
(`173a:21d5`); the meter initiates the transfer, so start it on the
device first (memory menu → transfer, LCD shows "Transferring data"):

```python
from glucobridge import GuideUsbMeter

with GuideUsbMeter() as m:               # waits up to 20 s for the meter
    print(m.device_info())               # model/serial as reported by the meter
    for r in m.download():               # the meter then shows
        print(r.timestamp, f"{r.value:.0f} mg/dL")   # "Data transfer completed"
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
    # Type the code that is on the meter's display RIGHT NOW — the code
    # is regenerated for every attempt, so it can never be pre-supplied.
    await pair(address)

    # Pairing is one-time: BlueZ stores the keys in /var/lib/bluetooth,
    # every later connect is transparent and fully unattended.
    async with GlucoBridge() as gb:
        readings = await gb.download(address, backend="verio")
        print(f"{len(readings)} readings, newest: {readings[-1].timestamp}")

asyncio.run(main())
```

A desktop agent (blueman, an attached `bluetoothctl` session) can answer
the prompt before we do.  `pair` registers itself as the default agent
for its duration — but close other agent sessions first anyway; a
*connected* `bluetoothctl` still steals `RequestPasskey` calls.

## Pairing notes

How `glucobridge pair` behaves (all live-learned on these meters):

* **One attempt at a time, answer fast.** Meters open a pairing window
  of roughly 30 s from the moment their pairing screen appears, and
  BlueZ adds its own 30 s pairing timer.  Have the meter already on its
  pairing display and type the code the instant the terminal asks —
  late answers die as `Authentication Canceled`, and `Page Timeout`
  simply means the meter wasn't connectable (window closed / asleep):
  wake it and retry.
* **The code is per-attempt.** It is generated when the handshake
  starts, so a stale on-screen code never matches — re-enter the
  pairing screen for every new attempt.
* **Idempotent by design.** If BlueZ already holds a bond, `pair` just
  marks the device trusted and exits; it will never touch working keys
  unless you pass `--renew` (which removes the BlueZ bond first).
  On success the device is auto-trusted so it can reconnect inside its
  short transfer windows.  `--just-works` forces the
  NoInputNoOutput flow; there is also an automatic Just Works retry
  when passkey entry fails.
* **Never unpair one side alone.** These meters have no unpair UI: if
  BlueZ forgets its keys while the meter still holds its half (e.g.
  after `--renew`, or a formatted host), the meter silently ignores
  fresh pairing attempts.  Recovery: power-cycle the meter hard
  (Bluetooth off in its menu, then batteries out for ~10 s), and keep
  `pair` out of your daily workflow entirely — after one successful
  pairing, `dump` alone is all you need.
* **First pair against a new host:** if the meter was bonded to another
  machine (or a phone app), that bond competes — delete it on the old
  host or power-cycle the meter, and keep phones away during pairing.
* **Raspberry Pi hosts** ship Bluetooth soft-blocked out of the box:
  `rfkill unblock bluetooth` before anything else (`rfkill list` proves
  it).
* Pairing traces every agent callback to stderr (`-v` shows more);
  when a handshake fails, that trace pinpoints which side dropped out.

Per meter:

* **Beurer GL 49** — enable Bluetooth in the device menu first (SET
  until the BT symbol, toggle on, confirm with SET), then enter the
  archive screen by pressing the up button at the top left.
  Outside pairing mode it only advertises while it wants to transfer
  (BT symbol flashing): press the Apple/app key, pull out the test
  strip, call up the memory, or switch it off.  Passkey entry —
  live-verified with `glucobridge pair` itself.
* **Accu-Chek Guide** — same passkey flow, live-verified.  Advertises
  as `meter+<serial>`.
* **OneTouch Reflect / Flex family** — connect on service `AF9DF7A1…`
  (write F7A2, notify F7A3).  Passkey pairing, live-verified.  Note the
  meter also exposes a *dead* standard Glucose Service as a decoy —
  `dump` auto-falls through, but a forced `--backend gls` will fail.
  Like the FORA meters they never push: `watch` polls the record
  counter.
* **TESTAmed GlucoCheck Advance** — Just Works pairing: `pair`
  completes with no prompt (live-verified).  It also exposes a Nordic
  UART service (00001523-…) which is *not* used for glucose data.  Its
  firmware reports the concentration in the formal GSS SI unit (kg/L),
  which the library converts to mg/dL automatically.
* **iFORA meters** — no pairing needed; the FORA backend connects
  directly (service FFF0).  They never push live data, so `watch` polls
  the record count.

Alternatively pair once with `bluetoothctl` — `pair` and `dump` will use
whatever bond BlueZ holds; but keep `bluetoothctl` *exited* afterwards
(a lingering attached session steals passkey prompts from the library).

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests/ -q
```

The test suite includes spec-derived byte vectors for every protocol
family (GSS RACP, FORA 8-byte frames, TaiDoc TD-42xx, Verio framed
commands, LifeScan MSC, IEEE 11073-20601 APDUs) and simulated meters
exercising the full download flows — no hardware needed.  Live captures
double as fixtures wherever a meter has been verified.

`tools/` holds the raw probes used during reverse-engineering
(`gl49_hid_diag.py`, `gl49_usb.py`, `glucocheck_probe.py`); the latter
fingerprints any CP2110-based meter's dialect (FORA vs TD-42xx) in one
run.
Protocol reverse-engineering writeups: `FINDINGS.md` (GL49/GSS/Guide/
Verio), `FINDINGS-fora.md` (FORA/TESTAmed).
