#!/usr/bin/env python3
"""Stream live readings over BLE — run it, then take a test.

Meters are weird about "live": most only push the measurement after you
power the test strip section off again, and the poll-based vendors
(LifeScan Reflect, FORA/iFORA) need the backend to re-read the record
counter periodically.  meter.watch() hides all that: it yields each NEW
Reading the moment it becomes available, forever.

Run:  python 04_watch_ble.py AA:BB:CC:DD:EE:FF   (Ctrl-C to stop)
"""
import asyncio

from glucobridge import GlucoBridge


async def main(address: str) -> None:
    async with GlucoBridge() as gb:

        # watch() lives on the connected meter object, so we hand a
        # coroutine to try_backends(): it picks the right backend from
        # the advertisement and keeps the meter connected while `fn`
        # runs (and falls through backends if one fails to deliver).
        async def stream(meter):
            print("watching — take a measurement now "
                  "(GL49: push test strip in AND out)")
            try:
                # async generator: yields Reading objects as they appear.
                # On push-capable meters this subscribes to the
                # Glucose Measurement characteristic; on poll-based ones
                # it reads the memory counter every ~10-15 s.
                async for r in meter.watch():
                    v = (f"{r.mgdl:.0f} mg/dL" if r.value is not None
                         else f"ERR({r.error})")
                    print(f"{r.timestamp}  {v}  {r.meal or ''}",
                          flush=True)
            except asyncio.CancelledError:
                # Ctrl-C / task cancel: run() exits, try_backends
                # disconnects the meter politely.
                print("stopped")

        await gb.try_backends(address, None, stream)


asyncio.run(main("AA:BB:CC:DD:EE:FF"))
