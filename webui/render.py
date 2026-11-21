"""Render the tailored CV JSON into files: Word (.docx), LaTeX (.tex, the repo's moderncv
banking template), Markdown, and an optional supporting statement (.docx).

The .docx is single-column with real text for every contact detail, which is what UK
ATSs (and NHS Jobs / Civil Service Jobs uploads) parse best.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from store import CV_DIR, REPO, WINDOWS_PATH_BUDGET, windows_path_length

HEADINGS = {
    "summary": "Profile", "achievements": "Key Achievements", "skills": "Core Competencies", "experience": "Professional Experience",
    "projects": "Selected Projects", "education": "Education", "certifications": "Certifications",
    "extras": None,
}


def _hex(color: str) -> tuple[int, int, int]:
    c = (color or "#1f4e79").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
        c = "1f4e79"
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _visible(cv: dict, key: str) -> bool:
    return bool(cv.get(key)) and key not in (cv.get("hidden_sections") or [])


# ----------------------------------------------------------------------------- DOCX

# Word designs. All are ATS-safe: one column, real text (no tables, text boxes or images), standard
# headings, contact details in the body, and body text never below 10pt.
TEMPLATES = {
    "classic": {"label": "Classic", "margin": (1.6, 1.8), "body": 10.5, "name": 22, "name_align": "left",
                "head": 11, "head_upper": True, "head_style": "rule", "space_before": 10, "role_gap": 7},
    "modern": {"label": "Modern", "margin": (1.5, 1.9), "body": 10.5, "name": 26, "name_align": "center",
               "head": 12, "head_upper": False, "head_style": "bar", "space_before": 12, "role_gap": 8},
    "compact": {"label": "Compact (fits more on two pages)", "margin": (1.2, 1.4), "body": 10, "name": 19,
                "name_align": "left", "head": 10.5, "head_upper": True, "head_style": "rule", "space_before": 6,
                "role_gap": 3},
    "elegant": {"label": "Elegant (serif)", "margin": (1.7, 2.0), "body": 10.5, "name": 24, "name_align": "center",
                "head": 11, "head_upper": True, "head_style": "rule", "space_before": 11, "role_gap": 7,
                "font": "Georgia", "spacing": 1.15},
    "minimal": {"label": "Minimal", "margin": (1.6, 2.0), "body": 10.5, "name": 24, "name_align": "left",
                "head": 11.5, "head_upper": False, "head_style": "none", "space_before": 12, "role_gap": 7,
                "spacing": 1.15},
}

# Fonts that every Windows PC and Word install has, and that ATSs read cleanly.
FONTS = ["Calibri", "Arial", "Cambria", "Georgia", "Garamond", "Verdana"]
DESIGN_LIMITS = {"size": (10.0, 12.0), "spacing": (1.0, 1.4), "margin": (1.2, 2.5), "gap": (4.0, 16.0)}


def clean_design(raw) -> dict | None:
    """Your own settings from the Design panel, clamped to sizes that stay readable and ATS-safe."""
    if not isinstance(raw, dict):
        return None
    out = {}
    if raw.get("font") in FONTS:
        out["font"] = raw["font"]
    for k, (lo, hi) in DESIGN_LIMITS.items():
        try:
            v = float(raw[k])
        except (KeyError, TypeError, ValueError):
            continue
        out[k] = round(min(hi, max(lo, v)), 2)
    return out or None


def design_for(template: str, design: dict | None) -> dict:
    """The template's settings with your own font, size, spacing, margins and section gap on top."""
    t = dict(TEMPLATES.get(template) or TEMPLATES["classic"])
    t.setdefault("font", "Calibri")
    t.setdefault("spacing", 1.1)
    d = clean_design(design) or {}
    if "font" in d:
        t["font"] = d["font"]
    if "size" in d:
        grow = d["size"] - t["body"]
        t["body"], t["head"], t["name"] = d["size"], t["head"] + grow / 2, t["name"] + grow
    if "spacing" in d:
        t["spacing"] = d["spacing"]
    if "margin" in d:
        t["margin"] = (round(d["margin"] * 0.9, 2), d["margin"])
    if "gap" in d:
        t["space_before"], t["role_gap"] = d["gap"], max(2.0, round(d["gap"] * 0.65, 1))
    return t


