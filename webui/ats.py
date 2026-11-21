"""Dedicated ATS and job-match checks. Pure code, no AI: instant, free, and the same answer
every time, whichever model wrote the CV.

1. ATS parse: read the actual file (DOCX or PDF) the way an applicant tracking system does,
   and report what it can and can't extract: contact details, standard section headings,
   date ranges, and layout features that commonly break parsers (tables, text boxes,
   headers/footers, columns, images, no text layer).
2. Keyword match: pull the hard skills, tools, certifications and acronyms from the job
   description (weighting the essential criteria), and look for each one in the CV text.
3. Job fit signals: job title, years of experience asked for, degree requirement.

Scores are weighted sums with the formula returned alongside, so they can be checked by hand.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?:\+44\s?\(?0?\)?\s?|\b0)(?:\d[\s-]?){9,10}\b")
LINKEDIN = re.compile(r"linkedin\.com/in/[\w-]+", re.I)
POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b")
DATE_RANGE = re.compile(
    r"\b(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+)?((?:19|20)\d\d)\s*"
    r"(-|–|—|to)\s*(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+)?"
    r"((?:19|20)\d\d|present|current|now|date)\b", re.I)
SECTIONS = {
    "Experience": r"(work |professional |employment |career )?(experience|history)",
    "Education": r"education|qualifications|academic",
    "Skills": r"(key |core |technical )?(skills|competencies|competences)",
    "Summary": r"(professional )?(summary|profile|personal statement|about me)",
    "Certifications": r"certifications?|certificates|licen[cs]es|accreditations",
    "Achievements": r"(key |career |selected )?(achievements|accomplishments|highlights)",
}
SOFT = ["stakeholder", "communication", "leadership", "mentoring", "collaboration", "problem solving",
        "attention to detail", "time management", "presentation", "negotiation", "coaching"]
ACRONYM_STOP = {"UK", "CV", "THE", "AND", "FOR", "WITH", "JOB", "ROLE", "OUR", "YOU", "WE", "NHS", "HR", "PA",
                "AM", "PM", "TBC", "FTE", "N/A", "OR", "IN", "OF", "TO", "A", "I", "IT", "ID", "PS", "JD",
                "DBS", "REF", "LTD", "PLC", "MON", "FRI", "BAND", "GBP", "EU", "USA", "US", "CEO", "CTO",
                # recruitment and contract jargon, not skills
                "IR35", "ASAP", "PAYE", "WFH", "UMB", "OTE", "DOE", "FTC", "EMEA", "APAC", "UK&I", "NB", "TBA",
                "SME", "SMES", "CFO", "COO", "CIO", "HQ", "MD", "NI", "LLP", "INC", "CIC", "EDI", "ETC", "EG", "IE",
                "AA", "AN", "AS", "AT", "BE", "BY", "IF", "IS", "ON", "SO", "UP", "VS", "ALL", "ANY", "NEW", "ARE"}


# ----------------------------------------------------------------------------- read the file

def _docx_text_and_risks(data: bytes) -> tuple[str, list[dict]]:
    risks = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        doc = z.read("word/document.xml").decode("utf-8", errors="replace")
        body_text = _xml_text(doc)
        if "<w:tbl>" in doc or "<w:tbl " in doc:
            risks.append({"sev": "warn", "item": "Tables",
                          "note": "Some ATSs read table cells out of order or skip them. Keep experience and contact details outside tables."})
        if "txbxContent" in doc:
            risks.append({"sev": "fail", "item": "Text boxes",
                          "note": "Text inside text boxes is often invisible to an ATS. Move it into normal paragraphs."})
        cols = re.findall(r'<w:cols [^>]*w:num="(\d+)"', doc)
        if any(int(c) > 1 for c in cols):
            risks.append({"sev": "warn", "item": "Multiple columns",
                          "note": "Two-column layouts can be read across both columns line by line, scrambling sections. "
                                  "Most UK employers' systems cope best with one column."})
        if any(n.startswith("word/media/") for n in names):
            risks.append({"sev": "warn", "item": "Images or icons",
                          "note": "Images carry no text for an ATS. In the UK a photo is not expected and can invite bias: "
                                  "leave it off. Icons are fine as decoration, never for contact details or skills."})
        header_text = " ".join(_xml_text(z.read(n).decode("utf-8", errors="replace"))
                               for n in names if re.match(r"word/(header|footer)\d*\.xml", n))
        if EMAIL.search(header_text) or PHONE.search(header_text):
            risks.append({"sev": "fail", "item": "Contact details in header/footer",
                          "note": "Many ATSs ignore headers and footers. Put email and phone in the body."})
        # Only fonts on actual text runs. Paragraph-mark properties (<w:pPr>) and Word's own bullet
        # glyphs (Symbol/Wingdings in numbering) never reach an ATS as text.
        run_xml = re.sub(r"<w:pPr>.*?</w:pPr>", "", doc, flags=re.S)
        fonts = set(re.findall(r'w:ascii="([^"]+)"', run_xml)) - {"Symbol", "Wingdings", "Wingdings 2", "Wingdings 3"}
        odd = [f for f in fonts if f.lower() not in {"calibri", "arial", "helvetica", "georgia", "garamond",
                                                     "times new roman", "cambria", "verdana", "tahoma",
                                                     "aptos", "segoe ui", "carlito", "liberation sans"}]
        if odd:
            risks.append({"sev": "warn", "item": "Unusual fonts", "note": ", ".join(sorted(odd))[:120]})
    return body_text + ("\n" + header_text if header_text.strip() else ""), risks


def _xml_text(xml: str) -> str:
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return re.sub(r"<[^>]+>", "", xml).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def _pdf_text_and_risks(data: bytes) -> tuple[str, list[dict]]:
    from pypdf import PdfReader
    risks = []
    reader = PdfReader(io.BytesIO(data))
    pages = [(p.extract_text() or "") for p in reader.pages]
    text = "\n".join(pages)
    if not text.strip():
        risks.append({"sev": "fail", "item": "No text layer",
                      "note": "This PDF is an image (scanned or exported as a picture). An ATS reads nothing."})
    elif any(len(p.strip()) < 40 for p in pages):
        risks.append({"sev": "warn", "item": "A near-empty page", "note": "A page with almost no extractable text."})
    if "(cid:" in text or "�" in text:
        risks.append({"sev": "fail", "item": "Garbled characters",
                      "note": "Some text extracts as (cid:..) or �. A font is embedded without a Unicode map."})
    if len(reader.pages) > 3:
        risks.append({"sev": "warn", "item": f"{len(reader.pages)} pages", "note": "UK CVs are usually two pages."})
    return text, risks


def read_cv(filename: str, data: bytes) -> tuple[str, list[dict], str]:
    name = (filename or "").lower()
    if name.endswith(".docx"):
        text, risks = _docx_text_and_risks(data)
        kind = "Word"
    elif name.endswith(".pdf"):
        text, risks = _pdf_text_and_risks(data)
        kind = "PDF"
    elif name.endswith((".txt", ".md")):
        text, risks, kind = data.decode("utf-8", errors="replace"), [], "text"
    else:
        raise ValueError("Upload the CV as .docx or .pdf (what you would actually send).")
    if not re.search(r"(cv|resume|curriculum)", name) and kind != "text":
        risks.append({"sev": "info", "item": "File name",
                      "note": "Recruiters see the file name: use 'Firstname-Lastname-CV.docx'."})
    return text, risks, kind


# ----------------------------------------------------------------------------- parse like an ATS

def heading_match(pat: str, line: str) -> bool:
    """A standard heading, alone or joined with another word: 'Skills', 'SKILLS AND TOOLS',
    'Education & Certification', 'Work Experience'."""
    joined = r"[a-z][a-z ]{0,24}"
    return bool(re.fullmatch(r"(?i)\W*(?:" + joined + r"\s*(?:and|&|/|,)\s*)?(" + pat + r")"
                             r"(?:\s*(?:and|&|/|,)\s*" + joined + r")?\W*", line.strip()))


def parse(text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    found_sections = {}
    for label, pat in SECTIONS.items():
        for l in lines:
            if len(l) <= 45 and heading_match(pat, l):
                found_sections[label] = l
                break
    ranges = DATE_RANGE.findall(text)
    dash_ranges = [r for r in ranges if r[1] in ("–", "—")]
    years = [int(y) for r in ranges for y in (r[0], r[2]) if y.isdigit()]
    email, phone = EMAIL.search(text), PHONE.search(text)
    return {
        "name": lines[0][:60] if lines else "",
        "email": email.group(0) if email else "",
        "phone": phone.group(0).strip() if phone else "",
        "linkedin": (LINKEDIN.search(text) or [""])[0] if LINKEDIN.search(text) else "",
        "postcode_or_town": bool(POSTCODE.search(text)) or bool(re.search(r"\b(UK|England|Scotland|Wales)\b", text)),
        "sections": found_sections,
        "date_ranges": len(ranges),
        "unicode_dash_ranges": len(dash_ranges),
        "earliest_year": min(years) if years else None,
        "words": len(text.split()),
        "jobs": parse_jobs(lines),
    }


def parse_jobs(lines: list[str]) -> list[dict]:
    """What an ATS files as each job: the title, employer and dates it can find on or just above a date line."""
    jobs = []
    for i, line in enumerate(lines):
        m = DATE_RANGE.search(line)
        if not m:
            continue
        before = line[:m.start()].strip(" \t|,-–—") or (lines[i - 1] if i else "")
        parts = [p.strip() for p in re.split(r"\s+\|\s+|\t", before) if p.strip()]
        title = parts[0] if parts else ""
        employer = parts[1] if len(parts) > 1 else (lines[i - 1] if i and lines[i - 1] != before else "")
        title = title.rstrip(" (–—-;")
        employer = re.split(r",\s*", employer)[0][:60]
        if employer.isupper() or any(heading_match(pat, employer) for pat in SECTIONS.values()):
            employer = ""  # a section heading is not an employer
        many = len(DATE_RANGE.findall(line)) > 1  # two roles on one line: an ATS may merge them
        jobs.append({"title": title[:80], "employer": employer, "dates": m.group(0),
                     "ok": bool(title and employer) and not many,
                     "note": "Two roles share one line: give each its own title and dates" if many else ""})
    return jobs[:15]


def parse_checks(p: dict, risks: list[dict]) -> tuple[list[dict], int]:
    checks = []

    def add(ok, label, note, weight=1, sev_if_bad="fail"):
        checks.append({"ok": ok, "label": label, "note": note, "weight": weight,
                       "sev": "pass" if ok else sev_if_bad})
    add(bool(p["email"]), "Email address readable", p["email"] or "No email found in the text an ATS sees.", 3)
    add(bool(p["phone"]), "Phone number readable", p["phone"] or "No UK phone number found.", 3)
    add(bool(p["linkedin"]), "LinkedIn URL as text", p["linkedin"] or "No linkedin.com/in/ link printed.", 1, "warn")
    add(p["postcode_or_town"], "Location", "Town or postcode found." if p["postcode_or_town"] else
        "No town/postcode: many UK ATSs filter by distance.", 1, "warn")
    for s in ("Experience", "Education", "Skills"):
        add(s in p["sections"], f"'{s}' section heading",
            f"Found as '{p['sections'][s]}'." if s in p["sections"] else
            f"No standard {s} heading. ATSs look for conventional headings.", 2, "warn" if s == "Skills" else "fail")
    add(p["date_ranges"] >= 2, "Job dates parse", f"{p['date_ranges']} date ranges found.", 2)
    add(p["unicode_dash_ranges"] == 0, "Dates use plain hyphens",
        "All ranges use '-'." if not p["unicode_dash_ranges"] else
        f"{p['unicode_dash_ranges']} ranges use an en/em dash; some ATSs drop those dates.", 1, "warn")
    add(350 <= p["words"] <= 1100, "Length", f"{p['words']} words (two pages is roughly 450-900).", 1, "warn")
    for r in risks:
        if r["sev"] in ("fail", "warn"):
            checks.append({"ok": False, "label": r["item"], "note": r["note"],
                           "weight": 3 if r["sev"] == "fail" else 1, "sev": r["sev"]})
        else:
            checks.append({"ok": True, "label": r["item"], "note": r["note"], "weight": 0, "sev": "info"})
    total = sum(c["weight"] for c in checks)
    got = sum(c["weight"] for c in checks if c["ok"])
    return checks, round(100 * got / total) if total else 0


# ----------------------------------------------------------------------------- keywords

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().replace("’", "'"))


def _variants(term: str) -> list[str]:
    t = _norm(term)
    out = {t}
    if t.endswith("s"):
        out.add(t[:-1])
    else:
        out.add(t + "s")
    swaps = [("modelling", "modeling"), ("visualisation", "visualization"), ("organisation", "organization"),
             ("analyse", "analyze"), ("optimisation", "optimization")]
    for a, b in swaps:
        if a in t:
            out.add(t.replace(a, b))
        if b in t:
            out.add(t.replace(b, a))
    aliases = {"power bi": ["powerbi"], "ci/cd": ["cicd", "ci cd"], "t-sql": ["tsql"], "azure data factory": ["adf"],
               "business intelligence": ["bi"], "machine learning": ["ml"], "microsoft fabric": ["fabric"],
               "data warehousing": ["data warehouse", "data warehouses"], "data warehouse": ["data warehousing"],
               "data modelling": ["data model", "data models"], "stakeholder": ["stakeholders"],
               "etl": ["extract, transform", "elt"], "sql server": ["mssql", "ms sql"]}
    out.update(aliases.get(t, []))
    return list(out)


def _has(text: str, term: str) -> bool:
    for v in _variants(term):
        if re.search(r"(?<![a-z0-9])" + re.escape(v) + r"(?![a-z0-9])", text):
            return True
    return False


def jd_keywords(jd: str, vocab: list[str]) -> list[dict]:
    """Hard skills from a known vocabulary, plus acronyms/tools the JD itself spells out."""
    low = _norm(jd)
    essential_part = ""
    m = re.search(r"(?is)(essential|required|requirements|must have)(.*?)(desirable|preferred|nice to have|$)", jd)
    if m:
        essential_part = _norm(m.group(2))
    desirable_part = ""
    m2 = re.search(r"(?is)(desirable|preferred|nice to have|advantage)(.*)", jd)
    if m2:
        desirable_part = _norm(m2.group(2)[:3000])
    terms: dict[str, dict] = {}
    for t in vocab:
        if _has(low, t):
            terms[t] = {"term": t, "kind": "skill"}
    for a in set(re.findall(r"\b[A-Z][A-Z0-9+#/&.-]{1,9}\b", jd)):
        a2 = a.strip(".-/&")
        if a2 in ACRONYM_STOP or len(a2) < 2 or a2.isdigit() or re.search(r"\d{3,}", a2):
            continue  # stop words and job reference numbers such as PHT-0458
        if _norm(a2) not in terms:
            terms[_norm(a2)] = {"term": a2, "kind": "acronym"}
    for camel in set(re.findall(r"\b[A-Z][a-z]+[A-Z][A-Za-z]+\b", jd)):  # PowerShell, HubSpot, GitHub
        terms.setdefault(_norm(camel), {"term": camel, "kind": "tool"})
    for s in SOFT:
        if s in low:
            terms.setdefault(s, {"term": s, "kind": "soft"})
    for k, t in terms.items():
        count = len(re.findall(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", low))
        if essential_part and _has(essential_part, k):
            t["importance"] = "essential"
        elif desirable_part and _has(desirable_part, k):
            t["importance"] = "desirable"
        else:
            t["importance"] = "high" if count >= 2 or t["kind"] == "skill" else "medium"
        if t["kind"] == "soft" and t["importance"] not in ("essential",):
            t["importance"] = "medium"
        t["count"] = count
    # Drop a term that is just part of a longer term already listed ("fabric" inside "microsoft fabric").
    keys = list(terms)
    for k in keys:
        if any(k != o and re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", o) for o in keys):
            terms.pop(k, None)
    for k in list(terms):  # "data warehouse" and "data warehousing" are one requirement
        if any(o in terms and o != k and o > k for o in _variants(k)):
            terms.pop(k, None)
    order = {"essential": 0, "high": 1, "desirable": 2, "medium": 3}
    return sorted(terms.values(), key=lambda t: (order[t["importance"]], -t["count"], t["term"]))[:40]


WEIGHT = {"essential": 3, "high": 2, "desirable": 1, "medium": 1}


def split_sections(cv_text: str) -> dict:
    """Split CV text by its headings: {"skills": ..., "experience": ..., "other": ...}.
    Experience and projects count as places a skill is shown in use."""
    out = {"skills": [], "experience": [], "other": []}
    cur = "other"
    for line in cv_text.splitlines():
        l = line.strip()
        if l and len(l) <= 45:
            if heading_match(SECTIONS["Skills"], l):
                cur = "skills"
                continue
            if heading_match(SECTIONS["Experience"], l) or heading_match(r"(selected |key |personal )?projects?", l):
                cur = "experience"
                continue
            if any(heading_match(SECTIONS[k], l) for k in ("Education", "Summary", "Certifications", "Achievements")):
                cur = "other"
                continue
        out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def keyword_match(cv_text: str, jd: str, vocab: list[str], bank_text: str = "") -> dict:
    cvl, bankl = _norm(cv_text), _norm(bank_text)
    parts = {k: _norm(v) for k, v in split_sections(cv_text).items()}
    rows, got, total, shown, found_skills = [], 0, 0, 0, 0
    for t in jd_keywords(jd, vocab):
        on = _has(cvl, t["term"])
        status = "found" if on else ("in_master" if bank_text and _has(bankl, t["term"]) else "missing")
        w = WEIGHT[t["importance"]]
        total += w
        got += w if on else 0
        row = {**t, "status": status}
        if on and t["kind"] != "soft":
            row["shown"] = _has(parts["experience"], t["term"])  # used in a role or project, not just listed
            found_skills += 1
            shown += row["shown"]
        rows.append(row)
    return {"rows": rows, "score": round(100 * got / total) if total else None,
            "formula": f"weighted keywords found {got} / {total} (essential 3, high 2, desirable/medium 1)",
            "shown": shown, "found_skills": found_skills,
            "shown_rate": round(100 * shown / found_skills) if found_skills else None}


# ----------------------------------------------------------------------------- fit signals

def fit_signals(cv_text: str, jd: str, parsed: dict, target_title: str = "") -> list[dict]:
    out = []
    jdl, cvl = _norm(jd), _norm(cv_text)
    first = (jd.strip().splitlines() or [""])[0][:120]
    title = target_title or re.split(r"\s[-|\u2013]\s", first)[-1].strip()
    tw = [w for w in re.findall(r"[a-z]+", _norm(title)) if w not in {"senior", "lead", "junior", "and", "the", "of", "for"}]
    hits = [w for w in tw if w in cvl[:1500]]
    out.append({"label": "Job title on your CV", "ok": bool(tw) and len(hits) >= max(1, len(tw) - 1),
                "note": f"'{title}': " + (f"{len(hits)}/{len(tw)} title words appear near the top of your CV."
                                          if tw else "no title detected in the JD."),
                "weight": 15})
    yrs = [int(n) for n in re.findall(r"(\d{1,2})\+?\s*(?:years|yrs)", jdl)]
    if yrs and parsed.get("earliest_year"):
        need, have = max(yrs), date.today().year - parsed["earliest_year"]
        out.append({"label": "Years of experience", "ok": have >= need,
                    "note": f"Advert asks for {need}+ years; your CV's dates span about {have}.", "weight": 15})
    if re.search(r"\b(degree|bsc|ba|msc|graduate)\b", jdl):
        has_degree = bool(re.search(r"\b(bsc|ba|msc|mba|degree|bachelor|master|phd|beng|meng|hnd)\b", cvl))
        out.append({"label": "Degree", "ok": has_degree,
                    "note": "Advert mentions a degree; " + ("one is on your CV." if has_degree else "none found on your CV."),
                    "weight": 10})
    # Seniority: what level the advert is hiring at vs the most senior title on the CV.
    levels = [("head", r"\b(head of|director)\b"), ("lead", r"\b(lead|principal|manager|architect)\b"),
              ("senior", r"\b(senior|sr\.?)\b"), ("mid", r"."), ]
    def level(text):
        for name, rx in levels:
            if re.search(rx, text, re.I):
                return name
        return "mid"
    rank = {"mid": 0, "senior": 1, "lead": 2, "head": 3}
    want = level(title)
    cv_titles = [l for l in cv_text.splitlines()[:80] if re.search(r"\b(19|20)\d\d\b", l) and len(l) < 160]
    have = max((rank[level(l.split("|")[0])] for l in cv_titles), default=0)
    if title:
        gap = rank[want] - have
        out.append({"label": "Seniority", "ok": gap <= 0,
                    "note": f"Advert is hiring at {want} level; your most senior title reads as "
                            f"{[k for k, v in rank.items() if v == have][0]} level."
                            + ("" if gap <= 0 else " Show the scope you led (people, budget, systems) to bridge it."),
                    "weight": 10})
    # Named certifications or qualifications the advert asks for.
    certs = set(re.findall(r"\b(?:DP|PL|AZ|AI|DA|SC|MS|AWS|SAA|DAS|DEA)-\d{2,3}\b", jd))
    certs |= {m.strip() for m in re.findall(r"(?i)\b((?:Microsoft|AWS|Azure|Google|Databricks|Snowflake|dbt|Tableau)"
                                              r"[A-Za-z ]{0,40}?Certifi(?:ed|cation)(?::? [A-Z][A-Za-z ]{0,45}?)?)(?=[,.;\n)]|$| or | and )", jd)}
    certs |= set(re.findall(r"\b(PRINCE2|ITIL|CISSP|CISM|PMP|TOGAF|Scrum Master|CBAP|CIPD|ACCA|CIMA)\b", jd))
    for c in sorted(certs)[:6]:
        has = _has(cvl, _norm(c))
        out.append({"label": f"Asks for {c}", "ok": has,
                    "note": "On your CV." if has else "Not on your CV. If you hold it or are studying for it, say so; "
                                                     "if it's essential and you don't, address it in your letter.",
                    "weight": 5})
    if re.search(r"(security clearance|\bSC\b|\bDV\b|BPSS|NPPV)", jd):
        out.append({"label": "Security clearance", "ok": None,
                    "note": "Advert mentions clearance. Say on your CV if you hold or can obtain it.", "weight": 0})
    if re.search(r"(?i)driving licen[cs]e", jd):
        has = bool(re.search(r"(?i)driving licen[cs]e", cv_text))
        out.append({"label": "Driving licence", "ok": has, "note": "Advert asks for a driving licence; "
                    + ("it's on your CV." if has else "not mentioned on your CV."), "weight": 5})
    return out


def analyse(cv_text: str, risks: list[dict], jd: str, vocab: list[str], bank_text: str = "",
            target_title: str = "") -> dict:
    parsed = parse(cv_text)
    pchecks, parse_score = parse_checks(parsed, risks)
    report = {"parsed": parsed, "parse_checks": pchecks, "parse_score": parse_score,
              "text_preview": cv_text[:4000]}
    if jd.strip() and len(jd.split()) < 60:
        # A title alone (or a few lines) can't be matched fairly: one keyword found would read as 100%.
        report["jd_too_short"] = len(jd.split())
    elif jd.strip():
        kw = keyword_match(cv_text, jd, vocab, bank_text)
        fits = fit_signals(cv_text, jd, parsed, target_title)
        parts = [("keywords", 60, kw["score"] or 0)] + [
            (f["label"], f["weight"], 100 if f["ok"] else 0) for f in fits if f["ok"] is not None and f["weight"]]
        total_w = sum(w for _, w, _ in parts)
        match = round(sum(w * v for _, w, v in parts) / total_w) if total_w else None
        report.update(keywords=kw, fit=fits, match_score=match,
                      match_formula=" + ".join(f"{n} {v}%×{w}" for n, w, v in parts) + f" ÷ {total_w}")
    return report
