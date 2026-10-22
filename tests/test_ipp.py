import struct

from printbot.ipp import (
    TAG_ENUM, TAG_KEYWORD, JobStatus, PrinterStatus, decode_response, describe_reasons,
    encode_request,
)


def test_encode_request_multi_value():
    data = encode_request(0x0009, 7, [(TAG_KEYWORD, "requested-attributes", ["a", "bc"])])
    assert data[:8] == struct.pack(">BBHI", 2, 0, 0x0009, 7)
    assert b"\x44\x00\x14requested-attributes\x00\x01a" in data
    assert b"\x44\x00\x00\x00\x02bc" in data
    assert data.endswith(b"\x03")


def _attr(tag, name, raw):
    n = name.encode()
    return bytes([tag]) + struct.pack(">H", len(n)) + n + struct.pack(">H", len(raw)) + raw


def test_decode_response():
    body = (struct.pack(">BBHI", 2, 0, 0x0000, 1) + b"\x01"
            + _attr(0x47, "attributes-charset", b"utf-8")
            + b"\x02"
            + _attr(TAG_ENUM, "job-state", struct.pack(">i", 9))
            + _attr(TAG_KEYWORD, "job-state-reasons", b"job-completed-successfully")
            + _attr(TAG_KEYWORD, "", b"other")
            + b"\x03")
    status, attrs = decode_response(body)
    assert status == 0
    assert attrs["job-state"] == [9]
    assert attrs["job-state-reasons"] == ["job-completed-successfully", "other"]


def test_job_status_labels():
    assert JobStatus(9).succeeded
    assert not JobStatus(9, reasons=("job-completed-with-errors",)).succeeded
    assert "エラー" in JobStatus(9, reasons=("job-completed-with-errors",)).label
    assert "失敗" in JobStatus(8).label


def test_describe_reasons():
    assert describe_reasons(("none",)) == []
    assert describe_reasons(("media-empty-error", "media-empty-warning", "door-open-report")) == [
        "用紙切れ", "カバーが開いています"]
    assert PrinterStatus(5).usable is False
