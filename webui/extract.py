"""Turn an uploaded file into plain text. PDF, DOCX, TXT/MD, and LaTeX (.tex)."""

from __future__ import annotations

import io
import re

SUPPORTED = (".pdf", ".docx", ".txt", ".md", ".tex")


class ExtractError(ValueError):
    pass


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise ExtractError("PDF support needs pypdf: pip install pypdf") from e
    reader = PdfReader(io.BytesIO(data))
    pages = [(p.extract_text() or "") for p in reader.pages]
    text = "\n".join(pages)
    if not text.strip():
        raise ExtractError("This PDF has no text layer (it may be a scan). Paste the text instead.")
    return text


def _para_text(p) -> str:
    """Text of one Word paragraph. Tabs and line breaks become spaces, so "London<tab>Apr 2025"
    reads as "London Apr 2025", not "LondonApr 2025" (one line per paragraph, as before)."""
    parts = []
    for n in p.iter():
        tag = n.tag.rsplit("}", 1)[-1]
        if tag == "t":
            parts.append(n.text or "")
        elif tag in ("tab", "br", "cr"):
            parts.append(" ")
    return "".join(parts)


def _docx(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as e:  # pragma: no cover
        raise ExtractError("Word support needs python-docx: pip install python-docx") from e
    doc = docx.Document(io.BytesIO(data))
    out: list[str] = []
    # Walk body in order so tables (person specs are usually tables) stay where they were.
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            txt = _para_text(child)
            if txt.strip():
                out.append(txt)
        elif tag == "tbl":
            for row in child.iter():
                if not row.tag.endswith("}tr"):
                    continue
                cells = []
                for cell in row.iterchildren():
                    if not cell.tag.endswith("}tc"):
                        continue
                    ctext = " ".join(_para_text(p) for p in cell.iter() if p.tag.endswith("}p")).strip()
                    cells.append(ctext)
                if any(cells):
                    out.append(" | ".join(cells))
    return "\n".join(out)


def _tex(data: bytes) -> str:
    t = data.decode("utf-8", errors="replace")
    t = re.sub(r"(?<!\\)%.*", "", t)                       # comments
    t = t.split("\\begin{document}", 1)[-1]
    t = re.sub(r"\\cventry\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}\{\}",
               r"\n\2 | \3 | \4 | \1\n", t)
    t = re.sub(r"\\section\{([^}]*)\}", r"\n\n## \1\n", t)
    t = re.sub(r"\\href\{([^}]*)\}\{([^}]*)\}", r"\2 (\1)", t)
    t = re.sub(r"\\item\s*", "\n- ", t)
    t = re.sub(r"\\textbf\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?", " ", t)     # remaining commands
    t = t.replace("\\&", "&").replace("\\%", "%").replace("\\$", "$").replace("\\#", "#")
    t = re.sub(r"[{}]", "", t)
    return re.sub(r"[ \t]+", " ", t)


def to_text(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        text = _pdf(data)
    elif name.endswith(".docx"):
        text = _docx(data)
    elif name.endswith(".tex"):
        text = _tex(data)
    elif name.endswith((".txt", ".md")):
        text = data.decode("utf-8", errors="replace")
    elif name.endswith(".doc"):
        raise ExtractError("Old .doc files are not supported. Save as .docx or PDF first.")
    else:
        raise ExtractError(f"Unsupported file type. Use one of: {', '.join(SUPPORTED)}")
    text = text.replace("\r\n", "\n").replace("\u00a0", " ")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
