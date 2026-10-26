"""Bluetooth SIG assigned numbers used by this library (16-bit UUIDs)."""
from __future__ import annotations

BASE = "0000{:04x}-0000-1000-8000-00805f9b34fb"


def uuid16(u: int) -> str:
    return BASE.format(u)


# Services
GLUCOSE_SERVICE = uuid16(0x1808)   # Glucose SERVICE (0x181F is the Profile)
DEVICE_INFO_SERVICE = uuid16(0x180A)
CURRENT_TIME_SERVICE = uuid16(0x1805)

# Glucose Service characteristics
GLUCOSE_MEASUREMENT = uuid16(0x2A18)          # notify
GLUCOSE_MEASUREMENT_CONTEXT = uuid16(0x2A34)  # notify
RECORD_ACCESS_CONTROL_POINT = uuid16(0x2A52)  # write + indicate
GLUCOSE_FEATURE = uuid16(0x2A51)              # read

# Device Information Service
MANUFACTURER_NAME = uuid16(0x2A29)
MODEL_NUMBER = uuid16(0x2A24)
FIRMWARE_REVISION = uuid16(0x2A26)

# Current Time Service (GLS servers must have it; some clients write it)
CURRENT_TIME = uuid16(0x2A2B)

# Client Characteristic Configuration Descriptor
CCCD = uuid16(0x2902)
