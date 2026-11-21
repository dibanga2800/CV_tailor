"""Deterministic checks. The model writes; this module decides what is grounded.

Nothing here calls a model, so the scores the UI shows are arithmetic you can re-run,
not the model grading its own work.
"""

from __future__ import annotations

import re

import spelling
import unicodedata

FILLER = ["responsible for", "worked on", "helped", "various", "utilised", "utilized",
          "leverage", "synergy", "synergies", "hit the ground running", "passionate",
          "team player", "go-getter", "results-driven", "detail-oriented"]
PRONOUNS = re.compile(r"\b(I|me|my|mine|we|our)\b")
DASHES = re.compile(r"[–—]")
NUM = re.compile(r"(?<![A-Za-z])[£$€]?\d[\d,.]*(?:\s?%|\s?(?:k|m|bn|x)(?![A-Za-z]))?", re.I)  # "30 minutes" is 30, not 30m
HOW_TO_APPLY = re.compile(r"^\W*(?:please\s+)?(?:apply|submit|send|upload)\b.*\b(?:portal|website|online|form|link|email|via|through|by)\b"
                         r"|\bcareers? (?:portal|site|page)\b|\bclosing date\b|\bapplications? close\b", re.I)
DATE_RANGE = re.compile(r"\b(19|20)\d\d\b.*\s-\s.*\b((19|20)\d\d|present|current|now)\b", re.I)
# Bump when a check or the scoring changes: saved CVs with an older version are rescored automatically.
CHECKS_VERSION = "2026-11-21.1"
STATUS_WEIGHT = {"essential": 2, "preferred": 1, "not supplied": 1}
COVER_VALUE = {"yes": 1.0, "partial": 0.5, "listed": 0.25, "no": 0.0}
SECTIONS = ["summary", "achievements", "skills", "experience", "projects", "education", "certifications", "extras"]


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = s.replace("’", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s)


def _words(s: str) -> str:
    """Words only: 'IT Lead, Software and Data' matches 'IT Lead – Software & Data'. The no-dash
    style rule makes the writer swap dashes and ampersands; that isn't a changed fact."""
    s = _fold(s).replace("&", " and ")
    return " " + re.sub(r"[^\w]+", " ", s).strip() + " "


def _num_key(tok: str) -> str:
    return re.sub(r"[^\d.%kmbnx]", "", tok.lower()).rstrip(".")


# ----------------------------------------------------------------------------- normalise

def normalise(draft: dict) -> dict:
    draft.setdefault("target", {})
    draft.setdefault("warnings", [])
    draft.setdefault("requirements", [])
    draft.setdefault("hr_flags", [])
    draft.setdefault("fit", {})
    draft.setdefault("stretch", [])
    draft.setdefault("decisions", [])
    draft.setdefault("statement", [])
    cl = draft.get("cover_letter")
    if isinstance(cl, dict):
        for k in ("paragraphs", "bullets"):
            cl[k] = [x for x in (cl.get(k) or []) if isinstance(x, dict)]
            for x in cl[k]:
                x.setdefault("facts", [])
    else:
        draft["cover_letter"] = None
    cv = draft.setdefault("cv", {})
    cv.setdefault("contact", {}).setdefault("links", [])
    for key in ("summary", "achievements", "skills", "experience", "projects", "education", "certifications",
                "extras"):
        if not isinstance(cv.get(key), list):
            cv[key] = []
    order = [s for s in cv.get("section_order") or [] if s in SECTIONS]
    if order and "achievements" not in order:  # older CVs: key achievements sit straight under the profile
        order.insert(order.index("summary") + 1 if "summary" in order else 0, "achievements")
    cv["section_order"] = order + [s for s in SECTIONS if s not in order]
    for item in cv["summary"] + cv["achievements"] + cv["certifications"]:
        item.setdefault("facts", [])
    for e in cv["experience"]:
        e.setdefault("bullets", [])
    for p in cv["projects"]:
        p.setdefault("bullets", [])
    for x in cv["extras"]:
        x.setdefault("items", [])
    for ed in cv["education"]:
        ed.setdefault("facts", [])
    for r in draft["requirements"]:
        r["status"] = r.get("status", "not supplied").lower()
        if r["status"] not in STATUS_WEIGHT:
            r["status"] = "not supplied"
        r["covered"] = (r.get("covered") or "no").lower()
        if r["covered"] not in COVER_VALUE:
            r["covered"] = "no"
        r.setdefault("fact_ids", [])
    return draft


