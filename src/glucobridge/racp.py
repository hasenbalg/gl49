"""Record Access Control Point (0x2A52) request/response codec.

Per the Bluetooth SIG GSS definition of
org.bluetooth.characteristic.record_access_control_point.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

# Request op codes (host -> meter)
OP_REPORT_STORED_RECORDS = 0x01
OP_DELETE_STORED_RECORDS = 0x02
OP_ABORT = 0x03
OP_REPORT_NUM_STORED = 0x04

# Response op codes (meter -> host)
OP_NUM_STORED_RESPONSE = 0x05
OP_RESPONSE_CODE = 0x06
OP_COMBINED_REPORT = 0x07
OP_FILTER_POLICY = 0x08

# Operators
OPERATOR_NULL = 0x00
OPERATOR_ALL = 0x01
OPERATOR_LE = 0x02
OPERATOR_GE = 0x03
OPERATOR_WITHIN_RANGE = 0x04
OPERATOR_FIRST = 0x05   # oldest
OPERATOR_LAST = 0x06    # most recent

# GLS filter format for operands
FILTER_SEQUENCE_NUMBER = 0x01

RESPONSE_CODES = {
    0x01: "success",
    0x02: "op_code_not_supported",
    0x03: "invalid_operator",
    0x04: "operator_not_supported",
    0x05: "invalid_operand",
    0x06: "no_records_found",
    0x07: "abort_unsuccessful",
    0x08: "procedure_not_completed",
    0x09: "operand_not_supported",
    0x0A: "user_control_rejected_operation",
    0x0B: "operation_not_supported",
}


# ------------------------------------------------------------------ requests
def report_stored_all() -> bytes:
    """Report stored records / all records."""
    return bytes([OP_REPORT_STORED_RECORDS, OPERATOR_ALL])


def report_stored_last(n: int = 1) -> bytes:
    """Report stored records / last n records (GLS: seq-number filter, GE).

    GLS uses 'greater than or equal to' on (max_seq - n + 1); since max seq
    is unknown, most stacks accept operator LAST with a count operand only
    for Combined Report.  We use GE with seq 0..n semantics instead:
    prefer report_stored_after().
    """
    return bytes([OP_REPORT_STORED_RECORDS, OPERATOR_LAST])


def report_stored_after(seq: int) -> bytes:
    """Report stored records with sequence number >= seq."""
    return (bytes([OP_REPORT_STORED_RECORDS, OPERATOR_GE, FILTER_SEQUENCE_NUMBER])
            + struct.pack("<H", seq))


def report_num_stored_all() -> bytes:
    return bytes([OP_REPORT_NUM_STORED, OPERATOR_ALL])


def abort() -> bytes:
    return bytes([OP_ABORT, OPERATOR_NULL])


def delete_stored_all() -> bytes:
    """DANGER: wipes the meter's memory. Never call implicitly."""
    return bytes([OP_DELETE_STORED_RECORDS, OPERATOR_ALL])


# ----------------------------------------------------------------- responses
@dataclass
class RACPResponse:
    op_code: int
    result: Optional[str] = None      # for 0x06
    request_op_code: Optional[int] = None
    count: Optional[int] = None       # for 0x05
    raw: bytes = b""

    @property
    def is_success(self) -> bool:
        return self.op_code == OP_RESPONSE_CODE and self.result == "success"

    @property
    def is_done(self) -> bool:
        """True when this response terminates a transfer."""
        return self.op_code == OP_RESPONSE_CODE


def parse_response(data: bytes) -> RACPResponse:
    data = bytes(data)
    if not data:
        raise ValueError("empty RACP response")
    op = data[0]
    if op == OP_RESPONSE_CODE and len(data) >= 4:
        req_op = data[2]
        code = data[3]
        return RACPResponse(
            op_code=op,
            request_op_code=req_op,
            result=RESPONSE_CODES.get(code, f"unknown_{code:#x}"),
            raw=data,
        )
    if op == OP_NUM_STORED_RESPONSE and len(data) >= 6:
        count = struct.unpack_from("<I", data, 2)[0]
        return RACPResponse(op_code=op, count=count, raw=data)
    return RACPResponse(op_code=op, raw=data)
