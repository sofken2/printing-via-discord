import pytest

from printbot.printing import Cups, PrintError, parse_lpstat_jobs, parse_request_id


def test_parse_request_id():
    assert parse_request_id("request id is TS6730-42 (1 file(s))\n") == "TS6730-42"


def test_parse_request_id_missing():
    with pytest.raises(PrintError):
        parse_request_id("lp: error")


def test_parse_lpstat_jobs():
    out = (
        "TS6730-12              printbot         102400   Wed 22 Oct 2026 10:00:00 AM JST\n"
        "TS6730-13              printbot           2048   Wed 22 Oct 2026 10:01:00 AM JST\n"
    )
    jobs = parse_lpstat_jobs(out)
    assert [j.job_id for j in jobs] == ["TS6730-12", "TS6730-13"]
    assert jobs[0].size == "102400"


def test_normalize_job_id():
    c = Cups("TS6730")
    assert c.normalize_job_id("12") == "TS6730-12"
    assert c.normalize_job_id(" TS6730-12 ") == "TS6730-12"
