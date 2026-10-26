#!/usr/bin/env python3
"""Pair with a meter ONCE — headless, works over SSH.

Pairing is the only step that needs a human: passkey-capable meters
(GL49, Accu-Chek Guide, OneTouch Reflect) display a 6-digit code during
the handshake that must be typed back within the meter's ~30-second
pairing window.  pair() registers its own BlueZ agent, shows the prompt
in the terminal, and marks the device trusted afterwards so the meter
can reconnect on its own inside its short transfer windows.

After this succeeds, BlueZ stores the keys in /var/lib/bluetooth and
every later dump is fully unattended — you will normally NEVER run
pairing again.  (unpair() exists, but see the big warning below.)

Run:  python 02_pair.py AA:BB:CC:DD:EE:FF
Prereqs: meter awake and in pairing mode (GL49: memory screen via the
UP button), no other pairing agent attached (`pkill bluetoothctl`).
"""
import asyncio
import sys

from glucobridge import pair


async def main() -> None:
    address = sys.argv[1]

    # pair() is IDEMPOTENT: if BlueZ already holds a bond for this
    # address it only ensures the trusted flag and returns.  Re-running
    # it can never destroy a working bond.
    #
    # renew=True is the exception: it deletes the host-side bond and
    # pairs from scratch — which usually BREAKS things, because the
    # meter keeps its own half of the old keys and silently refuses new
    # pairings until it is hard power-cycled (Bluetooth off + batteries
    # out ~10 s).  Only use renew if you already did that.
    await pair(address)          # passkey typed interactively;
                                 # Just Works is retried automatically
                                 # for prompt-less meters (GlucoCheck)

    print(f"paired with {address} — now just use download()")

    # ------------------------------------------------------------------
    # Moving the meter to ANOTHER host?  Remove this host's bond first:
    #
    #     from glucobridge import unpair
    #     if await unpair(address):
    #         print("host bond removed — power-cycle the meter before "
    #               "pairing it anywhere again (it keeps its own key!)")
    # ------------------------------------------------------------------


asyncio.run(main())