def tagged_lines(cv: dict):
    """Yield (path, label, text, facts) for every taggable line on the CV."""
    for i, s in enumerate(cv["summary"]):
        yield f"summary.{i}", "Summary", s.get("text", ""), s.get("facts", [])
    for i, a in enumerate(cv.get("achievements") or []):
        yield f"achievements.{i}", "Key achievement", a.get("text", ""), a.get("facts", [])
    for i, e in enumerate(cv["experience"]):
        for j, b in enumerate(e["bullets"]):
            yield (f"experience.{i}.bullets.{j}", e.get("company", "Experience"),
                   b.get("text", ""), b.get("facts", []))
    for i, p in enumerate(cv["projects"]):
        for j, b in enumerate(p["bullets"]):
            yield f"projects.{i}.bullets.{j}", p.get("name", "Project"), b.get("text", ""), b.get("facts", [])
    for i, ed in enumerate(cv["education"]):
        yield (f"education.{i}", "Education", " ".join(
            ed.get(k, "") for k in ("qualification", "detail") if ed.get(k)), ed.get("facts", []))
    for i, c in enumerate(cv["certifications"]):
        yield f"certifications.{i}", "Certification", c.get("text", ""), c.get("facts", [])
    for i, x in enumerate(cv["extras"]):
        for j, it in enumerate(x["items"]):
            yield f"extras.{i}.items.{j}", x.get("heading", ""), it.get("text", ""), it.get("facts", [])


def bullets(cv: dict):
    for i, e in enumerate(cv["experience"]):
        for j, b in enumerate(e["bullets"]):
            yield f"experience.{i}.bullets.{j}", b.get("text", "")
    for i, p in enumerate(cv["projects"]):
        for j, b in enumerate(p["bullets"]):
            yield f"projects.{i}.bullets.{j}", b.get("text", "")


def cv_text(cv: dict) -> str:
    parts = [cv.get("name", ""), cv.get("subtitle", "")]
    c = cv.get("contact", {})
    parts += [c.get("email", ""), c.get("phone", ""), c.get("location", "")]
    parts += [s.get("text", "") for s in cv["summary"]]
    parts += [a.get("text", "") for a in cv.get("achievements") or []]
    for k in cv["skills"]:
        parts.append(k.get("category", "") + ": " + ", ".join(k.get("items", [])))
    for e in cv["experience"]:
        parts += [e.get("title", ""), e.get("company", ""), e.get("dates", "")]
        parts += [b.get("text", "") for b in e["bullets"]]
    for p in cv["projects"]:
        parts += [p.get("name", ""), p.get("stack", "")] + [b.get("text", "") for b in p["bullets"]]
    for ed in cv["education"]:
        parts += [ed.get(k, "") for k in ("qualification", "institution", "dates", "detail")]
    parts += [c.get("text", "") for c in cv["certifications"]]
    for x in cv["extras"]:
        parts += [x.get("heading", "")] + [i.get("text", "") for i in x["items"]]
    return "\n".join(p for p in parts if p)


# ----------------------------------------------------------------------------- scoring

