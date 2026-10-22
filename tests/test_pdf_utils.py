import pytest
from pypdf import PdfWriter

from printbot.pdf_utils import PdfError, inspect_pdf, parse_page_ranges


@pytest.mark.parametrize("spec,total,ranges,count", [
    (None, 10, None, 10),
    ("", 10, None, 10),
    ("3", 10, "3", 1),
    ("1-3,5", 10, "1-3,5", 4),
    ("5,1-3,2", 10, "1-3,5", 4),
    ("1-2,3-4", 10, "1-4", 4),
    (" 1 - 2 、 7 ", 10, "1-2,7", 3),
])
def test_parse_page_ranges(spec, total, ranges, count):
    sel = parse_page_ranges(spec, total)
    assert (sel.cups_ranges, sel.count) == (ranges, count)


@pytest.mark.parametrize("spec", ["0", "11", "3-1", "a", "1--2", "1,", "-3"])
def test_parse_page_ranges_rejects(spec):
    with pytest.raises(PdfError):
        parse_page_ranges(spec, 10)


def _pdf(tmp_path, pages=3, password=None):
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=595, height=842)
    if password:
        w.encrypt(password)
    p = tmp_path / "doc.pdf"
    with p.open("wb") as f:
        w.write(f)
    return p


def test_inspect_pdf_counts_pages(tmp_path):
    assert inspect_pdf(_pdf(tmp_path, pages=3)) == 3


def test_inspect_pdf_rejects_non_pdf(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"MZ\x90\x00 not a pdf")
    with pytest.raises(PdfError):
        inspect_pdf(p)


def test_inspect_pdf_rejects_broken(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.7\ngarbage")
    with pytest.raises(PdfError):
        inspect_pdf(p)


def test_inspect_pdf_rejects_password(tmp_path):
    with pytest.raises(PdfError):
        inspect_pdf(_pdf(tmp_path, password="secret"))
