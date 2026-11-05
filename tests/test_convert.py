import asyncio
import shutil
import subprocess

import pytest
from PIL import Image

from printbot.convert import ConvertError, decode_text, detect_kind, image_to_pdf, to_pdf
from printbot.pdf_utils import PdfError, inspect_pdf

needs_soffice = pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice not installed")


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("name,kind", [
    ("a.pdf", "pdf"), ("A.PDF", "pdf"), ("p.JPG", "image"), ("p.png", "image"), ("s.tiff", "image"),
    ("r.docx", "office"), ("r.xlsx", "office"), ("r.ppt", "office"), ("r.odt", "office"),
    ("n.txt", "text"), ("d.csv", "text"),
])
def test_detect_kind(name, kind):
    assert detect_kind(name) == kind


@pytest.mark.parametrize("name", ["a.exe", "a.zip", "noext", "a.html", "a.heic"])
def test_detect_kind_rejects(name):
    with pytest.raises(ConvertError):
        detect_kind(name)


def test_convert_error_is_pdf_error():
    # bot.py catches PdfError for everything shown to the user
    assert issubclass(ConvertError, PdfError)


def test_image_to_pdf_with_transparency(tmp_path):
    src, out = tmp_path / "in.png", tmp_path / "out.pdf"
    Image.new("RGBA", (300, 200), (255, 0, 0, 128)).save(src)
    image_to_pdf(src, out)
    assert inspect_pdf(out) == 1


def test_multipage_tiff(tmp_path):
    src, out = tmp_path / "in.tiff", tmp_path / "out.pdf"
    frames = [Image.new("L", (100, 100), c) for c in (0, 128, 255)]
    frames[0].save(src, save_all=True, append_images=frames[1:])
    image_to_pdf(src, out)
    assert inspect_pdf(out) == 3


def test_broken_image(tmp_path):
    src = tmp_path / "in.jpg"
    src.write_bytes(b"\xff\xd8\xff not really a jpeg")
    with pytest.raises(ConvertError):
        image_to_pdf(src, tmp_path / "out.pdf")


def test_image_bomb(tmp_path, monkeypatch):
    monkeypatch.setattr("printbot.convert.MAX_IMAGE_PIXELS", 1000)
    src = tmp_path / "in.png"
    Image.new("L", (100, 100)).save(src)  # 10000 px > 2 * limit
    with pytest.raises(ConvertError):
        image_to_pdf(src, tmp_path / "out.pdf")


def test_decode_text():
    assert decode_text("日本語".encode("utf-8")) == "日本語"
    assert decode_text("﻿日本語".encode("utf-8")) == "日本語"
    assert decode_text("日本語".encode("cp932")) == "日本語"


def test_pdf_passthrough(tmp_path):
    src = tmp_path / "upload"
    src.write_bytes(b"%PDF-1.4")
    assert _run(to_pdf(src, "x.pdf", tmp_path, tmp_path / "lo")).pdf == src


def test_office_magic_mismatch(tmp_path):
    src = tmp_path / "upload"
    src.write_bytes(b"MZ this is an exe renamed to docx")
    with pytest.raises(ConvertError, match="docx"):
        _run(to_pdf(src, "evil.docx", tmp_path, tmp_path / "lo"))


@needs_soffice
def test_text_to_pdf_japanese(tmp_path):
    src = tmp_path / "upload"
    src.write_bytes("こんにちは、印刷テスト\n2行目".encode("cp932"))
    result = _run(to_pdf(src, "memo.txt", tmp_path, tmp_path / "lo"))
    assert inspect_pdf(result.pdf) == 1
    text = subprocess.run(["pdftotext", str(result.pdf), "-"], capture_output=True, text=True).stdout
    assert "こんにちは" in text


@needs_soffice
def test_docx_to_pdf(tmp_path):
    # build a real .docx with LibreOffice itself
    seed = tmp_path / "seed.txt"
    seed.write_text("docx test", encoding="utf-8")
    subprocess.run(["soffice", "--headless", f"-env:UserInstallation={(tmp_path / 'seedlo').as_uri()}",
                    "--convert-to", "docx", "--outdir", str(tmp_path), str(seed)],
                   check=True, capture_output=True, timeout=120)
    work = tmp_path / "work"
    work.mkdir()
    src = work / "upload"
    (tmp_path / "seed.docx").rename(src)
    result = _run(to_pdf(src, "report.docx", work, tmp_path / "lo"))
    assert inspect_pdf(result.pdf) == 1


@needs_soffice
def test_csv_to_pdf(tmp_path):
    src = tmp_path / "upload"
    src.write_bytes("名前,点数\n佐藤,90\n".encode("utf-8"))
    result = _run(to_pdf(src, "score.csv", tmp_path, tmp_path / "lo"))
    assert inspect_pdf(result.pdf) >= 1