def run(draft: dict, bank: list[dict], posting_text: str = "") -> dict:
    draft = normalise(draft)
    full_cv = draft["cv"]
    # Score the sections that will actually be exported.
    cv = {**full_cv, **{name: [] for name in full_cv.get("hidden_sections") or [] if name in SECTIONS}}
    ids = {f["id"]: f["text"] for f in bank}
    bank_fold = _fold("\n".join(ids.values()))
    bank_words = _words("\n".join(ids.values()))
    bank_nums = {_num_key(n) for n in NUM.findall("\n".join(ids.values()))}
    checks: list[dict] = []

    group = ["Honesty"]

    def add(cid, label, items, fail_if_any=True, detail_ok="All clear", hint=""):
        status = "pass" if not items else ("fail" if fail_if_any else "warn")
        for it in items:
            it.setdefault("severity", status)
        checks.append({"id": cid, "label": label, "status": status, "items": items,
                       "detail": detail_ok if not items else hint, "group": group[0]})

    # 1. Source tags exist and are valid
    untagged, bad_ids = [], []
    fact_paths: dict[str, list[str]] = {}
    for path, label, text, facts in tagged_lines(cv):
        valid = [f for f in facts if f in ids]
        if text.strip():
            for fid in valid:
                fact_paths.setdefault(fid, []).append(path)
        if len(valid) != len(facts):
            bad_ids.append({"path": path, "text": text,
                            "note": "unknown IDs: " + ", ".join(f for f in facts if f not in ids)})
        if not valid and text.strip():
            untagged.append({"path": path, "text": text, "note": f"{label}: no valid source tag"})
    add("sources", "Every line traces to your master CV", untagged + bad_ids,
        hint="Lines without a valid fact ID may be invented. Tag, edit or delete them.")

    # 2. Numbers grounded
    stray = []
    for path, label, text, facts in tagged_lines(cv):
        cited = "\n".join(ids.get(f, "") for f in facts)
        cited_nums = {_num_key(n) for n in NUM.findall(cited)}
        for tok in NUM.findall(text):
            k = _num_key(tok)
            if not k or len(k.strip(".")) == 0:
                continue
            if k not in cited_nums:
                where = "elsewhere in your CV" if k in bank_nums else "nowhere in your master CV"
                stray.append({"path": path, "text": text,
                              "note": f"'{tok.strip()}' is not in the cited facts ({where})"})
    add("numbers", "Numbers match your master CV exactly", stray,
        hint="A changed or invented number is the fastest way to fail a reference check.")

    # 3. Employers, titles, dates grounded
    drift = []
    for i, e in enumerate(cv["experience"]):
        for field in ("company", "title"):
            v = e.get(field, "")
            if v and _fold(v) not in bank_fold and _words(v) not in bank_words:
                drift.append({"path": f"experience.{i}.{field}", "text": v,
                              "note": f"{field} not found word-for-word in your master CV"})
        for y in re.findall(r"\b(?:19|20)\d\d\b", e.get("dates", "")):
            if y not in bank_fold:
                drift.append({"path": f"experience.{i}.dates", "text": e.get("dates", ""),
                              "note": f"year {y} not in your master CV"})
    for i, ed in enumerate(cv["education"]):
        v = ed.get("institution", "")
        if v and _fold(v) not in bank_fold and _words(v) not in bank_words:
            drift.append({"path": f"education.{i}.institution", "text": v,
                          "note": "institution not found word-for-word in your master CV"})
    add("employers", "Employers, titles and dates unchanged", drift, fail_if_any=False,
        hint="Check these against your master CV. Small wording differences are fine; changed facts are not.")

    group[0] = "ATS & format"
    # 4. Dates ATS-safe
    dates = []
    for i, e in enumerate(cv["experience"]):
        d = e.get("dates", "")
        parts = re.split(r"\s+-\s+|\s*[–—]\s*|\s+to\s+", d, maxsplit=1)
        start, end = (_ym(parts[0], False), _ym(parts[1], True)) if len(parts) == 2 else (None, None)
        if start and end and end < start:
            note = "end date is before the start date"
        elif DASHES.search(d) or not DATE_RANGE.search(d) or not (start and end):
            note = "use both ends of the range and a regular hyphen, e.g. 'Mar 2021 - Present'"
        else:
            continue
        dates.append({"path": f"experience.{i}.dates", "text": d, "note": note})
    add("dates", "Date ranges parse in an ATS", dates, fail_if_any=False,
        hint="Workday-style parsers drop en-dash ranges and single years (05-cv-templates.md).")

    # 5. Contact details as text
    c = cv.get("contact", {})
    missing = [{"path": "contact", "text": k, "note": f"no {k} on the CV"}
               for k in ("email", "phone") if not (c.get(k) or "").strip()]
    add("contact", "Email and phone printed as text", missing,
        hint="An ATS cannot read a contact detail it cannot see.")

    group[0] = "Spelling & grammar"
    spell, grammar = spelling.check_cv(cv, list(tagged_lines(cv)), ["\n".join(ids.values()), posting_text])
    add("spelling", "UK spelling", spell, fail_if_any=False,
        detail_ok="No typos or American spellings", hint="Recruiters read a typo as carelessness. UK employers expect UK spelling.")
    add("grammar", "Grammar", grammar, fail_if_any=False,
        detail_ok="No grammar slips found", hint="Past roles in the past tense; one word per slip.")

    group[0] = "Wording"
    # 6. Writing style
    style = []
    openers: dict[str, list[str]] = {}
    seen_lines: set[str] = set()
    for path, text in bullets(cv):
        words = text.split()
        if len(words) > 35:
            style.append({"path": path, "text": text, "note": f"{len(words)} words (max 35)"})
        if words:
            openers.setdefault(words[0].lower().strip(",."), []).append(path)
        if len(words) >= 5:
            normal = _words(text)
            if normal in seen_lines:
                style.append({"path": path, "text": text, "note": "duplicates an earlier bullet"})
            seen_lines.add(normal)
    for verb, paths in openers.items():
        if len(paths) > 1:
            for p in paths[1:]:
                style.append({"path": p, "text": verb, "note": f"opening verb '{verb}' reused"})
    add("bullets", "Bullets are tight and varied", style, fail_if_any=False,
        hint="Keep bullets within 35 words and vary repeated openings or duplicated text.")

    banned = []
    for path, label, text, facts in tagged_lines(cv):
        if DASHES.search(text):
            banned.append({"path": path, "text": text, "note": "em or en dash"})
        low = text.lower()
        for f in FILLER:
            if re.search(r"\b" + re.escape(f) + r"\b", low):
                banned.append({"path": path, "text": text, "note": f"filler: '{f}'"})
        if not path.startswith("summary") and PRONOUNS.search(text):
            banned.append({"path": path, "text": text, "note": "first-person pronoun in a bullet"})
    add("style", "No clichés, dashes or pronouns", banned, fail_if_any=False,
        hint="From the repo's 03-writing-style.md.")

    # 7. Length budget (~2 A4 pages)
    words = len(cv_text(cv).split())
    length_items = []
    if words > 900:
        length_items.append({"path": "cv", "text": "", "note": f"{words} words, likely over 2 pages"})
    elif words < 380:
        length_items.append({"path": "cv", "text": "", "note": f"{words} words, may look thin"})
    add("length", "Fits two A4 pages", length_items, fail_if_any=False,
        detail_ok=f"{words} words")

    # ---------------------------------------------------------------- cover letter
    cl = draft.get("cover_letter")
    if cl:
        group[0] = "Cover letter"
        lines = [(f"cover.paragraphs.{i}", p) for i, p in enumerate(cl["paragraphs"])] + \
                [(f"cover.bullets.{i}", b) for i, b in enumerate(cl["bullets"])]
        problems = []
        # Numbers the letter quotes from the advert itself (a job reference, "30,000 homes") are fine.
        posting_nums = {_num_key(n) for n in NUM.findall(posting_text or "")}
        for path, item in lines:
            text, facts = item.get("text", ""), item.get("facts", [])
            valid = [f for f in facts if f in ids]
            cited_nums = {_num_key(n) for n in NUM.findall("\n".join(ids.get(f, "") for f in valid))}
            for tok in NUM.findall(text):
                k = _num_key(tok)
                if k not in cited_nums and k not in posting_nums:
                    problems.append({"path": path, "text": text,
                                     "note": f"'{tok.strip()}' is not in the cited facts or the advert"})
            own = [t for t in NUM.findall(text) if _num_key(t) not in posting_nums]
            if not valid and own:
                problems.append({"path": path, "text": text, "note": "claim with a number but no source fact"})
            if DASHES.search(text):
                problems.append({"path": path, "text": text, "note": "em or en dash", "severity": "warn"})
        add("cover_facts", "Letter claims match your master CV", problems,
            hint="Same rule as the CV: no number or claim your facts don't support.")
        wc = len(" ".join([cl.get("greeting", "")] + [x.get("text", "") for _, x in lines]
                          + [cl.get("closing", "")]).split())
        add("cover_len", "Letter fits one page", [] if 220 <= wc <= 430 else
            [{"path": "cover", "text": "", "note": f"{wc} words (aim for 280-400)"}], fail_if_any=False,
            detail_ok=f"{wc} words")

    # ---------------------------------------------------------------- first 10 seconds
    group[0] = "First 10 seconds"
    first_ten_seconds(cv, (draft.get("target") or {}).get("role", ""), ids, add)

    # ---------------------------------------------------------------- recruiter checks
    group[0] = "Recruiter"
    recruiter_checks(cv, ids, add)
    interview_risks(cv, draft.get("stretch") or [], add)

    # ---------------------------------------------------------------- requirements & match
    # "Apply through the careers portal" is how to apply, not something to be good at: keep it out of the score.
    for r in draft["requirements"]:
        r["how_to_apply"] = bool(HOW_TO_APPLY.search(r.get("requirement", "")))
    reqs = [r for r in draft["requirements"] if not r["how_to_apply"]]
    downgraded = []
    for r in reqs:
        valid = [f for f in r.get("fact_ids", []) if f in ids]
        r["fact_ids"] = valid
        if r["covered"] in ("yes", "partial") and not valid:
            downgraded.append(r["id"])
            r["covered"] = "no"
            r["evidence"] = (r.get("evidence", "") + " [no valid evidence tag, marked as gap]").strip()
        r["cv_paths"] = list(dict.fromkeys(path for fid in valid for path in fact_paths.get(fid, [])))
        r["on_cv"] = bool(r["cv_paths"]) or (
            r["covered"] == "listed" and _fold(r.get("requirement", "")) in _fold(cv_text(cv)))

    # Match = how well THIS CV shows you meet the job. Evidence only in your master CV counts half,
    # so surfacing it (AI Help, Fix issues) or adding a true skill moves the score.
    kw = keyword_table(draft, cv, bank_fold)
    kw_on = {k["req"] for k in kw["rows"] if k.get("req") and k["backed_on_cv"]}
    for r in reqs:
        r["effective"] = r["covered"]
        if r["covered"] == "no" and r["id"] in kw_on:
            r["effective"], r["on_cv"] = "listed", True  # a skill you added that this requirement names
        if r["covered"] == "listed" and r["id"] in kw_on:
            r["on_cv"] = True
        r["shown"] = 1.0 if r["on_cv"] else 0.5 if r["effective"] in ("yes", "partial") else 0.0
    hidden = [{"path": f"req.{r['id']}", "text": r.get("requirement", ""),
               "note": "evidence exists in your master CV but is not on this CV"}
              for r in reqs if r["status"] == "essential" and r["covered"] in ("yes", "partial")
              and not r["on_cv"]]
    group[0] = "Job match"
    add("essentials", "Evidenced essentials appear on the CV", hidden, fail_if_any=True,
        hint="Shortlisting panels score against the person spec. Surface this evidence.")
    gaps = [{"path": f"req.{r['id']}", "text": r.get("requirement", ""),
             "note": "essential criterion with no evidence in your master CV: prepare an honest "
                     "answer, or add a true fact if you do have it"}
            for r in reqs if r["status"] == "essential" and r["effective"] == "no"]
    add("skillgaps", "Skill gaps against essential criteria", gaps, fail_if_any=False,
        hint="Never stuff these into the CV. Address them in the statement or at interview.")
    den = sum(STATUS_WEIGHT[r["status"]] for r in reqs)
    num = pot = 0.0
    for r in reqs:
        weight = STATUS_WEIGHT[r["status"]]
        available = weight * COVER_VALUE[r["effective"]]
        earned = available * r["shown"]
        num += earned
        pot += available
        r["match_points"] = {"earned": round(100 * earned / den, 1),
                             "maximum": round(100 * weight / den, 1)}
    match = round(100 * num / den) if den else None
    potential = round(100 * pot / den) if den else None
    counts = {}
    for r in reqs:
        key = (r["status"], r["effective"])
        counts[key] = counts.get(key, 0) + 1

    def cnt(status, cov):
        return counts.get((status, cov), 0)

    ess_total = sum(1 for r in reqs if r["status"] == "essential")
    coverage = {
        "essential": {c: cnt("essential", c) for c in COVER_VALUE} | {"total": ess_total},
        "preferred": {c: cnt("preferred", c) for c in COVER_VALUE}
        | {"total": sum(1 for r in reqs if r["status"] == "preferred")},
        "not supplied": {c: cnt("not supplied", c) for c in COVER_VALUE}
        | {"total": sum(1 for r in reqs if r["status"] == "not supplied")},
    }
    formula = (f"Σ(weight × coverage × on CV) / Σ(weight) = {num:g} / {den} "
               "(essential ×2, other ×1; yes 1, partial 0.5, listed 0.25, no 0; "
               "on this CV 1, only in your master CV 0.5)") if den else ""

    weights = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    quality = round(100 * sum(weights[c["status"]] for c in checks) / len(checks))
    verdict = None
    if match is not None:
        verdict = "strong" if match >= 75 else "moderate" if match >= 50 else "weak"

    draft["_checks"] = checks
    draft["_checks_version"] = CHECKS_VERSION
    draft["_keywords"] = kw
    draft["_review_first"] = review_first(cv, checks, ids)
    draft["_scores"] = {"match": match, "potential": potential, "quality": quality, "verdict": verdict,
                        "formula": formula, "coverage": coverage, "words": words,
                        "downgraded": downgraded, "keywords": kw["score"], "ready_at": 85}
    return draft


