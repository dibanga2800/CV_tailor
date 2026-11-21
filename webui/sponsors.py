"""Visa sponsorship signals for each job.

1. Licensed sponsor: the employer is matched against the Home Office "Register of licensed
   sponsors: workers" (GOV.UK, CSV, columns Organisation Name, Town/City, County,
   Type & Rating, Route). Downloaded via the GOV.UK Content API, cached in
   documents/postings/sponsors.csv (gitignored) and refreshed when older than 7 days.
2. What the advert says: wording such as "visa sponsorship available" or "unable to sponsor".

A licence means the employer *can* sponsor; whether this role qualifies still depends on its
occupation code and salary meeting the Skilled Worker rules. The UI says so.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import urllib.request

from store import DOCS, load_json, save_json

CSV_PATH = DOCS / "postings" / "sponsors.csv"
META = DOCS / "postings" / "sponsors_meta.json"
CONTENT_API = "https://www.gov.uk/api/content/government/publications/register-of-licensed-sponsors-workers"
UA = "ai-job-search-webui/1.0 (personal job search)"
MAX_AGE = 7 * 86400

SUFFIXES = {"ltd", "limited", "plc", "llp", "lp", "uk", "u.k", "group", "holdings", "the", "co", "company",
            "inc", "incorporated", "international", "gb", "europe", "trading", "services", "t/a"}
AGENCY = re.compile(r"\b(recruit\w*|resourcing|staffing|personnel|talent|search|selection|appointments|"
                    r"careers?|people|jobs|consult\w*)\b", re.I)

POSITIVE = re.compile(
    r"(visa\s+)?sponsorship\s+(is\s+|can\s+be\s+|will\s+be\s+)?(available|offered|provided|considered)"
    r"|(can|able\s+to|will|happy\s+to|willing\s+to)\s+(offer\s+|provide\s+)?(visa\s+)?sponsor"
    r"|skilled\s+worker\s+(visa\s+)?sponsorship|certificate\s+of\s+sponsorship|we\s+sponsor\s+visas", re.I)
NEGATIVE = re.compile(
    r"(unable|not\s+able|cannot|can't|can\s+not|do\s+not|don't|will\s+not|won't|are\s+not\s+able)\s+(to\s+)?"
    r"(offer\s+|provide\s+|support\s+)?(visa\s+)?sponsor"
    r"|no\s+(visa\s+)?sponsorship|sponsorship\s+(is\s+)?not\s+(available|offered|provided|possible)"
    r"|not\s+(in\s+a\s+position\s+to\s+)?(offer|provide)\s+(visa\s+)?sponsorship"
    r"|without\s+(the\s+need\s+for\s+)?(visa\s+)?sponsorship"
    r"|must\s+(already\s+)?(have|hold)\s+(the\s+|a\s+|an\s+)?(full\s+|existing\s+|current\s+)?right\s+to\s+work", re.I)


def norm(name: str) -> str:
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = [w for w in s.split() if w not in SUFFIXES]
    return " ".join(words)


_INDEX: dict | None = None


def _load_index() -> dict:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    idx: dict[str, list[dict]] = {}
    if CSV_PATH.exists():
        with CSV_PATH.open(newline="", encoding="utf-8-sig", errors="replace") as f:
            for row in csv.DictReader(f):
                name = row.get("Organisation Name") or row.get("Organisation name") or ""
                route = row.get("Route", "")
                key = norm(name)
                if not key:
                    continue
                idx.setdefault(key, []).append({
                    "name": name.strip(), "town": (row.get("Town/City") or "").strip(),
                    "county": (row.get("County") or "").strip(),
                    "rating": (row.get("Type & Rating") or "").strip(), "route": route.strip()})
    first: dict[str, list[str]] = {}
    for k in idx:
        first.setdefault(k.split()[0], []).append(k)
    _INDEX = {"by_name": idx, "by_first": first}
    return _INDEX


def status() -> dict:
    meta = load_json(META, {}) or {}
    idx = _load_index()
    return {"loaded": bool(idx["by_name"]), "organisations": len(idx["by_name"]),
            "updated": meta.get("updated"), "source": meta.get("source", ""),
            "stale": not meta.get("fetched") or time.time() - meta["fetched"] > MAX_AGE}


def refresh() -> dict:
    """Download the latest register CSV from GOV.UK."""
    global _INDEX
    req = urllib.request.Request(CONTENT_API, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        content = json.loads(r.read().decode("utf-8"))
    urls = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and re.search(r"assets\.publishing\.service\.gov\.uk/.+\.csv$", o):
            urls.append(o)
    walk(content.get("details", content))
    if not urls:
        raise RuntimeError("Couldn't find the CSV link on GOV.UK. Download it from the 'Register of "
                           "licensed sponsors: workers' page and use Upload register instead.")
    url = urls[0]
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    return save_csv(data, url)


def save_csv(data: bytes, source: str = "uploaded") -> dict:
    global _INDEX
    text = data.decode("utf-8-sig", errors="replace")
    head = next(csv.reader(io.StringIO(text)), [])
    if not any("Organisation" in h for h in head):
        raise ValueError("That file doesn't look like the sponsor register (no 'Organisation Name' column).")
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    CSV_PATH.write_text(text, encoding="utf-8")
    m = re.search(r"(\d{4}-\d{2}-\d{2})", source)
    save_json(META, {"fetched": time.time(), "source": source,
                     "updated": m.group(1) if m else time.strftime("%Y-%m-%d")})
    _INDEX = None
    return status()


def lookup(company: str) -> dict | None:
    """Exact match on the normalised legal name, then a cautious prefix match."""
    idx = _load_index()
    if not idx["by_name"]:
        return None
    key = norm(company)
    if not key:
        return None
    rows = idx["by_name"].get(key)
    match = "exact"
    if not rows and len(key) >= 6:
        cands = [k for k in idx["by_first"].get(key.split()[0], [])
                 if k.startswith(key + " ") or key.startswith(k + " ")]
        if len(cands) == 1:
            rows, match = idx["by_name"][cands[0]], "close"
    if not rows:
        return None
    skilled = [r for r in rows if "skilled worker" in r["route"].lower()]
    best = (skilled or rows)[0]
    a_rated = any("a rating" in r["rating"].lower() or "(a" in r["rating"].lower() for r in (skilled or rows))
    return {"name": best["name"], "town": best["town"], "rating": best["rating"],
            "routes": sorted({r["route"] for r in rows}), "skilled_worker": bool(skilled),
            "a_rated": a_rated, "match": match}


def advert_says(text: str) -> str:
    t = text or ""
    if NEGATIVE.search(t):
        return "no"
    if POSITIVE.search(t):
        return "yes"
    return "unknown"


NOT_AGENCY = re.compile(r"\b(NHS|Council|University|Trust|Police|Civil Service|HM |Ministry|Department)\b", re.I)


def annotate(job: dict) -> dict:
    lic = lookup(job.get("company", ""))
    advert = advert_says(f"{job.get('title', '')}\n{job.get('description', '')}")
    if advert == "unknown" and job.get("visa_sponsorship"):
        advert = "yes"  # the source itself flags the job as offering sponsorship
    return {"licence": lic, "advert": advert,
            "agency": bool(AGENCY.search(job.get("company", ""))) and not NOT_AGENCY.search(job.get("company", ""))}


# ----------------------------------------------------------------------------- sponsorship salary check
# Skilled Worker salary rules as published on GOV.UK (going rates table last updated 22 July 2025;
# rechecked November 2026). The employer chooses the occupation (SOC) code; the app only guesses
# the likely one from the job title, and says so. Not immigration advice.
RULES_CHECKED = "November 2026"
GENERAL_THRESHOLD = 41_700
NEW_ENTRANT_FLOOR = 33_400
SOC = {  # code: (occupation, going rate, degree level?)
    "2131": ("IT project managers", 58_200, True),
    "2132": ("IT managers", 55_000, True),
    "2133": ("IT business analysts, architects and systems designers", 54_900, True),
    "2134": ("Programmers and software development professionals", 54_700, True),
    "2139": ("IT professionals not elsewhere classified", 52_300, True),
    "2433": ("Actuaries, economists and statisticians", 55_100, True),
    "3544": ("Data analysts", 34_900, False),
}
TSL_DEADLINE = "31 December 2026"
_TITLE_SOC = [
    (r"\b(head of data|data (?:and|&) analytics manager|data manager|bi manager|it manager|head of (?:bi|analytics|it))\b", "2132"),
    (r"\b(?:data|bi|business intelligence|analytics|reporting|information|insights?)\b.{0,25}\bmanager\b"
     r"|\bmanager\b.{0,12}\b(?:bi|data|analytics)\b", "2132"),
    (r"\b(project manager|programme manager)\b", "2131"),
    (r"\b(data scientist|statistician|economist)\b", "2433"),
    (r"\b(data engineer|data architect|analytics engineer|analytical engineer|data platform|solutions architect|"
     r"bi architect|cloud architect|technical architect|business analyst|systems designer|bi engineer|"
     r"data engineering|data products? engineer)\b", "2133"),
    (r"\b(?:data|bi|business intelligence|analytics|reporting|insights?|data technical)\b.{0,20}\b(?:lead|tech lead|leader)\b"
     r"|\blead\b.{0,20}\b(?:bi|data|analytics|business intelligence)\b", "2133"),
    (r"\b(?:ai engineer|machine learning engineer|ml engineer)\b", "2134"),
    (r"\b(devops|it consultant|platform engineer|site reliability)\b"
     r"|\b(?:power bi|bi|data|analytics|business intelligence)\b.{0,20}\b(?:consultant|specialist)\b", "2139"),
    (r"\b(developer|software engineer|programmer|power bi developer|bi developer|etl developer)\b", "2134"),
    (r"\b(data analyst|information analyst|bi analyst|reporting analyst|insight analyst|mi analyst|"
     r"business intelligence analyst|performance analyst|analytics analyst|data & analytics|"
     r"principal analyst|information officer|performance and information)\b", "3544"),
]


def _money_range(text: str) -> tuple[int | None, int | None, bool]:
    """'£45,000 - £55,000 a year' -> (45000, 55000, annual?). Day/hour rates return (None, None, False)."""
    t = (text or "").replace(",", "").lower()
    if re.search(r"per (?:day|hour)|/day|/hr|p/?d\b|a day|an hour|per hr", t):
        return None, None, False
    nums = [float(n) * (1000 if k else 1) for n, k in re.findall(r"£\s?(\d+(?:\.\d+)?)\s*(k)?\b", t)]
    nums = [int(n) for n in nums if 10_000 <= n <= 500_000]
    if not nums:
        return None, None, True
    return min(nums), max(nums), True


def salary_in_text(text: str) -> tuple[int | None, int | None, bool]:
    """Find the advertised pay in the advert text, when the board gave no salary field.
    Lines that mention salary or pay are tried first; a day or hourly rate returns annual=False."""
    lines = [l for l in (text or "")[:8000].splitlines() if "£" in l]
    lines.sort(key=lambda l: not re.search(r"salary|pay|per annum|\bpa\b|a year|per year|rate", l, re.I))
    for line in lines:
        lo, hi, annual = _money_range(line)
        if not annual:
            return None, None, False
        if lo and lo >= 15_000:
            return lo, hi, True
    return None, None, True


def salary_check(job: dict) -> dict | None:
    title = job.get("title", "")
    code = next((c for rx, c in _TITLE_SOC if re.search(rx, title, re.I)), None)
    if not code:
        return None
    occ, going, degree = SOC[code]
    need = max(GENERAL_THRESHOLD, going)
    lo, hi, annual = _money_range(job.get("salary", ""))
    from_text = False
    if lo is None and annual and not job.get("salary"):
        lo, hi, annual, from_text = (*salary_in_text(job.get("description", "")), True)
    notes = [f"Likely occupation code {code} ({occ}); the employer picks the code, so treat this as a guide.",
             f"Needed: £{need:,} a year (the higher of the £{GENERAL_THRESHOLD:,} general threshold and the "
             f"£{going:,} going rate), for a 37.5-hour week."]
    if not degree:
        notes.append(f"Below degree level: only open through the Temporary Shortage List, for certificates of "
                     f"sponsorship issued before {TSL_DEADLINE}, and new applicants generally can't bring dependants.")
    if not annual:
        verdict = "unknown"
        notes.append("Pay is a day or hourly rate (usually a contract): Skilled Worker sponsorship is for employed roles.")
    elif lo is None:
        verdict = "unknown"
        notes.append("No salary in the advert: ask before applying.")
    elif lo >= need:
        verdict = "meets"
    elif hi >= need:
        verdict = "range"
        notes.append(f"Only the top of the range (£{hi:,}) meets it: confirm the offer would be at least £{need:,}.")
    else:
        verdict = "below"
        notes.append(f"The advertised £{hi:,} is below £{need:,}. Lower rates exist for new entrants "
                     f"(at least £{NEW_ENTRANT_FLOOR:,}, e.g. switching from a Student or Graduate visa) and some PhD holders.")
    if from_text and lo is not None:
        notes.append(f"Salary read from the advert text: £{lo:,}" + (f" to £{hi:,}" if hi != lo else "") + ".")
    return {"soc": code, "occupation": occ, "going_rate": going, "needed": need, "salary_min": lo, "salary_max": hi,
            "verdict": verdict, "degree_level": degree, "notes": notes, "checked": RULES_CHECKED}
