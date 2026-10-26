"""Advertisement classification tests (backend routing)."""
from glucobridge.client import classify

GSS = "00001808-0000-1000-8000-00805f9b34fb"
FORA = "0000fff0-0000-1000-8000-00805f9b34fb"
VERIO = "af9df7a1-e595-11e3-96b4-0002a5d5c51b"


def test_gl49_advertises_without_space():
    # live capture 2026-10-26: BlueZ shows plain "GL49"
    assert classify("GL49", []) == "gls"
    assert classify("GL 49", []) == "gls"


def test_name_patterns():
    assert classify("OneTouch CC91", [GSS]) == "verio"   # decoy GSS + name
    assert classify("GlucoCheck ADVANCE", [GSS]) == "gls"
    assert classify("TD4149", []) == "fora"
    assert classify("Beurer GL 60", []) == "gls"


def test_service_hints_when_name_unknown():
    assert classify("xyz", [GSS]) == "gls"
    assert classify("xyz", [FORA]) == "fora"
    assert classify("xyz", [VERIO, GSS]) == "verio"      # verio beats decoy
    assert classify("", []) == "unknown"