# ----------------------------------------------------------------------------- recruiter checks

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
WEAK_OPENERS = {"assisted", "supported", "participated", "involved", "contributed", "helped",
                "worked", "responsible", "tasked", "exposure", "familiar", "aided"}
UNBACKED = ["excellent", "outstanding", "world-class", "exceptional", "proven track record",
            "highly motivated", "dynamic", "strong communicator", "expert in", "best-in-class",
            "guru", "ninja", "rockstar"]
AGE_WORDS = re.compile(r"\b(date of birth|dob|aged? \d\d|\d\d years old|born in|nationality|"
                       r"marital status|married|single|religion)\b", re.I)


def _ym(token: str, end: bool):
    import datetime as _dt
    t = token.strip().lower()
    if t in ("present", "current", "now", "date", "ongoing"):
        today = _dt.date.today()
        return today.year * 12 + today.month
    m = re.search(r"([a-z]{3})[a-z]*\.?\s+((?:19|20)\d\d)", t)
    if m and m.group(1) in MONTHS:
        return int(m.group(2)) * 12 + MONTHS[m.group(1)]
    y = re.search(r"(19|20)\d\d", t)
    if y:
        return int(y.group(0)) * 12 + (12 if end else 1)
    return None


def parse_range(dates: str):
    parts = re.split(r"\s+-\s+|\s*[–—]\s*|\s+to\s+", dates or "", maxsplit=1)
    if len(parts) != 2:
        return None
    a, b = _ym(parts[0], False), _ym(parts[1], True)
    return (a, b) if a and b and b >= a else None


