"""Fix and improve a Word CV for ATS, then recheck. Pure code, no AI.

Every change is written to a NEW copy of the file (versions v0, v1, v2 ... under
documents/cv/ats_work/, gitignored). The uploaded original is never modified, and any earlier
version can be made current again.

Fixes:
  dates   en/em dashes in date ranges -> plain hyphens ("Feb 2021 - Present")
  phone   add your phone number to the contact line (the line with your email)
  fonts   unusual fonts on text runs -> Calibri
  skills  rewrite the items on your Skills lines (keeps each line's bold label)
  newline add one more Skills line ("Label: items") after the last one

Only skills the user puts there are added: suggestions come from the job's keywords, and a
keyword the user hasn't confirmed is never added automatically.
"""

from __future__ import annotations

import copy
import io
import json
import re
import shutil
import time
from pathlib import Path

import docx
from docx.text.paragraph import Paragraph

import ats
from store import DOCS

WORK = DOCS / "cv" / "ats_work"
SESSION = WORK / "session.json"
PROFILE = WORK / "profile.json"
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+"
DASH_RANGE = re.compile(r"(\b(?:19|20)\d\d)(\s*)[\u2013\u2014](\s*)((?:" + _MON + r")?(?:(?:19|20)\d\d|present|current|now|date))\b",
                        re.I)
GENERIC = re.compile(r"^\s*(?:microsoft |ms )?(?:word|powerpoint|outlook|office(?: 365)?|teams|trello|visio|project|"
                     r"onenote|publisher|zoom|slack|google (?:docs|sheets|drive)|windows|notepad)\s*$", re.I)
FAMILIES = [  # keyword pattern -> words to look for in a Skills line's label
    (r"sql|postgres|mysql|snowflake|bigquery|redshift|oracle|database|t-sql|nosql|mongo", ["sql", "database"]),
    (r"power bi|tableau|looker|qlik|dax|ssrs|ssas|report|dashboard|visuali", ["report", "bi"]),
    (r"python|dbt|airflow|spark|etl|elt|pipeline|kafka|databricks|data factory|fabric|synapse|pandas|ingest",
     ["engineering", "quality", "data eng"]),
    (r"azure|aws|gcp|cloud|terraform|gdpr|security|governance|entra|devops", ["cloud", "governance", "security"]),
    (r"agile|scrum|jira|stakeholder|project|prince2|kanban|confluence|waterfall", ["project", "ways of working"]),
    (r"excel|vba|access|macro|pivot", ["excel", "office"]),
    (r"statistic|model|warehouse|kimball|star schema|scd|regression|forecast", ["modelling", "statistics"]),
    (r"c#|\.net|react|typescript|javascript|java|api|git|ci/cd|docker", ["software", "development"]),
    (r"test|pytest|selenium|great expectations", ["quality", "software"]),
]


# ----------------------------------------------------------------------------- session & versions

