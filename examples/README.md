# examples

Small, heavily-commented scripts that double as usage documentation.
Each one runs standalone against your own meter — replace the address /
backend argument.

| Script | Shows |
|---|---|
| `01_scan.py` | discovering meters, reading the advertisement data |
| `02_pair.py` | the one-time pairing (and when *not* to touch bonds) |
| `03_download_ble.py` | full BLE history, units, meal tags, errors, export |
| `04_watch_ble.py` | live readings as you take them |
| `05_download_usb.py` | all four USB backends (needs `sudo` + `[usb]` extra) |

Common ground:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[usb]"   # from the repo root
.venv/bin/python examples/01_scan.py                          # BLE needs no root
sudo .venv/bin/python examples/05_download_usb.py gl49        # USB does
```

On Raspberry Pi: `sudo rfkill unblock bluetooth` before any BLE script.