def _fmt(ym: int) -> str:
    y, m = divmod(ym - 1, 12)
    return f"{list(MONTHS)[m].title()} {y}"


def recruiter_checks(cv: dict, ids: dict, add) -> None:
    import datetime as _dt
    this_year = _dt.date.today().year

    # Employment gaps (> 3 months between roles), the way a recruiter scans the timeline.
    spans = []
    for i, e in enumerate(cv["experience"]):
        r = parse_range(e.get("dates", ""))
        if r:
            spans.append((r[0], r[1], i))
    spans.sort()
    # Time spent studying (an education entry with a date range) is not a gap: a recruiter reads it off the CV.
    study = [(r[0], r[1], ed.get("qualification", "")) for ed in cv.get("education") or []
             if (r := parse_range(ed.get("dates", "")))]
    gaps, covered_to = [], None
    for start, end, i in spans:
        if covered_to is not None and start - covered_to > 3:
            g0, g1 = covered_to + 1, start - 1
            free = [m for m in range(g0, g1 + 1) if not any(a <= m <= b for a, b, _q in study)]
            by = [q for a, b, q in study if a <= g1 and b >= g0]
            if len(free) > 3:
                note = (f"{len(free)} month gap ({_fmt(free[0])} to {_fmt(free[-1])})" +
                        (f", after counting your {', '.join(by)}" if by else "") +
                        ". Be ready to explain it, or add the study, volunteering or contract work that filled it")
                gaps.append({"path": f"experience.{i}.dates", "text": cv["experience"][i].get("dates", ""), "note": note})
        covered_to = end if covered_to is None else max(covered_to, end)
    add("gaps", "Employment gaps", gaps, fail_if_any=False,
        detail_ok="No gaps over 3 months between the roles and study shown",
        hint="Recruiters notice unexplained gaps first. Hidden roles also create gaps.")

    # Age-bias signals (UK Equality Act 2010 protects age; recruiters advise trimming old dates).
    age = []
    for i, ed in enumerate(cv["education"]):
        years = [int(y) for y in re.findall(r"\b(?:19|20)\d\d\b", ed.get("dates", ""))]
        if years and this_year - max(years) > 20:
            age.append({"path": f"education.{i}", "text": ed.get("qualification", ""),
                        "note": f"graduation year {max(years)} lets a reader estimate your age; "
                                "you can drop the dates on qualifications over 20 years old"})
    for i, e in enumerate(cv["experience"]):
        r = parse_range(e.get("dates", ""))
        if r and this_year - r[0] // 12 > 20:
            age.append({"path": f"experience.{i}.dates", "text": e.get("dates", ""),
                        "note": "role starts over 20 years ago; consider an 'Earlier career' line "
                                "without dates"})
    for path, _label, text, _f in tagged_lines(cv):
        if AGE_WORDS.search(text):
            age.append({"path": path, "text": text, "note": "personal detail UK employers should "
                        "not ask for (age, birth date, marital status, religion)"})
    add("age", "Age-bias signals", age, fail_if_any=False,
        detail_ok="No dates or details that reveal your age",
        hint="Not wrong to include, but each one invites bias you can't see.")

    # Quantified impact: bullets without a number, and especially those whose own cited fact
    # has a number you left out.
    unquant, total, with_num = [], 0, 0
    for path, text in bullets(cv):
        total += 1
        if NUM.search(text):
            with_num += 1
            continue
        line = next((l for l in tagged_lines(cv) if l[0] == path), None)
        cited = " ".join(ids.get(f, "") for f in (line[3] if line else []))
        if NUM.search(cited):
            unquant.append({"path": path, "text": text,
                            "note": "your cited fact has a number this bullet leaves out"})
    share = round(100 * with_num / total) if total else 0
    add("impact", "Quantified impact", unquant, fail_if_any=False,
        detail_ok=f"{share}% of bullets carry a number; no unused numbers in your facts",
        hint=f"{share}% of bullets carry a number. These could carry one honestly.")

    # Missing results: bullets with no number anywhere in their evidence. The app never invents
    # one; it asks the candidate what changed, saves the answer as a fact, then rewrites the line.
    ask = []
    for path, text in bullets(cv):
        if NUM.search(text):
            continue
        line = next((l for l in tagged_lines(cv) if l[0] == path), None)
        cited = " ".join(ids.get(f, "") for f in (line[3] if line else []))
        if not NUM.search(cited):
            ask.append({"path": path, "text": text, "severity": "info", "ask": True,
                        "note": "no measurable result in this line or its facts"})
    add("evidence", "Results you could add", ask[:10], fail_if_any=False,
        detail_ok="Every bullet's evidence includes a measurable result",
        hint="These lines have no number in them or in their facts. If you know the result (how many, how much "
             "faster, how much saved, how often), add it: it's saved as a fact and the line is rewritten from it. "
             "Leave it if you don't know: an invented figure falls apart in interview.")

    # Scope & ownership: weak openers that hide what you owned.
    weak = []
    for path, text in bullets(cv):
        first = (text.split() or [""])[0].lower().strip(",.")
        if first in WEAK_OPENERS:
            weak.append({"path": path, "text": text,
                         "note": f"'{first}' hides your role. Say what you did yourself, if the fact supports it"})
    add("ownership", "Scope & ownership", weak, fail_if_any=False,
        detail_ok="Every bullet leads with what you did",
        hint="Recruiters read 'supported' and 'involved in' as 'someone else led it'.")

    # Credibility: self-praise with no evidence attached.
    cred = []
    for path, _label, text, _f in tagged_lines(cv):
        low = text.lower()
        for w in UNBACKED:
            if w in low and not NUM.search(text):
                cred.append({"path": path, "text": text,
                             "note": f"'{w}' with no number or example beside it reads as filler"})
                break
    add("credibility", "Credibility", cred, fail_if_any=False,
        detail_ok="No unbacked self-praise",
        hint="Show it instead: a result, a scale or a named outcome.")