def to_docx(cv: dict, path: Path, accent: str = "#1f4e79", template: str = "classic", design: dict | None = None) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor, Cm

    t = design_for(template, design)
    rgb = RGBColor(*_hex(accent))
    doc = Document()
    for s in doc.sections:
        s.page_width, s.page_height = Cm(21.0), Cm(29.7)  # A4: python-docx starts on US Letter
        s.top_margin = s.bottom_margin = Cm(t["margin"][0])
        s.left_margin = s.right_margin = Cm(t["margin"][1])
    base = doc.styles["Normal"]
    base.font.name = t["font"]
    base.element.rPr.rFonts.set(qn("w:eastAsia"), t["font"])
    base.font.size = Pt(t["body"])
    base.paragraph_format.space_after = Pt(2)
    base.paragraph_format.line_spacing = t["spacing"]
    ink = RGBColor(0x1F, 0x23, 0x28)
    muted = RGBColor(0x55, 0x5B, 0x63)
    base.font.color.rgb = ink
    lb = doc.styles["List Bullet"].paragraph_format  # tight hanging bullets, like Enhancv
    lb.left_indent, lb.first_line_indent = Cm(0.45), Cm(-0.35)
    lb.space_after = Pt(1.5)
    usable = 21.0 - 2 * t["margin"][1]  # A4 width minus margins, for the right-aligned date tab

    def rule(par):
        p_pr = par._p.get_or_add_pPr()
        bdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"),
                     ("w:color", "%02X%02X%02X" % _hex(accent))):
            bottom.set(qn(k), v)
        bdr.append(bottom)
        p_pr.append(bdr)

    def bar(par):
        p_pr = par._p.get_or_add_pPr()
        bdr = OxmlElement("w:pBdr")
        left = OxmlElement("w:left")
        for k, v in (("w:val", "single"), ("w:sz", "24"), ("w:space", "6"),
                     ("w:color", "%02X%02X%02X" % _hex(accent))):
            left.set(qn(k), v)
        bdr.append(left)
        p_pr.append(bdr)

    def heading(text):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(t["space_before"])
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.keep_with_next = True
        r = p.add_run(text.upper() if t["head_upper"] else text)
        r.bold = True
        r.font.size = Pt(t["head"])
        r.font.color.rgb = rgb
        if t["head_style"] == "bar":
            bar(p)
        elif t["head_style"] == "rule":
            rule(p)

    def bullet(text):
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(1)
        p.add_run(text)

    center = t["name_align"] == "center"
    name = doc.add_paragraph()
    r = name.add_run(cv.get("name") or "Your Name")
    r.bold = True
    r.font.size = Pt(t["name"])
    r.font.color.rgb = ink
    name.paragraph_format.space_after = Pt(0)
    name.paragraph_format.line_spacing = 1.0
    if center:
        name.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if cv.get("subtitle"):
        p = doc.add_paragraph()
        rr = p.add_run(cv["subtitle"])
        rr.font.size = Pt(12)
        rr.font.color.rgb = rgb  # the headline is the first thing read after the name
        p.paragraph_format.space_after = Pt(3)
        if center:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    c = cv.get("contact", {})
    bits = [c.get("location"), c.get("phone"), c.get("email")]
    bits += [l.get("url") or l.get("label") for l in c.get("links", [])]
    p = doc.add_paragraph("  •  ".join(b for b in bits if b))
    if p.runs:
        p.runs[0].font.size = Pt(max(9.5, t["body"] - 1))
        p.runs[0].font.color.rgb = muted
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    for key in cv.get("section_order", HEADINGS.keys()):
        if not _visible(cv, key):
            continue
        if key == "summary":
            heading(HEADINGS["summary"])
            doc.add_paragraph(" ".join(s.get("text", "") for s in cv["summary"]))
        elif key == "achievements":
            heading(HEADINGS["achievements"])
            for a in cv["achievements"]:
                bullet(a.get("text", ""))
        elif key == "skills":
            heading(HEADINGS["skills"])
            for k in cv["skills"]:
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(2)
                p.add_run(f"{k.get('category', '').strip()}: ").bold = True
                p.add_run(", ".join(k.get("items", [])))
        elif key == "experience":
            heading(HEADINGS["experience"])
            for e in cv["experience"]:
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(t["role_gap"])
                p.paragraph_format.keep_with_next = True
                tr = p.add_run(e.get("title", ""))
                tr.bold = True
                tr.font.size = Pt(t["body"] + 0.5)
                co = p.add_run(f"  |  {e.get('company', '')}" + (f", {e['location']}" if e.get("location") else ""))
                co.font.color.rgb = rgb
                tab = p.add_run(f"\t{e.get('dates', '')}")
                tab.font.color.rgb = muted
                p.paragraph_format.tab_stops.add_tab_stop(Cm(usable), alignment=2)
                for b in e["bullets"]:
                    bullet(b.get("text", ""))
        elif key == "projects":
            heading(HEADINGS["projects"])
            for pr in cv["projects"]:
                p = doc.add_paragraph()
                p.paragraph_format.keep_with_next = True
                p.add_run(pr.get("name", "")).bold = True
                if pr.get("stack"):
                    p.add_run(f"  |  {pr['stack']}").italic = True
                for b in pr["bullets"]:
                    bullet(b.get("text", ""))
        elif key == "education":
            heading(HEADINGS["education"])
            for ed in cv["education"]:
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(2)
                p.add_run(ed.get("qualification", "")).bold = True
                p.add_run(f"  |  {ed.get('institution', '')}").font.color.rgb = rgb
                p.add_run(f"\t{ed.get('dates', '')}").font.color.rgb = muted
                p.paragraph_format.tab_stops.add_tab_stop(Cm(usable), alignment=2)
                if ed.get("detail"):
                    dp = doc.add_paragraph()
                    dr = dp.add_run(ed["detail"])
                    dr.font.color.rgb = muted
                    dr.font.size = Pt(max(9.5, t["body"] - 0.5))
        elif key == "certifications":
            heading(HEADINGS["certifications"])
            for ce in cv["certifications"]:
                bullet(ce.get("text", ""))
        elif key == "extras":
            for x in cv["extras"]:
                if not x.get("items"):
                    continue
                heading(x.get("heading", "Additional"))
                for it in x["items"]:
                    bullet(it.get("text", ""))
    doc.core_properties.title = f"{cv.get('name', '')} - CV"
    doc.core_properties.author = cv.get("name", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)


