"""Local page rendering and exact-byte snapshots for application exports."""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import Lock
from uuid import uuid4

from pypdf import PdfReader
from pypdf.errors import PdfReadError

import ats

MAX_PAGES = 8
_conversion_lock = Lock()
_snapshot_lock = Lock()


@dataclass(frozen=True)
class Snapshot:
    name: str
    file: bytes
    layout: bytes | None
    pages: tuple[bytes, ...]


_snapshots: OrderedDict[tuple[str, str], Snapshot] = OrderedDict()


def snapshot(slug: str, token: str) -> Snapshot | None:
    with _snapshot_lock:
        return _snapshots.get((slug, token))


@lru_cache(maxsize=3)
def _word_layout(data: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="cv-preview-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        source, output = root / "cv.docx", root / "cv.pdf"
        source.write_bytes(data)
        office = _office_cli()
        if not office:
            raise ValueError("Word page rendering and fresh PDF exports need LibreOffice.")
        command = [office, f"-env:UserInstallation={(root / 'profile').as_uri()}", "--headless",
                   "--convert-to", "pdf", "--outdir", str(root), str(source)]
        try:
            subprocess.run(command, capture_output=True, timeout=35, check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return output.read_bytes()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("The local Word renderer could not create a PDF preview. Open the Word download to check its layout.") from exc


def _office_cli() -> str | None:
    if command := shutil.which("soffice") or shutil.which("libreoffice"):
        return command
    local = Path(__file__).with_name(".venv") / "office-renderer"
    roots = [local / "LibreOffice", local / "Program Files" / "LibreOffice", local]
    if os.environ.get("ProgramFiles"):
        roots.append(Path(os.environ["ProgramFiles"]) / "LibreOffice")
    return next((str(root / "program" / "soffice.com") for root in roots
                 if (root / "program" / "soffice.com").is_file()), None)


def _images(layout: bytes, count: int) -> tuple[bytes, ...]:
    poppler = shutil.which("pdftoppm")
    if not poppler or not count:
        return ()
    with tempfile.TemporaryDirectory(prefix="cv-pages-") as temp:
        root = Path(temp)
        pdf = root / "cv.pdf"
        pdf.write_bytes(layout)
        try:
            subprocess.run([poppler, "-l", str(min(count, MAX_PAGES)), "-scale-to", "1150",
                            "-png", str(pdf), str(root / "page")], capture_output=True, timeout=20,
                           check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            paths = sorted(root.glob("page-*.png"), key=lambda path: int(path.stem.split("-")[-1]))
            return tuple(path.read_bytes() for path in paths)
        except (OSError, subprocess.SubprocessError):
            return ()


def build(slug: str, name: str, data: bytes, *, convert: bool = False) -> dict:
    is_word = name.lower().endswith(".docx")
    layout, layout_note, layout_engine = (None if is_word else data), "", ""
    if is_word:
        try:
            with _conversion_lock:
                layout = _word_layout(data)
            layout_engine = "LibreOffice"
        except ValueError as exc:
            if convert:
                raise
            layout_note = str(exc)
    if convert and layout:
        name = Path(name).with_suffix(".pdf").name
        data, is_word = layout, False

    try:
        text, risks, kind = ats.read_cv(name, data)
    except PdfReadError as exc:
        raise ValueError("The selected PDF could not be read. Generate a fresh export from Word.") from exc
    per_page = []
    if layout:
        reader = PdfReader(BytesIO(layout))
        per_page = [(page.extract_text() or "").strip() for page in reader.pages]
        if len(per_page) > 2:
            layout_note = f"This export runs to {len(per_page)} pages. Check the final page for spillover."
        elif any(len(page.split()) < 25 for page in per_page):
            layout_note = "One page contains little extractable text. Check for spillover or hidden text."
        if is_word:
            words = Counter(re.findall(r"\w+", text.casefold()))
            visible = Counter(re.findall(r"\w+", " ".join(per_page).casefold()))
            if sum((words - visible).values()) >= max(8, sum(words.values()) * .10):
                layout_note = (layout_note + " Check for missing or clipped text. The page rendering contains fewer words "
                               "than the Word document.").strip()
    parse_checks, score = ats.parse_checks(ats.parse(text), risks)
    pages = _images(layout, len(per_page)) if layout else ()
    entry = Snapshot(name, data, layout, pages)
    token = uuid4().hex
    with _snapshot_lock:
        _snapshots[(slug, token)] = entry
        while len(_snapshots) > 6:
            _snapshots.popitem(last=False)
    return {
        "token": token, "name": name, "kind": kind, "text": text, "parse_score": score,
        "checks": [check for check in parse_checks if not check["ok"]],
        "page_count": len(per_page), "image_count": len(pages), "has_layout": bool(layout),
        "layout_note": layout_note, "layout_engine": layout_engine, "page_text": per_page if not is_word else [],
    }
