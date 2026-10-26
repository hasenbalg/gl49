"""glucobridge — read blood glucose meters over Bluetooth LE.

Supported (standard Glucose Service):
  * Beurer GL 49 / GL 40 (mg/dL)
  * Accu-Chek Guide / Guide Me
  * TESTAmed GlucoCheck Advance
Supported (LifeScan "Verio" protocol, reverse-engineered):
  * OneTouch Ultra Plus Reflect / Verio Reflect / Verio Flex
Supported (FORA proprietary protocol, reverse-engineered):
  * iFORA GH810 / HE211 / TD-Link

Quick start::

    import asyncio
    from glucobridge import GlucoBridge, pair

    async def main():
        async with GlucoBridge() as gb:
            # One-time for meters that require bonding (GL49, Guide,
            # Reflect): pair() prompts for the meter's 6-digit code in
            # the terminal, or pass passkey="123456" to script it.
            await pair("EA:7B:85:08:A1:D9")
            devices = await gb.scan()
            print(devices)
            readings = await gb.download(devices[0].address)
            for r in readings:
                print(r.timestamp, r.mgdl, r.error or r.meal or "")

    asyncio.run(main())
"""
from . import gatt, racp
from .backends.fora import ForaMeter
from .backends.gl49_usb import Gl49UsbMeter
from .backends.gls import GlucoseMeter
from .backends.glucocheck_usb import Td42xxUsbMeter
from .backends.guide_usb import GuideUsbMeter
from .backends.lifescan_usb import LifeScanUSBMeter
from .backends.verio import VerioMeter
from .client import Advertisement, GlucoBridge, classify, scan
from .models import Reading
from .pairing import pair, unpair
from .records import (
    merge_context,
    parse_glucose_context,
    parse_glucose_measurement,
)
from .sfloat import decode_sfloat16, encode_sfloat16

__version__ = "0.3.0"
__all__ = [
    "GlucoBridge", "GlucoseMeter", "Reading", "Advertisement",
    "scan", "classify", "pair", "unpair", "LifeScanUSBMeter", "Gl49UsbMeter",
    "GuideUsbMeter", "Td42xxUsbMeter", "VerioMeter", "ForaMeter",
    "parse_glucose_measurement", "parse_glucose_context", "merge_context",
    "racp", "gatt", "decode_sfloat16", "encode_sfloat16", "__version__",
]