def statement_docx(draft: dict, path: Path) -> None:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    t = draft.get("target", {})
    doc.add_heading(f"Supporting statement: {t.get('role', '')}, {t.get('company', '')}", level=1)
    reqs = {r["id"]: r for r in draft.get("requirements", [])}
    for para in draft.get("statement", []):
        crit = ", ".join(reqs[c]["requirement"] for c in para.get("criteria", []) if c in reqs)
        doc.add_heading(para.get("heading") or crit or "Criterion", level=2)
        doc.add_paragraph(para.get("text", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)


def cover_docx(draft: dict, path: Path, accent: str = "#1f4e79") -> None:
    from datetime import date
    from docx import Document
    from docx.shared import Cm, Pt, RGBColor

    cl, cv = draft["cover_letter"], draft["cv"]
    doc = Document()
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Cm(2.0)
        s.left_margin = s.right_margin = Cm(2.2)
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    doc.styles["Normal"].paragraph_format.space_after = Pt(8)
    p = doc.add_paragraph()
    r = p.add_run(cv.get("name", ""))
    r.bold = True
    r.font.size = Pt(18)
    r.font.color.rgb = RGBColor(*_hex(accent))
    p.paragraph_format.space_after = Pt(0)
    c = cv.get("contact", {})
    doc.add_paragraph("  |  ".join(x for x in (c.get("email"), c.get("phone"), c.get("location")) if x))
    t = draft.get("target", {})
    doc.add_paragraph(date.today().strftime("%d %B %Y").lstrip("0"))
    if t.get("company"):
        doc.add_paragraph(f"{t['company']}" + (f"\nRe: {t.get('role', '')}" if t.get("role") else "")
                          + (f" (ref {t['reference']})" if t.get("reference") else ""))
    doc.add_paragraph(cl.get("greeting") or "Dear Hiring Manager,")
    after = cl.get("bullets_after")
    for i, para in enumerate(cl["paragraphs"]):
        doc.add_paragraph(para.get("text", ""))
        if i == after:
            for b in cl["bullets"]:
                bp = doc.add_paragraph(style="List Bullet")
                bp.paragraph_format.space_after = Pt(2)
                bp.add_run(b.get("text", ""))
    if after is None or after >= len(cl["paragraphs"]):
        for b in cl["bullets"]:
            doc.add_paragraph(b.get("text", ""), style="List Bullet")
    if cl.get("closing"):
        doc.add_paragraph(cl["closing"])
    doc.add_paragraph(cl.get("sign_off") or "Kind regards,")
    doc.add_paragraph(cv.get("name", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)


def cover_latex(draft: dict) -> str:
    """The repo's cover.cls structure (compile with xelatex in cover_letters/)."""
    cl, cv = draft["cover_letter"], draft["cv"]
    c = cv.get("contact", {})
    link = next((l for l in c.get("links", []) if l.get("url")), None)
    head = " | ".join(x for x in (
        f"\\href{{mailto:{c['email']}}}{{{tex(c['email'])}}}" if c.get("email") else "",
        tex(c.get("phone", "")),
        f"\\urlstyle{{same}}\\href{{{link['url']}}}{{{tex(link.get('label') or 'LinkedIn')}}}" if link else "") if x)
    out = ["%% Cover letter - generated by the web UI. Compile in cover_letters/ with: xelatex <file>",
           "\\documentclass[]{cover}", "\\usepackage{fancyhdr}", "\\pagestyle{fancy}", "\\fancyhf{}",
           "\\rfoot{Page \\thepage \\hspace{0pt}}", "\\thispagestyle{empty}",
           "\\renewcommand{\\headrulewidth}{0pt}", "\\begin{document}",
           f"\\namesection{{}}{{\\Huge{{{tex(cv.get('name', ''))}}}}}{{ {head}\n}}",
           "\\currentdate{\\today}", f"\\lettercontent{{{tex(cl.get('greeting') or 'Dear Hiring Manager,')}}}"]
    after = cl.get("bullets_after")
    for i, para in enumerate(cl["paragraphs"]):
        out.append(f"\\lettercontent{{{tex(para.get('text', ''))}}}")
        if i == after and cl["bullets"]:
            out += ["{\\raggedright\\fontspec[Path = OpenFonts/fonts/raleway/]{Raleway-Medium}"
                    "\\fontsize{11pt}{13pt}\\selectfont", "\\begin{itemize}"]
            out += [f"    \\item {{{tex(b.get('text', ''))}}}" for b in cl["bullets"]]
            out += ["\\end{itemize}\\par}", "\\vspace{6pt}"]
    if cl.get("closing"):
        out.append(f"\\lettercontent{{{tex(cl['closing'])}}}")
    out += ["\\begin{flushright}", f"\\closing{{{tex(cl.get('sign_off') or 'Kind regards,')}}}",
            f"\\signature{{{tex(cv.get('name', ''))}}}", "\\end{flushright}", "\\end{document}", ""]
    return "\n".join(out)


# ----------------------------------------------------------------------------- Markdown

def to_markdown(cv: dict) -> str:
    out = [f"# {cv.get('name', '')}"]
    if cv.get("subtitle"):
        out.append(f"**{cv['subtitle']}**")
    c = cv.get("contact", {})
    bits = [c.get("location"), c.get("phone"), c.get("email")] + [
        l.get("url") for l in c.get("links", [])]
    out.append(" | ".join(b for b in bits if b))
    for key in cv.get("section_order", HEADINGS.keys()):
        if not _visible(cv, key):
            continue
        if key == "summary":
            out += ["", "## Profile", " ".join(s.get("text", "") for s in cv["summary"])]
        elif key == "achievements":
            out += ["", "## Key Achievements"] + [f"- {a.get('text', '')}" for a in cv["achievements"]]
        elif key == "skills":
            out += ["", "## Core Competencies"] + [
                f"- **{k.get('category', '')}:** {', '.join(k.get('items', []))}" for k in cv["skills"]]
        elif key == "experience":
            out += ["", "## Professional Experience"]
            for e in cv["experience"]:
                out += ["", f"### {e.get('title', '')}, {e.get('company', '')} ({e.get('dates', '')})"]
                out += [f"- {b.get('text', '')}" for b in e["bullets"]]
        elif key == "projects":
            out += ["", "## Selected Projects"]
            for p in cv["projects"]:
                out += ["", f"### {p.get('name', '')}" + (f" | {p['stack']}" if p.get("stack") else "")]
                out += [f"- {b.get('text', '')}" for b in p["bullets"]]
        elif key == "education":
            out += ["", "## Education"] + [
                f"- **{e.get('qualification', '')}**, {e.get('institution', '')} ({e.get('dates', '')})"
                + (f". {e['detail']}" if e.get("detail") else "") for e in cv["education"]]
        elif key == "certifications":
            out += ["", "## Certifications"] + [f"- {x.get('text', '')}" for x in cv["certifications"]]
        elif key == "extras":
            for x in cv["extras"]:
                if x.get("items"):
                    out += ["", f"## {x.get('heading', '')}"] + [f"- {i.get('text', '')}" for i in x["items"]]
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------- LaTeX

_TEX = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}", "{": r"\{", "}": r"\}", "\\": r"\textbackslash{}",
        "\u2013": "-", "\u2014": ", ", "|": r"\textbar{}"}