def _session() -> dict:
    try:
        return json.loads(SESSION.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_session(s: dict) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    SESSION.write_text(json.dumps(s, indent=2), encoding="utf-8")


def start(name: str, data: bytes) -> dict:
    """New working session from an uploaded Word CV (v0 = the original, untouched)."""
    if WORK.exists():
        for d in WORK.glob("v*"):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
    name = Path(name).name
    _write_version(0, name, data)
    s = {"name": name, "current": 0, "versions": [{"v": 0, "note": "Your original", "at": time.time()}]}
    _save_session(s)
    return s


def _write_version(v: int, name: str, data: bytes) -> Path:
    d = WORK / f"v{v}"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(data)
    return p


def version_path(v: int | None = None) -> Path:
    s = _session()
    if not s:
        raise FileNotFoundError("Run an ATS check on a Word CV first.")
    v = s["current"] if v is None else v
    p = WORK / f"v{v}" / s["name"]
    if not p.exists():
        raise FileNotFoundError(f"Version {v} isn't there any more.")
    return p


def session() -> dict:
    return _session()


def use(v: int) -> dict:
    s = _session()
    version_path(v)
    s["current"] = v
    _save_session(s)
    return s


def record_score(v: int, parse_score, kw_score, match_score) -> None:
    s = _session()
    for x in s.get("versions", []):
        if x["v"] == v:
            x.update(parse=parse_score, keywords=kw_score, match=match_score)
    _save_session(s)


def profile() -> dict:
    try:
        return json.loads(PROFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_profile(**kw) -> None:
    p = profile()
    p.update({k: v for k, v in kw.items() if v})
    WORK.mkdir(parents=True, exist_ok=True)
    PROFILE.write_text(json.dumps(p, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------- reading the Skills section

def _is_heading(p) -> bool:
    t = p.text.strip()
    if not t or len(t) > 45:
        return False
    runs = [r for r in p.runs if r.text.strip()]
    bold = runs and all(r.bold or (p.style is not None and p.style.font.bold) for r in runs)
    return bool(bold and (t.isupper() or p.style.name.lower().startswith("heading"))) or \
        p.style.name.lower().startswith("heading")


def skills_paragraphs(doc) -> list[int]:
    """Indexes of the paragraphs under the Skills heading, up to the next heading."""
    paras = doc.paragraphs
    start = next((i for i, p in enumerate(paras)
                  if _is_heading(p) and ats.heading_match(ats.SECTIONS["Skills"], p.text)), None)
    if start is None:
        return []
    out = []
    for i in range(start + 1, len(paras)):
        if _is_heading(paras[i]):
            break
        if paras[i].text.strip():
            out.append(i)
    return out


def _label_runs(p) -> int:
    """How many leading runs make up the bold 'Label:' part of a skills line (0 if none)."""
    n, txt = 0, ""
    for r in p.runs:
        if not r.bold:
            break
        n += 1
        txt += r.text
        if txt.rstrip().endswith(":"):
            return n
    return 0


def split_items(text: str) -> list[str]:
    """Top-level items split on ; and , (commas inside brackets stay together)."""
    items, depth, cur = [], 0, ""
    for ch in text:
        depth += ch in "(["
        depth -= ch in ")]"
        if ch in ",;" and depth <= 0:
            if cur.strip():
                items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur.strip().rstrip("."))
    return items


def skills_lines(data: bytes) -> list[dict]:
    doc = docx.Document(io.BytesIO(data))
    out = []
    for i in skills_paragraphs(doc):
        p = doc.paragraphs[i]
        n = _label_runs(p)
        label = "".join(r.text for r in p.runs[:n]).strip().rstrip(":").strip()
        items = "".join(r.text for r in p.runs[n:]).strip() if n else p.text.strip()
        out.append({"idx": i, "label": label, "items": items, "list": split_items(items)})
    return out


def _item_terms(item: str) -> list[str]:
    head = re.sub(r"\(.*?\)", "", item).strip()
    inner = re.findall(r"\((.*?)\)", item)
    terms = [head] + [t.strip() for x in inner for t in re.split(r"[,;]", x)]
    return [t for t in terms if len(t) > 1]


def suggestions(lines: list[dict], keyword_rows: list[dict], jd: str, bank_skills: list[str],
                vocab: list[str] | None = None) -> dict:
    """What to add (job keywords not on the CV) and what could go (CV items the job never mentions)."""
    jdn = ats._norm(jd)
    labels = [l["label"].lower() for l in lines]

    def target(term: str) -> int | None:
        t = term.lower()
        for pat, hints in FAMILIES:
            if re.search(pat, t):
                for h in hints:  # hints in priority order: "SQL" before "Office and databases"
                    for k, lab in enumerate(labels):
                        if h in lab:
                            return lines[k]["idx"]
        return None  # no obvious home: goes on a new "Other" line the user can rename

    bank_n = {ats._norm(b) for b in bank_skills}
    add = []
    for r in keyword_rows or []:
        if r["status"] == "found":
            continue
        have = "yes" if (r["status"] == "in_master" or ats._norm(r["term"]) in bank_n) else "unknown"
        add.append({"term": r["term"], "importance": r["importance"], "have": have, "kind": r.get("kind", "skill"),
                    "target": target(r["term"])})
    order = {"essential": 0, "high": 1, "desirable": 2, "medium": 3}
    add.sort(key=lambda a: (a["have"] != "yes", order.get(a["importance"], 9)))
    cut = {}
    if jd.strip():
        for l in lines:
            cut[l["idx"]] = [
                # strong: the ad doesn't mention it AND it's a general office tool that adds little for
                # data roles (Word, Outlook, Trello...). Everything else is the candidate's call.
                {"item": it, "strong": bool(GENERIC.search(re.sub(r"\(.*?\)", "", it)))}
                for it in l["list"] if not any(ats._has(jdn, t) for t in _item_terms(it))]
    cv_text = " ".join(l["items"] for l in lines)
    extra = [{"term": b, "target": target(b)} for b in bank_skills if not ats._has(ats._norm(cv_text), b)]
    return {"add": add, "cut": cut, "bank_not_on_cv": extra}


def _blocks(doc) -> dict:
    """The headline under your name, and the first paragraph of the profile/summary section."""
    paras = doc.paragraphs
    out = {}
    nonempty = [i for i, p in enumerate(paras[:6]) if p.text.strip()]
    if len(nonempty) >= 2:
        i = nonempty[1]
        t = paras[i].text.strip()
        if len(t) <= 140 and not ats.EMAIL.search(t) and not _is_heading_section(paras[i]):
            out["headline"] = i
    start = next((i for i, p in enumerate(paras)
                  if _is_heading(p) and ats.heading_match(ats.SECTIONS["Summary"], p.text)), None)
    if start is not None:
        nxt = next((i for i in range(start + 1, min(start + 4, len(paras))) if paras[i].text.strip()), None)
        if nxt is not None and not _is_heading(paras[nxt]):
            out["profile"] = nxt
    return out


def _is_heading_section(p) -> bool:
    return _is_heading(p) and any(ats.heading_match(pat, p.text) for pat in ats.SECTIONS.values())


def blocks(data: bytes) -> dict:
    doc = docx.Document(io.BytesIO(data))
    return {k: {"idx": i, "text": doc.paragraphs[i].text.strip()} for k, i in _blocks(doc).items()}


def _set_text(p, text: str) -> None:
    runs = [r for r in p.runs]
    if not runs:
        p.add_run(text)
        return
    runs[0].text = text
    for r in runs[1:]:
        r.text = ""


# ----------------------------------------------------------------------------- applying fixes

def _set_items(p, items: str) -> None:
    n = _label_runs(p)
    rest = p.runs[n:]
    if n and items and not items.startswith(" ") and not p.runs[n - 1].text.endswith(" "):
        items = " " + items
    if rest:
        rest[0].text = items
        for r in rest[1:]:
            r.text = ""
    else:
        p.add_run(items)


def apply(data: bytes, spec: dict) -> tuple[bytes, list[str]]:
    doc = docx.Document(io.BytesIO(data))
    changes = []
    if spec.get("dates"):
        n = 0
        for p in doc.paragraphs:
            for r in p.runs:
                new, k = DASH_RANGE.subn(r"\1 - \4", r.text)
                if k:
                    r.text, n = new, n + k
        if n:
            changes.append(f"{n} date range{'s' if n != 1 else ''} now use a plain hyphen")
    phone = (spec.get("phone") or "").strip()
    if phone and not ats.PHONE.search("\n".join(p.text for p in doc.paragraphs[:8])):
        line = next((p for p in doc.paragraphs[:10] if ats.EMAIL.search(p.text)), None)
        if line is not None and line.runs:
            sep = "  |  " if "  |  " in line.text else (" | " if " | " in line.text else ", ")
            line.runs[-1].text = line.runs[-1].text.rstrip() + sep + phone
            changes.append(f"Phone {phone} added to your contact line")
        else:
            changes.append("Couldn't find your contact line (the one with your email), so the phone wasn't added")
    if spec.get("fonts"):
        n = 0
        ok = {"calibri", "arial", "helvetica", "georgia", "garamond", "times new roman", "cambria", "verdana",
              "tahoma", "aptos", "segoe ui", "carlito", "liberation sans"}
        for p in doc.paragraphs:
            for r in p.runs:
                if r.font.name and r.font.name.lower() not in ok and r.text.strip():
                    r.font.name = "Calibri"
                    n += 1
        if n:
            changes.append(f"{n} text runs switched to Calibri")
    where = _blocks(doc)
    for key, label in (("headline", "Headline"), ("profile", "Profile")):
        text = re.sub(r"\s*\n\s*", " ", spec.get(key) or "").strip()  # keep "  |  " spacing as typed
        if text and key in where and text != doc.paragraphs[where[key]].text.strip():
            _set_text(doc.paragraphs[where[key]], text)
            changes.append(f"{label} rewritten")
    lines = {l: (t or "").strip() for l, t in (spec.get("skills") or {}).items()}
    if lines:
        idxs = set(skills_paragraphs(doc))
        for k, text in lines.items():
            i = int(k)
            if i not in idxs:
                continue
            p = doc.paragraphs[i]
            n = _label_runs(p)
            old = "".join(r.text for r in p.runs[n:]).strip() if n else p.text.strip()
            if text != old:
                _set_items(p, text)
                label = "".join(r.text for r in p.runs[:n]).strip().rstrip(":") or "Skills"
                changes.append(f"{label}: updated")
    new = spec.get("newline") or {}
    if (new.get("label") or "").strip() and (new.get("items") or "").strip():
        idxs = skills_paragraphs(doc)
        if idxs:
            last = doc.paragraphs[idxs[-1]]
            el = copy.deepcopy(last._p)
            last._p.addnext(el)
            p = Paragraph(el, last._parent)
            n = _label_runs(p)
            if n:
                p.runs[0].text = new["label"].strip().rstrip(":") + ": "
                for r in p.runs[1:n]:
                    r.text = ""
                _set_items(p, new["items"].strip())
            else:
                for r in p.runs[1:]:
                    r.text = ""
                p.runs[0].text = f"{new['label'].strip().rstrip(':')}: {new['items'].strip()}"
            changes.append(f"New skills line: {new['label'].strip()}")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue(), changes


def save_new_version(data: bytes, note: str) -> dict:
    s = _session()
    v = max(x["v"] for x in s["versions"]) + 1
    _write_version(v, s["name"], data)
    s["versions"].append({"v": v, "note": note, "at": time.time(), "from": s["current"]})
    s["current"] = v
    _save_session(s)
    return s
