from glucobridge import racp


def test_report_stored_all():
    assert racp.report_stored_all() == bytes([0x01, 0x01])


def test_report_num_all():
    assert racp.report_num_stored_all() == bytes([0x04, 0x01])


def test_abort():
    assert racp.abort() == bytes([0x03, 0x00])


def test_report_after_seq():
    assert racp.report_stored_after(10) == bytes([0x01, 0x03, 0x01, 0x0A, 0x00])


def test_parse_success():
    r = racp.parse_response(bytes([0x06, 0x00, 0x01, 0x01]))
    assert r.is_success
    assert r.is_done
    assert r.request_op_code == 0x01


def test_parse_no_records():
    r = racp.parse_response(bytes([0x06, 0x00, 0x01, 0x06]))
    assert r.result == "no_records_found"
    assert r.is_done


def test_parse_num_stored():
    r = racp.parse_response(bytes([0x05, 0x00, 0x03, 0x00, 0x00, 0x00]))
    assert r.count == 3
    assert not r.is_done


def test_parse_unknown_op():
    r = racp.parse_response(bytes([0x80]))
    assert r.op_code == 0x80
    assert not r.is_done