def tex(s: str) -> str:
    return "".join(_TEX.get(ch, ch) for ch in (s or ""))


PREAMBLE = r"""%% Tailored CV - generated by the web UI (webui/). Compile with: lualatex %(file)s
\documentclass[11pt,a4paper,sans]{moderncv}
\moderncvstyle{banking}
\moderncvcolor{blue}
\renewcommand*{\namefont}{\fontsize{34}{36}\bfseries\upshape}
\colorlet{firstnamecolor}{color1}
\colorlet{lastnamecolor}{color1}
\colorlet{namecolor}{color1}
\renewcommand*{\sectionstyle}[1]{{\sectionfont\color{color1}#1}}
\usepackage[utf8]{inputenc}
\ifpdftex\usepackage[T1]{fontenc}\fi
\AtEndPreamble{\hypersetup{colorlinks=true, linkcolor=blue, urlcolor=blue,
    pdftitle={%(name)s - CV}, pdfpagemode=UseNone}}
\usepackage[scale=0.80]{geometry}
\usepackage{needspace}
"""


def to_latex(cv: dict, filename: str) -> str:
    name = cv.get("name") or "Your Name"
    first, _, last = name.partition(" ")
    c = cv.get("contact", {})
    lines = [PREAMBLE % {"file": filename, "name": tex(name)},
             f"\\name{{{tex(first)}}}{{{tex(last)}}}"]
    if c.get("location"):
        lines.append(f"\\address{{{tex(c['location'])}}}{{}}{{}}")
    if c.get("phone"):
        lines.append(f"\\phone[mobile]{{{tex(c['phone'])}}}")
    if c.get("email"):
        lines.append(f"\\email{{{tex(c['email'])}}}")
    links = [l for l in c.get("links", []) if l.get("url")]
    if links:
        lines.append("\\extrainfo{" + ", ".join(
            f"\\href{{{l['url']}}}{{{tex(l.get('label') or l['url'])}}}" for l in links) + "}")
    lines += ["", "\\begin{document}", "\\makecvtitle", ""]
    if cv.get("subtitle"):
        lines.append(f"{{\\large\\textbf{{{tex(cv['subtitle'])}}}}}\\par\\vspace{{4pt}}")

    def items(rows):
        return ["\\begin{itemize}"] + [f"\\item {{{tex(r)}}}" for r in rows] + ["\\end{itemize}"]

    for key in cv.get("section_order", HEADINGS.keys()):
        if not _visible(cv, key):
            continue
        if key == "summary":
            lines += ["\\vspace{6pt}", "\\small{" + tex(" ".join(s.get("text", "") for s in cv["summary"])) + "}", ""]
        elif key == "achievements":
            lines += ["\\section{Key Achievements}", "\\vspace{1pt}"] + items([a.get("text", "") for a in cv["achievements"]]) + [""]
        elif key == "skills":
            lines += ["\\section{Core Competencies}", "\\vspace{1pt}", "\\begin{itemize}"]
            lines += [f"\\item \\textbf{{{tex(k.get('category', ''))}}}: {tex(', '.join(k.get('items', [])))}"
                      for k in cv["skills"]]
            lines += ["\\end{itemize}", ""]
        elif key == "experience":
            lines += ["\\section{Professional Experience}", "\\vspace{3pt}", "\\begin{itemize}"]
            for e in cv["experience"]:
                lines.append("\\needspace{5\\baselineskip}")
                lines.append(
                    f"\\item{{\\cventry{{{tex(e.get('dates', ''))}}}{{{tex(e.get('title', ''))}}}"
                    f"{{{tex(e.get('company', ''))}}}{{{tex(e.get('location', ''))}}}{{}}{{\\vspace{{1pt}}")
                lines += items([b.get("text", "") for b in e["bullets"]]) + ["}}", "\\vspace{3pt}"]
            lines += ["\\end{itemize}", ""]
        elif key == "projects":
            lines += ["\\section{Selected Projects}", "\\vspace{1pt}", "\\begin{itemize}"]
            for p in cv["projects"]:
                lines.append(f"\\item{{\\cventry{{}}{{{tex(p.get('name', ''))}}}{{{tex(p.get('stack', ''))}}}{{}}{{}}{{\\vspace{{1pt}}")
                lines += items([b.get("text", "") for b in p["bullets"]]) + ["}}"]
            lines += ["\\end{itemize}", ""]
        elif key == "education":
            lines += ["\\section{Education}", "\\vspace{1pt}", "\\begin{itemize}"]
            for ed in cv["education"]:
                lines.append(
                    f"\\item{{\\cventry{{{tex(ed.get('dates', ''))}}}{{{tex(ed.get('qualification', ''))}}}"
                    f"{{{tex(ed.get('institution', ''))}}}{{{tex(ed.get('location', ''))}}}{{}}{{{tex(ed.get('detail', ''))}}}}}")
            lines += ["\\end{itemize}", ""]
        elif key == "certifications":
            lines += ["\\section{Certifications}", "\\vspace{1pt}"]
            lines += items([x.get("text", "") for x in cv["certifications"]]) + [""]
        elif key == "extras":
            for x in cv["extras"]:
                if x.get("items"):
                    lines += [f"\\section{{{tex(x.get('heading', ''))}}}", "\\vspace{1pt}"]
                    lines += items([i.get("text", "") for i in x["items"]]) + [""]
    lines += ["\\section{References}", "\\vspace{1pt}", "\\begin{itemize}",
              "\\item{Available upon request.}", "\\end{itemize}", "", "\\end{document}", ""]
    return "\n".join(lines)