# ----------------------------------------------------------------------------- interview risks
# Lines a recruiter will probe: wording that blurs who did the work, and lines the tailoring reframed
# toward the advert (the "stretch" list). Each comes with the question you should be ready for.

SHARED_CREDIT = re.compile(r"\b(partnered with|in partnership with|alongside|in collaboration with|collaborated with|"
                           r"with the support of|supported by|as part of a team|jointly|co-led|assisted (?:with|in)|"
                           r"contributed to|involved in|participated in)\b", re.I)


def interview_risks(cv: dict, stretch: list, add) -> None:
    risks, seen = [], set()
    lines = list(bullets(cv))
    for s in stretch:
        st = _fold(s.get("text", ""))[:60]
        hit = next(((p, t) for p, t in lines if st and (st in _fold(t) or _fold(t)[:60] in _fold(s.get("text", "")))), None)
        if hit and hit[0] not in seen:
            seen.add(hit[0])
            risks.append({"path": hit[0], "text": hit[1],
                          "note": "reframed toward the advert's wording" + (f": {s.get('why')}" if s.get("why") else ""),
                          "question": "Walk me through exactly what you did there. Is it the same work this role needs?"})
    for path, text in lines:
        m = SHARED_CREDIT.search(text)
        if m and path not in seen:
            seen.add(path)
            risks.append({"path": path, "text": text,
                          "note": f"'{m.group(1)}': clarify whether you led it or supported someone who did",
                          "question": "Did you personally build and run this, or did others do it while you advised?"})
    add("interview", "Interview risks", risks, fail_if_any=False,
        detail_ok="No lines that blur who did the work",
        hint="Say what you did yourself, e.g. 'Led a team of 3 to…' or 'Provided the data model that…'.")


