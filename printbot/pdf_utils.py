from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

log = logging.getLogger(__name__)

_RANGE_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")


class PdfError(Exception):
    """Raised for PDFs we refuse to print. The message is shown to the user."""


@dataclass(frozen=True)
class PageSelection:
    cups_ranges: str | None  # value for `lp -P`, None = all pages
    count: int


def inspect_pdf(path: Path) -> int:
    """Validate the file and return its page count."""
    with path.open("rb") as f:
        head = f.read(1024)
    if b"%PDF-" not in head:
        raise PdfError("PDFファイルではありません")
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfError("パスワード付きPDFは印刷できません")
        pages = len(reader.pages)
    except PdfError:
        raise
    except (PdfReadError, ValueError, OSError, KeyError) as e:
        log.info("rejecting unreadable pdf: %s", e)
        raise PdfError("PDFを読み込めませんでした（壊れている可能性があります）")
    if pages < 1:
        raise PdfError("ページがありません")
    return pages


def parse_page_ranges(spec: str | None, total: int) -> PageSelection:
    """Parse "1-3,5" into a merged CUPS page-ranges string and a page count."""
    if spec is None or not spec.strip():
        return PageSelection(None, total)
    spec = spec.replace(" ", "").replace("、", ",").replace("，", ",")
    if not _RANGE_RE.match(spec):
        raise PdfError("ページ範囲の書式が不正です（例: 1-3,5）")

    pages: set[int] = set()
    for part in spec.split(","):
        start, _, end = part.partition("-")
        lo, hi = int(start), int(end or start)
        if lo < 1 or hi > total or lo > hi:
            raise PdfError(f"ページ範囲 {part} が不正です（1〜{total}ページ）")
        pages.update(range(lo, hi + 1))

    ordered = sorted(pages)
    merged: list[str] = []
    run_start = prev = ordered[0]
    for p in ordered[1:] + [None]:
        if p is not None and p == prev + 1:
            prev = p
            continue
        merged.append(str(run_start) if run_start == prev else f"{run_start}-{prev}")
        if p is not None:
            run_start = prev = p
    return PageSelection(",".join(merged), len(ordered))


async def render_preview(pdf: Path, page: int, out_png: Path) -> Path | None:
    """Render one page to PNG with pdftoppm. Returns None on failure (preview is optional)."""
    prefix = out_png.with_suffix("")
    try:
        proc = await asyncio.create_subprocess_exec(
            "pdftoppm", "-png", "-singlefile", "-f", str(page), "-l", str(page),
            "-scale-to", "800", str(pdf), str(prefix),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except FileNotFoundError:
        log.warning("pdftoppm not found; install poppler-utils for previews")
        return None
    except asyncio.TimeoutError:
        proc.kill()
        log.warning("pdftoppm timed out")
        return None
    if proc.returncode != 0 or not out_png.exists():
        log.warning("pdftoppm failed: %s", err.decode(errors="replace").strip())
        return None
    return out_png