def compile_pdf(tex_path: Path) -> dict:
    """lualatex like /apply Step 5a, then the repo's verify_pdf.py page count. Best effort."""
    exe = shutil.which("lualatex")
    if not exe:
        return {"ok": False, "message": "lualatex not found. Install MiKTeX or TeX Live, or use the Word file."}
    try:
        for _ in range(2):  # second pass settles needspace/refs
            proc = subprocess.run([exe, "-interaction=nonstopmode", tex_path.name], cwd=tex_path.parent,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=240)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "lualatex timed out"}
    pdf = tex_path.with_suffix(".pdf")
    for ext in (".aux", ".log", ".out", ".fls", ".fdb_latexmk", ".synctex.gz"):
        try:
            tex_path.with_suffix(ext).unlink()
        except OSError:
            pass
    if not pdf.exists():
        tail = "\n".join(l for l in proc.stdout.splitlines() if l.startswith("!"))[:600]
        return {"ok": False, "message": "Compile failed. " + (tail or "See the .tex for errors.")}
    pages = None
    verify = REPO / "tools" / "verify_pdf.py"
    if verify.exists():
        v = subprocess.run([sys.executable, str(verify), str(pdf), "--pages", "2", "--ascii-dates"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        msg = (v.stdout + v.stderr).strip()
        m = re.search(r"(\d+)\s+page", msg)
        pages = int(m.group(1)) if m else None
        return {"ok": v.returncode == 0, "pdf": pdf.name, "pages": pages,
                "message": msg[-500:] or "verified"}
    return {"ok": True, "pdf": pdf.name, "pages": pages, "message": "compiled"}


def export_path(out_dir: Path, slug: str, extension: str, *, kind: str = "main", existing: bool = False) -> Path:
    """Keep the application slug in the folder and shorten filenames when Windows needs it."""
    original = out_dir / f"{kind}_{slug}.{extension}"
    if WINDOWS_PATH_BUDGET is None or windows_path_length(original) <= WINDOWS_PATH_BUDGET:
        return original
    compact = out_dir / f"{kind}_cv.{extension}"
    if existing and not compact.is_file():
        try:
            if original.is_file():
                return original
        except OSError:
            pass
    return compact


def write_all(draft: dict, slug: str, out_dir: Path, accent: str) -> list[dict]:
    """Write every format. CV sources go to cv/ (gitignored main_*), everything to the app folder."""
    cv = draft["cv"]
    files = []
    docx_path = export_path(out_dir, slug, "docx")
    to_docx(cv, docx_path, accent, draft.get("_template") or "classic", draft.get("_design"))
    files.append({"name": docx_path.name, "kind": "Word (.docx)", "primary": True})
    markdown_path = export_path(out_dir, slug, "md")
    markdown_path.write_text(to_markdown(cv), encoding="utf-8")
    files.append({"name": markdown_path.name, "kind": "Markdown"})
    tex_path = export_path(out_dir, slug, "tex")
    tex_src = to_latex(cv, tex_path.name)
    tex_path.write_text(tex_src, encoding="utf-8")
    CV_DIR.mkdir(exist_ok=True)
    repo_tex = CV_DIR / f"main_{slug}.tex"
    repo_tex.write_text(tex_src if tex_path.name == repo_tex.name else to_latex(cv, repo_tex.name),
                        encoding="utf-8")
    files.append({"name": tex_path.name, "kind": "LaTeX (repo template)"})
    if draft.get("cover_letter") and draft["cover_letter"].get("paragraphs"):
        word = export_path(out_dir, slug, "docx", kind="cover")
        cover_docx(draft, word, accent)
        files.append({"name": word.name, "kind": "Cover letter (Word)", "primary": True})
        ctex = cover_latex(draft)
        letter_tex = export_path(out_dir, slug, "tex", kind="cover")
        letter_tex.write_text(ctex, encoding="utf-8")
        cl_dir = REPO / "cover_letters"
        if cl_dir.exists():
            (cl_dir / f"cover_{slug}.tex").write_text(ctex, encoding="utf-8")
        files.append({"name": letter_tex.name, "kind": "Cover letter (repo cover.cls)"})
    if draft.get("statement"):
        statement_path = export_path(out_dir, slug, "docx", kind="statement")
        statement_docx(draft, statement_path)
        files.append({"name": statement_path.name, "kind": "Supporting statement"})
    pdf = tex_path.with_suffix(".pdf")
    if pdf.exists():
        files.append({"name": pdf.name, "kind": "PDF (compiled)"})
    epdf = export_path(out_dir, slug, "pdf", kind="enhancv", existing=True)
    if epdf.exists():
        files.append({"name": epdf.name, "kind": "PDF (Enhancv design)"})
    return files
