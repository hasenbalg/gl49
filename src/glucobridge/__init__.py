"""glucobridge — read blood glucose meters over Bluetooth LE.

Supported (standard Glucose Service):
  * Beurer GL 49 (mg/dL)
  * Accu-Chek Guide / Guide Me
Supported (FORA proprietary protocol, reverse-engineered):
  * TESTAmed GlucoCheck Advance

Quick start::

    import asyncio
    from glucobridge import GlucoBridge

    async def main():
        async with GlucoBridge() as gb:
            devices = await gb.scan()
            print(devices)
            readings = await gb.download(devices[0].address)
            for r in readings:
                print(r.timestamp, r.mgdl, r.meal)

    asyncio.run(main())
"""
from .client import GlucoBridge, Advertisement, scan, classify
from .models import Reading
from .backends.gls import GlucoseMeter
from .backends.fora import ForaMeter
from .backends.verio import VerioMeter
from .records import (
    parse_glucose_measurement, parse_glucose_context, merge_context,
)
from . import racp, gatt
from .sfloat import decode_sfloat16, encode_sfloat16

__version__ = "0.1.0"
__all__ = [
    "GlucoBridge", "GlucoseMeter", "Reading", "Advertisement",
    "scan", "classify",
    "parse_glucose_measurement", "parse_glucose_context", "merge_context",
    "racp", "gatt", "decode_sfloat16", "encode_sfloat16", "__version__",
]
