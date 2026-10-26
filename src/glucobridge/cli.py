"""Command-line interface: glucobridge scan|pair|dump|watch|info."""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import logging
import sys

from .client import GlucoBridge, Advertisement
from .models import Reading


def _readings_out(readings: list[Reading], args) -> None:
    if not readings and not (args.csv or args.json):
        print("0 readings.  BLE: verify pairing first "
              "(bluetoothctl info <addr> | grep Paired); if paired, the "
              "meter's RACP may be silent -- try `watch` and trigger the "
              "meter (memory recall / strip eject).", file=sys.stderr)
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            readings[0].write_csv_header(w) if readings else None
            for r in readings:
                w.writerow(r.to_csv_row())
        print(f"wrote {len(readings)} readings to {args.csv}")
    elif args.json:
        json.dump([r.to_dict() for r in readings], sys.stdout, indent=1)
        print()
    else:
        for r in readings:
            print(r)
        print(f"-- {len(readings)} readings")


def _filter_since(readings, since: str | None):
    if not since:
        return readings
    cutoff = dt.datetime.fromisoformat(since)
    return [r for r in readings
            if r.timestamp is None or r.timestamp >= cutoff]


async def cmd_scan(args):
    async with GlucoBridge() as gb:
        devices = await gb.scan(timeout=args.timeout, all_devices=args.all)
    for d in devices:
        print(d)
    if not devices:
        print("no glucose meters found")


async def cmd_pair(args):
    from .pairing import pair
    await pair(args.address, passkey=args.passkey,
               capability="NoDisplayNoKeyboard" if args.just_works
               else None)
    print(f"paired with {args.address}")


async def _with_meter(args, coro):
    async with GlucoBridge() as gb:
        meter = await gb.connect(args.address, backend=getattr(args, "backend", None))
        try:
            return await coro(meter)
        finally:
            await meter.disconnect()


async def cmd_dump(args):
    async def run(meter):
        readings = await meter.download(timeout=args.timeout)
        readings = _filter_since(readings, args.since)
        _readings_out(readings, args)
    await _with_meter(args, run)


async def cmd_watch(args):
    async def run(meter):
        print("watching for live measurements (Ctrl-C to stop)...")
        async for r in meter.watch():
            print(r, flush=True)
    try:
        await _with_meter(args, run)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


async def cmd_info(args):
    async def run(meter):
        print("GATT tree:")
        print(await meter.gatt_dump())
        info = await meter.device_info()
        if info:
            print("\nDevice info:", info)
        try:
            n = await meter.record_count()
            print(f"stored records: {n}")
        except Exception as e:
            print(f"record count not available: {e}")
    try:
        await _with_meter(args, run)
    except Exception as e:
        print(f"backend connect failed ({e}); raw GATT dump:")
        from bleak import BleakClient
        async with BleakClient(args.address) as c:
            for svc in c.services:
                print(f"service {svc.uuid}")
                for ch in svc.characteristics:
                    print(f"  char {ch.uuid}  [{','.join(ch.properties)}]")


async def cmd_usb_dump(args):
    if getattr(args, "backend", "lifescan") == "gl49":
        return await _usb_dump_gl49(args)
    from .backends.lifescan_usb import LifeScanUSBMeter, detect_device
    device = args.device or detect_device()
    if not device:
        print("no LifeScan USB meter found (lsblk should show vendor "
              "'LifeScan'); pass --device /dev/sgN", file=sys.stderr)
        return 1
    meter = LifeScanUSBMeter(device)
    await asyncio.to_thread(meter.connect)
    try:
        info = await asyncio.to_thread(meter.device_info)
        if not args.json:
            print(f"# {device}: {info}", file=sys.stderr)
        readings = await asyncio.to_thread(meter.download)
    finally:
        meter.close()
    readings = _filter_since(readings, args.since)
    _readings_out(readings, args)


async def _usb_dump_gl49(args):
    from .backends.gl49_usb import Gl49UsbMeter, SessionLost, UsbError
    meter = Gl49UsbMeter()
    try:
        await asyncio.to_thread(meter.connect)
    except UsbError as e:
        print(str(e), file=sys.stderr)
        return 1
    try:
        if not await asyncio.to_thread(meter.wait_for_session, 90.0):
            print("no session — meter OFF -> plug USB -> wait for the USB "
                  "symbol, then retry", file=sys.stderr)
            return 1
        try:
            info = await asyncio.to_thread(meter.device_info)
        except UsbError:
            info = {}
        if not args.json:
            print(f"# {meter.product}: {info}", file=sys.stderr)
        readings = await asyncio.to_thread(meter.download)
    except SessionLost as e:
        print(f"{e} — replug and retry", file=sys.stderr)
        return 1
    finally:
        meter.close()
    readings = _filter_since(readings, args.since)
    _readings_out(readings, args)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="glucobridge",
                                description="Read BLE blood glucose meters")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="scan for glucose meters")
    s.add_argument("--timeout", type=float, default=10.0)
    s.add_argument("--all", action="store_true", help="show all BLE devices")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("pair", help="pair a meter (handles the PIN prompt)")
    s.add_argument("address")
    s.add_argument("--passkey", help="6-digit code shown on the meter")
    s.add_argument("--just-works", action="store_true",
                   help="force NoInputNoOutput pairing (no passkey)")
    s.set_defaults(fn=cmd_pair)

    s = sub.add_parser("dump", help="download stored readings")
    s.add_argument("address")
    s.add_argument("--backend", choices=["gls", "fora", "verio"],
                   help="force a backend instead of auto-detecting")
    s.add_argument("--json", action="store_true")
    s.add_argument("--csv", metavar="FILE")
    s.add_argument("--since", help="ISO date/datetime filter")
    s.add_argument("--timeout", type=float, default=60.0)
    s.set_defaults(fn=cmd_dump)

    s = sub.add_parser("watch", help="stream live measurements")
    s.add_argument("address")
    s.add_argument("--backend", choices=["gls", "fora", "verio"])
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("info", help="GATT dump + device info (diagnostics)")
    s.add_argument("address")
    s.add_argument("--backend", choices=["gls", "fora", "verio"])
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("usb-dump",
                       help="download readings over USB (LifeScan MSC "
                            "meters: keep the LIFESCAN volume MOUNTED; "
                            "GL49: --backend gl49, needs root)")
    s.add_argument("--device", metavar="/dev/sgN",
                   help="sg/block node (default: auto-detect by vendor; "
                        "LifeScan backend only)")
    s.add_argument("--backend", choices=["lifescan", "gl49"],
                   default="lifescan")
    s.add_argument("--json", action="store_true")
    s.add_argument("--csv", metavar="FILE")
    s.add_argument("--since", help="ISO date/datetime filter")
    s.set_defaults(fn=cmd_usb_dump)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        return 130
    except NotImplementedError as e:
        print(f"not supported yet: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        if args.verbose:
            raise
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
