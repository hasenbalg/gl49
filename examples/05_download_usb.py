#!/usr/bin/env python3
"""Download via the USB cable — no Bluetooth involved.

USB is the fallback/second opinion transport: for every meter here the
USB history has been verified to match the BLE history exactly.  All
four backends are plain synchronous classes (no asyncio) and share one
shape: context manager -> optional wait_for_session() -> download().

Run (needs root for raw USB/SCSI access; inside a virtualenv, route
sudo through the venv python, plain `sudo python` won't see the
package):

    sudo .venv/bin/python 05_download_usb.py gl49
    sudo .venv/bin/python 05_download_usb.py glucocheck
    sudo .venv/bin/python 05_download_usb.py guide
    sudo .venv/bin/python 05_download_usb.py lifescan

Requires:  pip install "glucobridge[usb]"
"""
import sys

# SessionLost / UsbError are raised by BOTH CP2110-based backends —
# import them once from the module that defines them.
from glucobridge.backends.gl49_usb import SessionLost, UsbError


def dump_gl49() -> None:
    """Beurer GL 49 (and FORA-cloned meters) — CP2110 HID-UART bridge.

    Physical dance: switch the meter OFF -> plug the USB cable -> the
    LCD shows a USB symbol.  The meter leaves transfer mode on its own
    a few seconds after plug-in, so wait_for_session() hammers the
    wake/identity sequence until the MCU answers; replugging while it
    runs is the documented recovery.
    """
    from glucobridge import Gl49UsbMeter

    with Gl49UsbMeter() as m:                  # finds 10c4:ea80 itself
        if not m.wait_for_session(timeout=90):
            raise SystemExit("no session — replug while it waits, retry")
        # FORA model code, session info — nice for sanity checks:
        print("device:", m.device_info())      # e.g. code 0x4149 = GL 49
        for r in m.download():                 # oldest first
            print(r.timestamp, f"{r.value:.0f} mg/dL", r.meal or "")


def dump_glucocheck() -> None:
    """TESTAmed GlucoCheck Advance & other TaiDoc TD-42xx rebadges.

    Same cable/bridge (CP2110) as the GL 49, different frame dialect.
    The meter is woken by the first frames, no power-button dance.
    Bonus: the TD-42xx dialect exposes the exact model word and the
    meter's RTC.
    """
    from glucobridge import Td42xxUsbMeter

    with Td42xxUsbMeter() as m:
        if not m.wait_for_session(timeout=90):
            raise SystemExit("no session — replug and retry")
        print("device:", m.device_info())      # {'model': 'TD-4288', ...}
        for r in m.download():
            print(r.timestamp, f"{r.value:.0f} mg/dL", r.meal or "")


def dump_guide() -> None:
    """Accu-Chek Guide — IEEE 11073-20601 over bulk USB.

    The METER initiates: start the transfer in its memory menu first
    ("Transferring data" on the LCD), then run this.  When the download
    completes cleanly the meter shows "Data transfer completed".
    Note: no meal context over USB — use BLE for tagged readings.
    """
    from glucobridge import GuideUsbMeter

    with GuideUsbMeter() as m:                 # waits for the meter's
        print("device:", m.device_info())      # association request
        for r in m.download():
            print(r.timestamp, f"{r.value:.0f} mg/dL")


def dump_lifescan() -> None:
    """OneTouch Ultra Plus Reflect / Verio 2015 family — MSC registers.

    The meter enumerates as USB mass storage, but the data path is
    vendor SCSI registers, NOT the FAT volume: keep the LIFESCAN volume
    MOUNTED (unmounting it = virtual eject, device disappears).
    detect_device() finds the SCSI generic node by vendor string.
    """
    from glucobridge import LifeScanUSBMeter
    from glucobridge.backends.lifescan_usb import detect_device

    node = detect_device()
    if node is None:
        raise SystemExit("no LifeScan meter on USB (cable? mounted?)")
    with LifeScanUSBMeter(node) as m:
        print("device:", m.device_info())      # serial, model, firmware
        for r in m.download():
            print(r.timestamp, f"{r.value:.0f} mg/dL", r.meal or "")


DUMPS = {"gl49": dump_gl49, "glucocheck": dump_glucocheck,
         "guide": dump_guide, "lifescan": dump_lifescan}

try:
    DUMPS[sys.argv[1]]()
except KeyError:
    raise SystemExit(f"usage: {sys.argv[0]} {{{'|'.join(DUMPS)}}}")
except SessionLost:
    # the meter left transfer mode (or the cable came out) mid-read;
    # the safe recovery is ALWAYS: replug, rerun.  (Must come first:
    # SessionLost is a subclass of UsbError.)
    raise SystemExit("unplugged mid-download — replug and retry")
except UsbError as e:
    # no bridge found / no permission — the message says what to fix
    raise SystemExit(str(e))
