#!/usr/bin/env python3
"""Find nearby glucose meters.

glucobridge.scan() listens for BLE advertisements and keeps anything
that either advertises the standard Glucose Service (0x1808) or carries
a known vendor name pattern (GL49, GlucoCheck, meter+, OneTouch ...).
It returns Advertisement objects — no connections, so this is safe to
run anytime, alongside other work.

Run:  python 01_scan.py            (no root needed for scanning)
"""
import asyncio

from glucobridge import GlucoBridge


async def main() -> None:
    # GlucoBridge owns the BlueZ adapter access; `async with` cleans up
    # the D-Bus state even if the scan raises.
    async with GlucoBridge() as gb:

        # timeout = how long to listen.  Meters advertise only while
        # "awake": the GL49 broadcasts when its BT symbol flashes
        # (memory recall / power-off), the others when recently used.
        # 10 s is a good default; raise it if nothing shows up.
        devices = await gb.scan(timeout=10.0)

        if not devices:
            print("no meters found — are they awake? (GL49: BT enabled "
                  "in its menu, then call up the memory)")

        for adv in devices:
            # .address   MAC string, pass this to pair()/download()
            # .name      advertised local name (None for anonymous meters)
            # .rssi      signal strength, None when the adapter omits it
            # .backend   what the classifier would use: "gls" (standard
            #            Glucose Service), "verio" (LifeScan), "fora"
            #            (legacy), "unknown"
            print(adv)

            # Full detail if you need to make your own routing decision:
            print("   service UUIDs:", adv.service_uuids)

        # `await gb.scan(all_devices=True)` returns every BLE device the
        # adapter sees, glucose or not — handy when a meter advertises
        # under an unexpected name.


asyncio.run(main())