# ----------------------------------------------------------------------------- first 10 seconds
# A recruiter reads the headline, the summary and the first role, then decides. These checks look at
# exactly that: a title you can stand behind, proof near the top, and the strongest role and result first.

SENIORITY = ["staff", "principal", "head of", "director", "chief", "vp", "vice president", "senior",
             "lead", "manager", "architect"]
ROLE_NOUNS = ["engineer", "developer", "analyst", "scientist", "architect", "consultant", "manager",
              "designer", "administrator", "specialist"]
PART_TIME = re.compile(r"part[- ]time|placement|internship|volunteer|freelance", re.I)


def headline_problems(subtitle: str, titles: list[str]) -> list[str]:
    head = _fold(re.split(r"\s\|\s|\|", subtitle or "", maxsplit=1)[0])
    held = " ".join(_fold(t) for t in titles)
    notes = [f"headline says '{w}' but none of your job titles do: use a title you can stand behind"
             for w in SENIORITY if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", head)
             and not re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", held)]
    if len((subtitle or "").split()) > 14:
        notes.append(f"{len(subtitle.split())} words: keep the headline to one line (12 or fewer)")
    return notes


def first_ten_seconds(cv: dict, target_role: str, ids: dict, add) -> None:
    exp = cv.get("experience") or []
    titles = [e.get("title", "") for e in exp]
    head = [{"path": "subtitle", "text": cv.get("subtitle", ""), "note": n}
            for n in headline_problems(cv.get("subtitle", ""), titles)]
    if not (cv.get("subtitle") or "").strip():
        head.append({"path": "subtitle", "text": "", "note": "no headline under your name: add the role you're targeting"})
    add("headline", "Headline is a title you can stand behind", head,
        hint="It's the first line read after your name. Claiming a title you haven't held fails the first check.")

    summary = " ".join(x.get("text", "") for x in cv.get("summary") or [])
    proof = []
    bank_has_numbers = any(NUM.search(t) for t in ids.values())
    wins = " ".join(x.get("text", "") for x in cv.get("achievements") or [])
    if summary and not NUM.search(summary) and not NUM.search(wins) and bank_has_numbers:
        proof.append({"path": "summary.0", "text": summary[:200],
                      "note": "no number in your profile or key achievements: put your strongest result near the top (exactly as your facts state it)"})
    if re.search(r"\b(hands-on experience|passionate|results-driven|dynamic|team player|go-getter)\b", summary, re.I):
        proof.append({"path": "summary.0", "text": summary[:200], "note": "generic opener: say who you are and the scope you work at"})
    add("top_proof", "Proof in the top third", proof, fail_if_any=False,
        hint="Recruiters decide in seconds. A real result in the profile is what makes them read on.")

    order = []
    current = [i for i, e in enumerate(exp) if re.search(r"present|current|now", e.get("dates", ""), re.I)]
    if exp and PART_TIME.search(exp[0].get("title", "")) and any(not PART_TIME.search(exp[i].get("title", "")) for i in current[1:]):
        main = next(i for i in current[1:] if not PART_TIME.search(exp[i].get("title", "")))
        order.append({"path": "experience.0", "text": exp[0].get("title", ""),
                      "note": f"a part-time role is first: move '{exp[main].get('title', '')}' above it (↑ on the role)"})
    for i, e in enumerate(exp[:2]):
        b = e.get("bullets") or []
        if len(b) > 1 and not NUM.search(b[0].get("text", "")) and any(NUM.search(x.get("text", "")) for x in b[1:]):
            order.append({"path": f"experience.{i}.bullets.0", "text": b[0].get("text", ""),
                          "note": f"first bullet in '{e.get('title', '')}' has no result while a later one does: lead with your strongest"})
    words = re.findall(r"[a-z]+", _fold(re.split(r"[,(|]", target_role or "")[0]))
    noun = next((i for i in range(len(words) - 1, -1, -1) if words[i] in ROLE_NOUNS), None)
    phrase = " ".join(words[max(0, noun - 1):noun + 1]) if noun is not None else ""  # "software engineer"
    for i, e in enumerate(exp):
        if phrase and not (e.get("bullets") or []) and phrase in " ".join(re.findall(r"[a-z]+", _fold(e.get("title", "")))):
            order.append({"path": f"experience.{i}", "text": e.get("title", ""),
                          "note": f"'{e.get('title', '')}' matches the job you're applying for but shows no bullets: add 1-2 from your facts"})
    add("lead_first", "Strongest role and result first", order, fail_if_any=False,
        hint="The first role and its first bullet get read; the rest gets skimmed.")


# ----------------------------------------------------------------------------- ATS keywords

KW_WEIGHT = {"high": 3, "medium": 2, "low": 1}


def keyword_table(draft: dict, cv: dict, bank_fold: str) -> dict:
    text = _fold(cv_text(cv))
    dismissed = {k.lower() for k in draft.get("dismissed_keywords", [])}
    rows, num, den = [], 0, 0
    for k in draft.get("keywords", []) or []:
        term = (k.get("term") or "").strip()
        if not term:
            continue
        pr = (k.get("priority") or "medium").lower()
        pr = pr if pr in KW_WEIGHT else "medium"
        alts = [a.strip() for a in re.split(r"\s+/\s+|\s+or\s+", term, flags=re.I) if a.strip()]

        def matches(hay):
            return {a for a in alts if re.search(r"(?<![a-z0-9])" + re.escape(_fold(a)) + r"(?![a-z0-9])", hay)}

        on_cv = matches(text)
        in_bank = matches(bank_fold)
        status = "on_cv" if on_cv else "in_bank" if in_bank else "gap"
        is_dismissed = term.lower() in dismissed
        if not is_dismissed:
            den += KW_WEIGHT[pr]
            num += KW_WEIGHT[pr] if on_cv else 0
        rows.append({"term": term, "priority": pr, "status": status, "req": k.get("req", ""),
                     "dismissed": is_dismissed, "backed_on_cv": bool(on_cv & in_bank),
                     "skill_options": [a for a in alts if not in_bank or a in in_bank]})
    order = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda r: (r["dismissed"], order[r["priority"]], r["status"] == "on_cv"))
    return {"rows": rows, "score": round(100 * num / den) if den else None,
            "formula": f"weighted terms on CV {num} / {den} (high 3, medium 2, low 1; dismissed excluded)"
            if den else ""}


# ----------------------------------------------------------------------------- review first

def review_first(cv: dict, checks: list[dict], ids: dict) -> dict | None:
    """The single line most worth a look: failing checks first, then warnings, then the weakest bullet."""
    lines = {p: (t, f) for p, _l, t, f in tagged_lines(cv)}
    for severity in ("fail", "warn"):
        for c in checks:
            if c["status"] != severity:
                continue
            for it in c["items"]:
                if it["path"] in lines:
                    return {"path": it["path"], "text": lines[it["path"]][0], "reason": it["note"],
                            "check": c["label"]}
    best = None
    for path, text in bullets(cv):
        score = (0 if NUM.search(text) else 2) + (1 if len(text.split()) > 28 else 0)
        if best is None or score > best[0]:
            best = (score, path, text)
    if best:
        return {"path": best[1], "text": best[2], "reason": "the weakest remaining bullet: "
                "no number or the longest", "check": ""}
    return None
