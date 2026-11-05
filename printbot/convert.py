"""Convert attachments (images, Office documents, text) to PDF before printing.

PDF files pass through unchanged. Images are converted with Pillow, everything
else with LibreOffice in headless mode. The result is always validated by
``inspect_pdf`` afterwards, so this module only has to produce a PDF.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

from .pdf_utils import PdfError

log = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
# extension -> magic bytes the file must start with (None = no binary signature)
OFFICE_EXTS: dict[str, tuple[bytes, ...] | None] = {
    # Office Open XML / OpenDocument are zip files
    **{e: (b"PK\x03\x04",) for e in (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp")},
    # legacy Office binary (OLE2)
    **{e: (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",) for e in (".doc", ".xls", ".ppt")},
    ".rtf": (b"{\\rtf",),
}
TEXT_EXTS = {".txt", ".md", ".csv"}
SUPPORTED_EXTS = {".pdf"} | IMAGE_EXTS | set(OFFICE_EXTS) | TEXT_EXTS

SUPPORTED_LABEL = "PDF、画像（JPEG/PNG/GIF/BMP/WebP/TIFF）、Word/Excel/PowerPoint、OpenDocument、RTF、テキスト/CSV"

MAX_IMAGE_PIXELS = 60_000_000   # reject decompression bombs early
MAX_IMAGE_FRAMES = 50           # multi-page TIFF
CONVERT_TIMEOUT = 90            # seconds for one LibreOffice conversion

_soffice_lock = asyncio.Lock()   # one LibreOffice at a time (shared profile, bounded memory)


class ConvertError(PdfError):
    """Raised for files we cannot convert. The message is shown to the user."""


@dataclass(frozen=True)
class Converted:
    pdf: Path
    kind: str  # "PDF", "画像", "文書", "テキスト"


def detect_kind(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in OFFICE_EXTS:
        return "office"
    if ext in TEXT_EXTS:
        return "text"
    raise ConvertError(f"このファイル形式（{ext or '拡張子なし'}）には対応していません。対応形式: {SUPPORTED_LABEL}")


def _check_magic(path: Path, ext: str) -> None:
    magics = OFFICE_EXTS.get(ext)
    if not magics:
        return
    with path.open("rb") as f:
        head = f.read(16)
    if not head.startswith(magics):
        raise ConvertError(f"ファイルの中身が {ext} 形式ではありません")


def image_to_pdf(src: Path, out: Path) -> None:
    """Convert an image (all pages for TIFF, first frame otherwise) to a PDF."""
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(src) as im:
            frames = ImageSequence.all_frames(im) if im.format == "TIFF" else [im.copy()]
            if len(frames) > MAX_IMAGE_FRAMES:
                raise ConvertError(f"画像のページ数が多すぎます（上限 {MAX_IMAGE_FRAMES}）")
            pages = [_flatten(ImageOps.exif_transpose(f)) for f in frames]
    except ConvertError:
        raise
    except Image.DecompressionBombError:
        raise ConvertError("画像の解像度が大きすぎます")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as e:
        log.info("rejecting unreadable image: %s", e)
        raise ConvertError("画像を読み込めませんでした（壊れているか、対応していない形式です）")
    # 150 dpi gives a sensible physical size; CUPS fit-to-page scales it to the paper.
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=150.0)


def _flatten(im: Image.Image) -> Image.Image:
    """Return an RGB image, putting transparency on a white background."""
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp932", "euc_jp"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ConvertError("テキストの文字コードを判別できませんでした（UTF-8 か Shift_JIS で保存してください）")


async def _soffice(src: Path, outdir: Path, profile: Path, infilter: str | None) -> Path:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise ConvertError("サーバーに LibreOffice がないため、この形式は印刷できません（PDFで送ってください）")
    args = [
        exe, "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nologo",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
    ]
    if infilter:
        args.append(f"--infilter={infilter}")
    args += ["--convert-to", "pdf", "--outdir", str(outdir), str(src)]
    # A private HOME keeps LibreOffice from touching the service user's real home.
    env = {**os.environ, "HOME": str(profile), "SAL_USE_VCLPLUGIN": "svp"}
    async with _soffice_lock:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=CONVERT_TIMEOUT)
        except asyncio.TimeoutError:
            # soffice forks helpers, so kill the whole process group
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            raise ConvertError("ファイルの変換に時間がかかりすぎたため中止しました")
    pdf = outdir / (src.stem + ".pdf")
    if proc.returncode != 0 or not pdf.is_file():
        log.warning("soffice failed (%s): %s %s", proc.returncode,
                    out.decode(errors="replace").strip(), err.decode(errors="replace").strip())
        raise ConvertError("ファイルをPDFに変換できませんでした（壊れているか、パスワード付きの可能性があります）")
    return pdf


async def to_pdf(src: Path, filename: str, workdir: Path, profile: Path) -> Converted:
    """Turn the downloaded attachment ``src`` into a PDF inside ``workdir``."""
    kind = detect_kind(filename)
    ext = Path(filename).suffix.lower()
    if kind == "pdf":
        return Converted(src, "PDF")
    out = workdir / "converted.pdf"
    if kind == "image":
        await asyncio.to_thread(image_to_pdf, src, out)
        return Converted(out, "画像")

    outdir = workdir / "out"
    outdir.mkdir(exist_ok=True)
    if kind == "office":
        _check_magic(src, ext)
        staged = workdir / f"document{ext}"
        src.rename(staged)
        pdf = await _soffice(staged, outdir, profile, None)
        return Converted(pdf, "文書")

    # text / csv: normalise the encoding to UTF-8 so Japanese files do not come out garbled
    text = decode_text(src.read_bytes())
    staged = workdir / f"document{ext if ext == '.csv' else '.txt'}"
    staged.write_text(text, encoding="utf-8")
    infilter = "CSV:44,34,76" if ext == ".csv" else "Text (encoded):UTF8,LF,IPAGothic,ja-JP"
    pdf = await _soffice(staged, outdir, profile, infilter)
    return Converted(pdf, "テキスト")
