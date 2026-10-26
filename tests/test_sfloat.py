import math

import pytest

from glucobridge.sfloat import SFloatError, decode_sfloat16, encode_sfloat16


def test_basic_positive():
    # mantissa 105, exponent 0 -> 105
    assert decode_sfloat16(0x0069) == 105.0


def test_negative_exponent():
    # mantissa 55, exponent -1 -> 5.5
    assert decode_sfloat16(0xF037) == pytest.approx(5.5)


def test_negative_mantissa():
    # exponent 0, mantissa -1 (0xFFF) -> -1
    assert decode_sfloat16(0x0FFF) == -1.0


def test_negative_exponent_8():
    # exponent nibble 0b1000 is -8 in two's complement, mantissa 1 -> 1e-8
    assert decode_sfloat16(0x8001) == pytest.approx(1e-8)


def test_specials():
    with pytest.raises(SFloatError):
        decode_sfloat16(0x07FF)   # NaN
    with pytest.raises(SFloatError):
        decode_sfloat16(0x07FE)   # +NRes
    assert decode_sfloat16(0x07FC) == math.inf
    assert decode_sfloat16(0x07FB) == -math.inf


def test_roundtrip():
    for v in (0.0, 1.0, 105.0, 5.5, 400.0, 0.1, -3.0):
        raw = encode_sfloat16(v)
        assert decode_sfloat16(raw) == pytest.approx(v)


def test_out_of_range():
    with pytest.raises(SFloatError):
        decode_sfloat16(0x10000)
