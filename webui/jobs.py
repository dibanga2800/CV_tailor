"""Job scanner: pull vacancies from official APIs and public ATS feeds, de-duplicate them,
score a quick keyword match against your fact bank, and keep them in one store.

Sources
  reed     Reed.co.uk Jobseeker API (free key; HTTP basic auth, key as username)
  adzuna   Adzuna API, UK (free app_id + app_key; ~1,000 calls/month on the free tier)
  ats      Public Greenhouse / Lever / Ashby / Workable job-board feeds for employers you list
  nhs      NHS Jobs official search XML feed (free, no key; Trac-run trusts publish here too)
  workhub  GOV.UK Work Hub, the DWP service that replaced Find a Job (free; robots.txt allows /jobs)
  arbeitnow Arbeitnow UK job board API (free, no key; mostly jobs from employers' own ATSs)
  linkedin The repo's own guest-search CLI (.agents/skills/linkedin-search). Against
           LinkedIn's terms for automated use, so it is off by default and capped low.

Store: documents/postings/webui_jobs.json (gitignored, so a public fork never commits it). Keys are
tools/job_key.py make_key(company, title, url), the same canonical key /scrape uses.
Settings: documents/postings/job_search_settings.json. API keys: webui/.env (gitignored).
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from store import DOCS, REPO, WEBUI, load_json, save_json

sys.path.insert(0, str(REPO / "tools"))
try:
    from job_key import make_key  # the repo's canonical dedup key
except Exception:  # pragma: no cover - repo tool missing
    def make_key(company, title, url=""):
        s = re.sub(r"[^a-z0-9]+", "-", f"{company}_{title}".lower()).strip("-")
        return s[:120] or url

STORE = DOCS / "postings" / "webui_jobs.json"          # gitignored (documents/postings/**)
SETTINGS = DOCS / "postings" / "job_search_settings.json"
UA = "ai-job-search-webui/1.0 (personal job search)"
MOCK = os.environ.get("CV_TAILOR_JOBS_MOCK") == "1"

DEFAULT_SETTINGS = {
    "titles": ["Data Engineer", "Senior Data Engineer", "BI Lead", "Power BI Developer",
               "Analytics Engineer", "Data Platform Lead", "Senior Data Analyst", "Senior Information Analyst"],
    "locations": [{"name": "Stoke-on-Trent", "radius": 25}, {"name": "Birmingham", "radius": 20},
                  {"name": "Manchester", "radius": 20}],
    "remote": True,
    "salary_min": 0,
    "max_days": 14,
    "exclude": ["graduate", "apprentice", "intern"],
    "sources": {"arbeitnow": True, "nhs": True, "workhub": True, "reed": True, "adzuna": True, "ats": True,
                "linkedin": False, "himalayas": False, "wwr": False, "jobserve": False, "yugenox": False,
                "ktp": True},
    "jobserve_rss": "",
    "ats_boards": [],
    "linkedin_cap": 10,
    # Your preferences, used to rank jobs by fit (not to hide them).
    "prefs": {"modes": ["Remote", "Hybrid", "On-site"], "types": ["Permanent", "Contract", "Temporary", "Full-time"],
              "needs_sponsorship": False, "avoid_agencies": False, "notice_period": ""},
}

MANUAL_BOARDS = {
    "indeed": ("indeed.com", "indeed.co.uk"),
    "jobserve": ("jobserve.com",),
    "charityjob": ("charityjob.co.uk",),
    "academia": ("jobs.ac.uk",),
    "civilservice": ("civilservicejobs.service.gov.uk",),
    "nhs": ("jobs.nhs.uk",),
    "linkedin": ("linkedin.com",),
    "glassdoor": ("glassdoor.co.uk", "glassdoor.com"),
}
JOBSERVE_HOSTS = {"jobserve.com", "www.jobserve.com"}
WWR_HOSTS = {"weworkremotely.com", "www.weworkremotely.com"}
MAX_FEED_BYTES = 5 * 1024 * 1024


def _web_host(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme in ("https", "http") and not (parsed.username or parsed.password):
            return (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        pass
    return ""


def _feed_allowed(url: str, hosts: set[str]) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme == "https" and parsed.port in (None, 443) and _web_host(url) in hosts
    except ValueError:
        return False


# ----------------------------------------------------------------------------- settings & keys

def get_settings() -> dict:
    s = load_json(SETTINGS, None) or {}
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    out.update({k: v for k, v in s.items() if k in DEFAULT_SETTINGS})
    out["sources"] = {**DEFAULT_SETTINGS["sources"], **(s.get("sources") or {})}
    out["prefs"] = {**DEFAULT_SETTINGS["prefs"], **(s.get("prefs") or {})}
    return out


def save_settings(s: dict) -> dict:
    clean = get_settings()
    for k in DEFAULT_SETTINGS:
        if k in s:
            clean[k] = s[k]
    clean["titles"] = [str(t).strip() for t in clean["titles"] if str(t).strip()][:20]
    clean["locations"] = [{"name": str(l.get("name", "")).strip(), "radius": int(l.get("radius") or 20)}
                          for l in clean["locations"] if str(l.get("name", "")).strip()][:10]
    clean["jobserve_rss"] = str(clean["jobserve_rss"] or "").strip()
    if clean["jobserve_rss"] and not _feed_allowed(clean["jobserve_rss"], JOBSERVE_HOSTS):
        raise ValueError("Enter the HTTPS JobServe RSS alert URL from your saved search.")
    clean["ats_boards"] = [b.strip() for b in clean["ats_boards"]
                           if re.fullmatch(r"(greenhouse|lever|ashby|workable):[A-Za-z0-9_.-]+", b.strip())][:60]
    clean["exclude"] = [str(x).strip() for x in clean["exclude"] if str(x).strip()][:30]
    p = {**DEFAULT_SETTINGS["prefs"], **(clean.get("prefs") or {})}
    clean["prefs"] = {"modes": [m for m in p["modes"] if m in ("Remote", "Hybrid", "On-site")],
                      "types": [t for t in p["types"] if t in ("Permanent", "Contract", "Temporary", "Full-time", "Part-time")],
                      "needs_sponsorship": bool(p["needs_sponsorship"]), "avoid_agencies": bool(p["avoid_agencies"]),
                      "notice_period": str(p.get("notice_period", "")).strip()[:40]}
    save_json(SETTINGS, clean)
    return clean


KEY_NAMES = ("GITHUB_TOKEN", "REED_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "ENHANCV_API_KEY", "DEEPSEEK_API_KEY",
             "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def key_status() -> dict:
    return {k: bool(os.environ.get(k)) for k in KEY_NAMES} | {
        "bun": bool(bun_path()), "linkedin_cli": _linkedin_cli_path().is_file(),
    }


def save_keys(values: dict) -> dict:
    """Write keys to webui/.env (gitignored). Values are never sent back to the browser."""
    env = WEBUI / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    for k in KEY_NAMES:
        v = str(values.get(k, "")).strip()
        if not v:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_\-.]{4,300}", v):
            raise ValueError(f"{k} doesn't look like a valid key")
        lines = [l for l in lines if not l.startswith(k + "=")] + [f"{k}={v}"]
        os.environ[k] = v
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return key_status()


def save_env(name: str, value: str) -> None:
    """Set or remove one line in webui/.env (gitignored)."""
    env = WEBUI / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [l for l in lines if not l.startswith(name + "=")]
    if value:
        lines.append(f"{name}={value}")
        os.environ[name] = value
    else:
        os.environ.pop(name, None)
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------- http

def _read(req, timeout: int = 25, allow_partial: bool = False) -> bytes:
    """Fetch with one retry for dropped or cut-short downloads (IncompleteRead, reset, timeout).
    HTTP errors such as 404 are real answers and are not retried.
    allow_partial: for HTML pages whose content sits near the top (Work Hub puts every job card in
    roughly the first 90 KB of a 300 KB page), keep what arrived when the server cuts the page short."""
    import http.client
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise
        except http.client.IncompleteRead as e:
            if allow_partial and len(e.partial) > 20_000:
                return e.partial
            if attempt == 2:
                raise
            time.sleep(1.5)
        except (ConnectionError, TimeoutError, urllib.error.URLError):
            if attempt == 2:
                raise
            time.sleep(1.5)
    return b""


def _get(url: str, headers: dict | None = None, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json",
                                               **(headers or {})})
    return json.loads(_read(req, timeout).decode("utf-8", errors="replace"))


def strip_html(s: str) -> str:
    s = html.unescape(html.unescape(s or ""))
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t ]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def _days_ago(dt: datetime | None) -> int | None:
    if not dt:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _parse_dt(v) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / (1000 if v > 1e11 else 1), tz=timezone.utc)
    s = str(v)
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _job(source, sid, title, company, location, url, apply_url="", salary="", posted=None,
         description="", full=False, remote=False, extra=None) -> dict:
    dt = _parse_dt(posted)
    return {
        "source": source, "source_id": str(sid), "title": (title or "").strip(),
        "company": (company or "").strip() or "Unknown employer", "location": (location or "").strip(),
        "url": url or "", "apply_url": apply_url or url or "", "salary": salary or "",
        "posted": dt.date().isoformat() if dt else "", "age_days": _days_ago(dt),
        "description": description or "", "full_description": bool(full), "remote": bool(remote),
        **(extra or {}),
    }


def _money(lo, hi, predicted=False) -> str:
    def f(v):
        return f"£{int(v):,}" if v else ""
    s = " - ".join(x for x in (f(lo), f(hi)) if x)
    return (s + (" (estimated)" if predicted else "")) if s else ""


# ----------------------------------------------------------------------------- sources

def reed_search(title: str, loc: dict | None, s: dict) -> list[dict]:
    key = os.environ.get("REED_API_KEY")
    if not key and not MOCK:
        raise RuntimeError("Reed API key not set")
    q = {"keywords": title + ("" if loc else " remote"), "resultsToTake": 50}
    if loc:
        q.update(locationName=loc["name"], distanceFromLocation=loc["radius"])
    if s.get("salary_min"):
        q["minimumSalary"] = s["salary_min"]
    url = "https://www.reed.co.uk/api/1.0/search?" + urllib.parse.urlencode(q)
    auth = base64.b64encode(f"{key}:".encode()).decode()
    data = _mock("reed") if MOCK else _get(url, {"Authorization": f"Basic {auth}"})
    out = []
    for r in data.get("results", []):
        out.append(_job("reed", r.get("jobId"), r.get("jobTitle"), r.get("employerName"),
                        r.get("locationName"), r.get("jobUrl"), r.get("jobUrl"),
                        _money(r.get("minimumSalary"), r.get("maximumSalary")), r.get("date"),
                        strip_html(r.get("jobDescription", "")), remote=not loc))
    return out


def reed_detail(job: dict) -> dict:
    key = os.environ.get("REED_API_KEY")
    url = f"https://www.reed.co.uk/api/1.0/jobs/{urllib.parse.quote(job['source_id'])}"
    auth = base64.b64encode(f"{key}:".encode()).decode()
    d = _mock("reed_detail") if MOCK else _get(url, {"Authorization": f"Basic {auth}"})
    return {"description": strip_html(d.get("jobDescription", "")),
            "apply_url": d.get("externalUrl") or d.get("jobUrl") or job["apply_url"]}


def adzuna_search(title: str, loc: dict | None, s: dict) -> list[dict]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key) and not MOCK:
        raise RuntimeError("Adzuna app_id/app_key not set")
    q = {"app_id": app_id, "app_key": app_key, "results_per_page": 50, "what": title,
         "max_days_old": s.get("max_days") or 30, "content-type": "application/json"}
    if loc:
        q.update(where=loc["name"], distance=round(loc["radius"] * 1.609))
    else:
        q["what"] = title + " remote"
    if s.get("salary_min"):
        q["salary_min"] = s["salary_min"]
    url = "https://api.adzuna.com/v1/api/jobs/gb/search/1?" + urllib.parse.urlencode(q)
    data = _mock("adzuna") if MOCK else _get(url)
    out = []
    for r in data.get("results", []):
        out.append(_job("adzuna", r.get("id"), strip_html(r.get("title", "")),
                        (r.get("company") or {}).get("display_name"),
                        (r.get("location") or {}).get("display_name"), r.get("redirect_url"),
                        r.get("redirect_url"),
                        _money(r.get("salary_min"), r.get("salary_max"),
                               str(r.get("salary_is_predicted")) == "1"),
                        r.get("created"), strip_html(r.get("description", "")), remote=not loc,
                        extra={"salary_estimated": str(r.get("salary_is_predicted")) == "1",
                               "type_hints": [x for x in (r.get("contract_type"), r.get("contract_time")) if x]}))
    return out


def ats_board(board: str) -> list[dict]:
    kind, name = board.split(":", 1)
    out = []
    if kind == "greenhouse":
        d = _mock("greenhouse") if MOCK else _get(
            f"https://boards-api.greenhouse.io/v1/boards/{name}/jobs?content=true")
        company = d.get("meta", {}).get("name") or name
        for j in d.get("jobs", []):
            out.append(_job("greenhouse", j.get("id"), j.get("title"), j.get("company_name") or company,
                            (j.get("location") or {}).get("name"), j.get("absolute_url"),
                            j.get("absolute_url"), "", j.get("first_published") or j.get("updated_at"),
                            strip_html(j.get("content", "")), full=True))
    elif kind == "lever":
        d = _mock("lever") if MOCK else _get(f"https://api.lever.co/v0/postings/{name}?mode=json")
        for j in d:
            body = "\n\n".join([j.get("descriptionPlain", "")] + [
                f"{l.get('text', '')}\n{strip_html(l.get('content', ''))}" for l in j.get("lists", [])]
                + [j.get("additionalPlain", "")])
            cat = j.get("categories") or {}
            out.append(_job("lever", j.get("id"), j.get("text"), name.replace("-", " ").title(),
                            cat.get("location"), j.get("hostedUrl"), j.get("applyUrl"), "",
                            j.get("createdAt"), body.strip(), full=True,
                            remote=(j.get("workplaceType") == "remote"),
                            extra={"type_hints": [x for x in (cat.get("commitment"), j.get("workplaceType")) if x]}))
    elif kind == "ashby":
        d = _mock("ashby") if MOCK else _get(
            f"https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true")
        for j in d.get("jobs", []):
            comp = (j.get("compensation") or {}).get("compensationTierSummary", "")
            out.append(_job("ashby", j.get("id"), j.get("title"), name.replace("-", " ").title(),
                            j.get("location"), j.get("jobUrl"), j.get("applyUrl"), comp,
                            j.get("publishedAt"), j.get("descriptionPlain") or strip_html(
                                j.get("descriptionHtml", "")), full=True, remote=bool(j.get("isRemote")),
                            extra={"type_hints": [x for x in (j.get("employmentType"), j.get("workplaceType")) if x]}))
    elif kind == "workable":
        d = _mock("workable") if MOCK else _get(
            f"https://apply.workable.com/api/v1/widget/accounts/{name}?details=true")
        for j in d.get("jobs", []):
            loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
            out.append(_job("workable", j.get("shortcode"), j.get("title"), d.get("name") or name, loc,
                            j.get("url"), j.get("application_url") or j.get("url"), "",
                            j.get("published_on"), strip_html(j.get("description", "")),
                            full=bool(j.get("description")), remote=bool(j.get("telecommuting"))))
    return out


def nhs_search(title: str, loc: dict | None, s: dict) -> list[dict]:
    import xml.etree.ElementTree as ET
    if not loc:
        return []  # NHS roles are tied to a site; the feed has no remote filter
    # No sort parameter: with sort=publicationDate the feed ignores the keyword entirely.
    q = {"keyword": title, "location": loc["name"], "distance": loc["radius"], "limit": 50}
    url = "https://www.jobs.nhs.uk/api/v1/search_xml?" + urllib.parse.urlencode(q)
    if MOCK:
        raw = (WEBUI / "fixtures" / "jobs" / "nhs.xml").read_text(encoding="utf-8")
    else:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/xml"})
        raw = _read(req).decode("utf-8", errors="replace")
    out = []
    for v in ET.fromstring(raw).iter("vacancyDetails"):
        g = lambda k: (v.findtext(k) or "").strip()  # noqa: E731
        desc = strip_html(g("description"))
        out.append(_job("nhs", g("id") or g("reference"), g("title"), g("employer"), g("locations"),
                        g("url"), g("url"), g("salary").replace(".00", ""), g("postDate"), desc,
                        full=len(desc) > 600, extra={"closing": g("closeDate"), "reference": g("reference"),
                                                     "type_hints": [g("type")] if g("type") else []}))
    return out


KTP = "https://iuk-ktp.org.uk"


def _ktp_field(page: str, label: str) -> str:
    m = re.search(r">\s*" + re.escape(label) + r"\s*</h3>\s*<p[^>]*>(.*?)</p>", page, re.S | re.I)
    return _text(m.group(1)) if m else ""


def _uk_date(d: str) -> str:
    try:
        return datetime.strptime(d.strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return ""


def global_talent_check(months: int | None, employer: str, up_to: bool = False) -> dict:
    """Does this KTP role look like it fits the Global Talent visa's UKRI endorsed-funder fast track?

    KTPs are funded by Innovate UK (part of UKRI). The route also needs a grant of 2+ years worth
    £30,000+, a contract with 1+ year left, at least half your time on the grant, and an employer
    on UKRI's approved list. KTPs run 12 to 36 months, so length is the usual deal-breaker.
    """
    uni = bool(re.search(r"universit|college|institute|school of|research", employer, re.I))
    checks = [
        {"ok": True, "text": "Funded by Innovate UK, part of UKRI (an endorsed funder)"},
        {"ok": True, "text": "Full time on the project, so well over half your time on the grant"},
    ]
    if months is None:
        checks.append({"ok": None, "text": "Project length not shown: the grant must cover at least 2 years"})
        level = "check"
    elif months >= 24 and up_to:
        checks.append({"ok": None, "text": f"'Up to {months} months': ask if the grant itself runs the full 2 years the route needs"})
        level = "check"
    elif months >= 24:
        checks.append({"ok": True, "text": f"{months}-month project: covers the 2 years the route needs"})
        checks.append({"ok": True, "text": "Contract has more than a year to run and the grant is well over £30,000"})
        level = "likely"
    else:
        checks.append({"ok": False, "text": f"Only {months} months: the grant must cover at least 2 years, so this one won't qualify"})
        level = "no"
    checks.append({"ok": None if uni else False, "text": (f"Employed by {employer}: check it's on UKRI's approved organisations list"
                                                          if uni else "Employer doesn't look like a university or research body: check it's UKRI-approved")})
    checks.append({"ok": None, "text": "Ask the university to confirm the role is in the KTP grant and that HR will write the endorsement letter"})
    checks.append({"ok": None, "text": "You need a UK bachelor's degree or equivalent research degree or experience"})
    return {"level": level, "months": months, "checks": checks}


def _ktp_pages(max_pages: int = 5) -> list[tuple[str, str]]:
    """All current KTP Associate vacancies: (job page url, card html). The whole list is small."""
    if MOCK:
        page = (WEBUI / "fixtures" / "jobs" / "ktp_list.html").read_text(encoding="utf-8")
        return re.findall(r'<a href="(https://iuk-ktp\.org\.uk/job/[^"]+)"[^>]*>(.*?)</a>\s*</div>', page, re.S)
    out, seen = [], set()
    for n in range(1, max_pages + 1):
        req = urllib.request.Request(f"{KTP}/jobs/?page_number={n}", headers={"User-Agent": UA, "Accept": "text/html"})
        page = _read(req, allow_partial=True).decode("utf-8", errors="replace")
        cards = [c for c in re.findall(r'<a href="(https://iuk-ktp\.org\.uk/job/[^"]+)"[^>]*>(.*?)</a>\s*</div>', page, re.S)
                 if c[0] not in seen]
        if not cards:
            break
        seen.update(c[0] for c in cards)
        out += cards
        if f"page_number={n + 1}" not in page:
            break
        time.sleep(0.4)
    return out


def ktp_search(title: str, loc: dict | None, s: dict) -> list[dict]:
    """Innovate UK Knowledge Transfer Partnership Associate roles, each with a Global Talent check."""
    out = []
    for url, card in _ktp_pages():
        t = re.search(r"<h2[^>]*>(.*?)</h2>", card, re.S)
        kb = re.search(r"</h2>\s*<div[^>]*>(.*?)</div>", card, re.S)
        spans = [_text(x) for x in re.findall(r'<span class="text-sm">(.*?)</span>', card, re.S)]
        summary = re.search(r'sm:not-sr-only[^"]*"[^>]*>(.*?)</div>', card, re.S)
        posted = None
        if len(spans) > 2:
            m = re.match(r"(\d+)\s+(day|week|month)s?\s+ago", spans[2])
            days = int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)] if m else (0 if "today" in spans[2].lower() or "hour" in spans[2].lower() else None)
            if days is not None:
                posted = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            if MOCK:
                slug = url.rstrip("/").rsplit("/", 1)[-1]
                fx = WEBUI / "fixtures" / "jobs" / f"ktp_job_{slug}.html"
                page = (fx if fx.exists() else WEBUI / "fixtures" / "jobs" / "ktp_job.html").read_text(encoding="utf-8")
            else:
                time.sleep(0.3)
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
                page = _read(req, allow_partial=True).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - the card alone is still useful
            page = ""
        dur_text = _ktp_field(page, "Job duration") or " ".join(
            re.findall(r"(?:project|post|role|position|contract)[^.]{0,40}?(?:duration|for|of|lasts?)[^.]{0,15}?\d+\s*(?:months|years?)", _text(page), re.I)[:1])
        dur = re.search(r"(\d+)\s*months?", dur_text, re.I)
        years = re.search(r"(\d(?:\.5)?)\s*years?", dur_text, re.I)
        months = int(dur.group(1)) if dur else (round(float(years.group(1)) * 12) if years else None)
        closing = _uk_date(_ktp_field(page, "Application closing date"))
        if closing and closing < datetime.now().date().isoformat():
            continue  # closed
        apply = re.search(r'<a href="(https?://[^"]+)"[^>]*>\s*<span[^>]*>\s*Apply now', page)
        body = "\n\n".join(_text(x) for x in re.findall(r'<div class="wysiwyg">(.*?)</div>', page, re.S))
        employer = _text(kb.group(1)) if kb else ""
        partner = re.search(r"between (?:the )?(.+?)(?: \([A-Z]+\))? and (.+?)(?: Ltd| Limited| plc)?[,.]", body)
        apply_url = absolute_url(apply.group(1), url) if apply else url
        out.append(_job("ktp", url.rstrip("/").rsplit("/", 1)[-1], _text(t.group(1)) if t else "", employer,
                        spans[0] if spans else "", url, apply_url,
                        ("£" + spans[1].replace(" - ", " - £")) if len(spans) > 1 and spans[1] else "", posted,
                        body or (_text(summary.group(1)) if summary else ""), full=len(body) > 600,
                        extra={"closing": closing,
                               "start_date": _uk_date(_ktp_field(page, "Start date")),
                               "sector": _ktp_field(page, "Job sector"), "ktp_partner": partner.group(2) if partner else "",
                               "type_hints": ["Fixed term"] + ([f"{months} months"] if months else []),
                               "global_talent": global_talent_check(months, employer, "up to" in dur_text.lower())}))
    return out


WORKHUB = "https://www.jobs.service.gov.uk"


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def workhub_search(title: str, loc: dict | None, s: dict, pages: int = 2) -> list[dict]:
    out = []
    for page in range(1, pages + 1):
        q = {"keywords": title, "pageNumber": page}
        if loc:
            q["location"] = loc["name"]
        else:
            q["keywords"] = title + " remote"
        url = f"{WORKHUB}/jobs/search?" + urllib.parse.urlencode(q)
        if MOCK:
            page_html = (WEBUI / "fixtures" / "jobs" / "workhub.html").read_text(encoding="utf-8")
        else:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            page_html = _read(req, allow_partial=True).decode("utf-8", errors="replace")
        cards = page_html.split('data-testid="searchResultCard-')[1:]
        for c in cards:
            jid = c.split('"', 1)[0]
            t = re.search(r'data-testid="jobTitle-[^"]*"[^>]*>(.*?)</a>', c, re.S)
            emp = re.search(r'data-testid="searchResultCardEmployer"[^>]*>(.*?)</p>', c, re.S)
            spans = re.findall(r"<span[^>]*>(.*?)</span>", emp.group(1), re.S) if emp else []
            sal = re.search(r'govuk-!-font-weight-bold govuk-!-margin-bottom-2"[^>]*>(.*?)</p>', c, re.S)
            tags = [_text(x) for x in re.findall(r'class="govuk-tag[^"]*"[^>]*>(.*?)</span>', c, re.S)]
            desc = re.search(r'data-testid="searchResultCardJobDescription"[^>]*>(.*?)</p>', c, re.S)
            added = re.search(r"Added on ([^<]+)</p>", c)
            posted = None
            if added:
                try:
                    posted = datetime.strptime(added.group(1).strip(), "%d %b %Y").replace(tzinfo=timezone.utc)
                except ValueError:
                    posted = None
            link = f"{WORKHUB}/jobs/{jid}"
            out.append(_job("workhub", jid, _text(t.group(1)) if t else "", _text(spans[0]) if spans else "",
                            _text(spans[1]).lstrip("- ").strip() if len(spans) > 1 else "", link, link,
                            _text(sal.group(1)) if sal else "", posted.isoformat() if posted else None,
                            _text(desc.group(1)) if desc else "",
                            remote=any(x.lower() in ("remote", "fully remote") for x in tags),
                            extra={"type_hints": tags}))
        if MOCK or len(cards) < 10:
            break
        time.sleep(0.5)
    return out


def absolute_url(url: str, base: str = "") -> str:
    """Links scraped from a page can be relative ("/jobs/123/apply"). Join them to the page they
    came from (or the Work Hub site) so the Apply button always gets a full web address."""
    url = (url or "").strip()
    if not url or re.match(r"https?://", url, re.I):
        return url
    if not re.match(r"https?://", base or "", re.I):
        base = WORKHUB + "/"
    return urllib.parse.urljoin(base, url)


def workhub_detail(job: dict) -> dict:
    req = urllib.request.Request(job["url"], headers={"User-Agent": UA, "Accept": "text/html"})
    page_html = _read(req, allow_partial=True).decode("utf-8", errors="replace")
    main = re.search(r"<main[^>]*>(.*?)(?:</main>|$)", page_html, re.S)  # a cut-short page may lack </main>
    text = strip_html(main.group(1) if main else page_html)
    apply = re.search(r'href="([^"]+)"[^>]*>\s*Apply for this job', page_html)
    link = absolute_url(html.unescape(apply.group(1)), job["url"]) if apply else job["url"]
    return {"description": text, "apply_url": link}


def add_manual(title: str, company: str, location: str, url: str, text: str, bank_text: str = "") -> dict:
    """A job you found elsewhere (Indeed, Civil Service Jobs, Trac, a recruiter email)."""
    if not title.strip() or len(text.strip()) < 150:
        raise ValueError("Add a job title and paste the full advert text.")
    url = url.strip()
    host = _web_host(url)
    if url and not host:
        raise ValueError("Enter an advert link starting with https:// or http://.")
    board = next((name for name, domains in MANUAL_BOARDS.items()
                  if any(host == domain or host.endswith("." + domain) for domain in domains)), "manual")
    st = load_store()
    job = _job("manual", url or f"{company}-{title}", title, company, location, url, url, "", None,
               text.strip(), full=True)
    key = make_key(job["company"], job["title"], url)
    job.update(key=key, sources=[board], status="saved",
               first_seen=datetime.now().isoformat(timespec="seconds"),
               last_seen=datetime.now().isoformat(timespec="seconds"))
    job["match"] = quick_match(job, bank_terms(bank_text), get_settings()["titles"])
    st["jobs"][key] = job
    save_store(st)
    return job


ARBEITNOW = "https://www.arbeitnow.co.uk/api/job-board-api"


def arbeitnow_all(s: dict, pages: int = 5) -> list[dict]:
    """The feed is UK-wide and not keyword-searchable, so pull a few pages and filter by title."""
    out = []
    for page in range(1, pages + 1):
        d = _mock("arbeitnow") if MOCK else _get(f"{ARBEITNOW}?page={page}")
        for r in d.get("data", []):
            out.append(_job("arbeitnow", r.get("slug") or r.get("url"), r.get("title"), r.get("company_name"),
                            r.get("location"), r.get("url"), r.get("url"), "", r.get("created_at"),
                            strip_html(r.get("description", "")), full=True, remote=bool(r.get("remote")),
                            extra={"visa_sponsorship": bool(r.get("visa_sponsorship")),
                                   "type_hints": list(r.get("job_types") or [])}))
        if MOCK or not (d.get("links") or {}).get("next"):
            break
        time.sleep(0.5)
    return out


def himalayas_search(title: str, _location: dict | None, _settings: dict) -> list[dict]:
    """Get the first page of UK-eligible remote results for a title."""
    params = urllib.parse.urlencode({"q": title, "country": "GB", "sort": "recent"})
    data = _mock("himalayas") if MOCK else _get(f"https://himalayas.app/jobs/api/search?{params}")
    jobs = []
    for row in data["jobs"]:
        url = row.get("applicationLink") or ""
        if not _feed_allowed(url, {"himalayas.app", "www.himalayas.app"}):
            continue
        expires = _parse_dt(row.get("expiryDate"))
        if expires and expires <= datetime.now(timezone.utc):
            continue
        description = strip_html(row.get("description") or row.get("excerpt") or "")
        salary = ""
        if row.get("currency") == "GBP" and row.get("salaryPeriod") in (None, "annual"):
            salary = _money(row.get("minSalary"), row.get("maxSalary"))
        jobs.append(_job("himalayas", row.get("guid") or url, row.get("title"), row.get("companyName"),
                         "Remote (UK eligible)", url, url, salary, row.get("pubDate"), description, full=True,
                         remote=True, extra={"source_links": {"himalayas": url},
                                             "type_hints": [row["employmentType"]] if row.get("employmentType") else []}))
    return jobs


# ----------------------------------------------------------------------------- Yugenox Jobs (MCP)
# A free, read-only job search exposed as an MCP server (https://jobs.yugenox.com/connect): employers' own
# postings, agency reposts excluded, mostly US and Canada. Up to 60 calls a minute per address; no key.
# The service doesn't publish its parameter names, so they are read from tools/list and mapped by meaning.

YUGENOX_MCP = "https://jobs.yugenox.com/mcp"


class MCPError(RuntimeError):
    pass


class MCPClient:
    """Minimal MCP client over Streamable HTTP (JSON or server-sent events replies)."""

    def __init__(self, url: str, timeout: int = 40):
        self.url, self.timeout, self.session, self.n, self.tools = url, timeout, None, 0, None
        self.last_call = 0.0

    def _post(self, method: str, params: dict | None = None, notify: bool = False):
        body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            self.n += 1
            body["id"] = self.n
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "User-Agent": UA, "MCP-Protocol-Version": "2025-06-18"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        wait = 1.1 - (time.time() - self.last_call)  # stay well under 60 calls a minute
        if wait > 0:
            time.sleep(wait)
        self.last_call = time.time()
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self.session = r.headers.get("Mcp-Session-Id") or self.session
                kind = r.headers.get("Content-Type", "")
                raw = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise MCPError("Yugenox is busy (rate limit); try again in a minute") from e
            raise MCPError(f"Yugenox replied HTTP {e.code}") from e
        if notify:
            return None
        msgs = []
        if "event-stream" in kind:
            for block in raw.split("\n\n"):
                data = "\n".join(l[5:].lstrip() for l in block.splitlines() if l.startswith("data:"))
                if data.strip():
                    msgs.append(json.loads(data))
        elif raw.strip():
            msgs.append(json.loads(raw))
        reply = next((m for m in msgs if m.get("id") == body["id"]), None)
        if reply is None:
            raise MCPError("Yugenox sent no reply")
        if reply.get("error"):
            raise MCPError(f"Yugenox: {reply['error'].get('message', reply['error'])}")
        return reply.get("result") or {}

    def connect(self) -> dict:
        if self.tools is None:
            self._post("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "cv-tailor", "version": "1.0"}})
            self._post("notifications/initialized", notify=True)
            self.tools = {t["name"]: t.get("inputSchema") or {} for t in self._post("tools/list").get("tools", [])}
        return self.tools

    def call(self, name: str, args: dict):
        res = self._post("tools/call", {"name": name, "arguments": args})
        if res.get("isError"):
            text = " ".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            raise MCPError(f"Yugenox {name}: {text[:200] or 'error'}")
        if res.get("structuredContent") is not None:
            return res["structuredContent"]
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        for t in texts:
            try:
                return json.loads(t)
            except ValueError:
                continue
        return {"_text": "\n".join(texts)}


def _pick(props: dict, *names: str) -> str | None:
    """The first schema property whose name matches one of these (exactly, then by containing it)."""
    low = {k.lower(): k for k in props}
    for n in names:
        if n in low:
            return low[n]
    for n in names:
        for k_low, k in low.items():
            if n in k_low:
                return k
    return None


def _first(row: dict, *keys, default=""):
    for k in keys:
        v = row.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("display") or v.get("text") or ""
        if isinstance(v, list):
            v = ", ".join(str(x.get("name") if isinstance(x, dict) else x) for x in v if x)
        if v not in (None, "", []):
            return v
    return default


_UK_PLACE = re.compile(r"\b(united kingdom|uk|england|scotland|wales|northern ireland|great britain|gb)\b", re.I)
_YUGENOX_CLIENT: MCPClient | None = None


def _yugenox() -> MCPClient:
    global _YUGENOX_CLIENT
    if _YUGENOX_CLIENT is None:
        _YUGENOX_CLIENT = MCPClient(YUGENOX_MCP)
    return _YUGENOX_CLIENT


def yugenox_rows(data) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("jobs", "results", "postings", "items", "data", "matches"):
            if isinstance(data.get(k), list):
                return [r for r in data[k] if isinstance(r, dict)]
            if isinstance(data.get(k), dict):
                inner = yugenox_rows(data[k])
                if inner:
                    return inner
    return []


def yugenox_search(title: str, loc: dict | None, s: dict) -> list[dict]:
    """Search Yugenox Jobs for a title in a place (or remote). Keeps UK and remote postings only."""
    if MOCK:
        data = _mock("yugenox")
    else:
        client = _yugenox()
        tools = client.connect()
        if "search_jobs" not in tools:
            raise MCPError("Yugenox no longer offers search_jobs")
        props = (tools["search_jobs"].get("properties") or {})
        args = {}
        q = _pick(props, "query", "q", "keywords", "keyword", "title", "search", "text")
        if q:
            args[q] = title
        where = _pick(props, "location", "place", "where", "city")
        remote = _pick(props, "remote", "remote_only", "is_remote")
        if loc:
            if where:
                args[where] = f"{loc['name']}, United Kingdom"
        elif remote and (props[remote].get("type") in ("boolean", None)):
            args[remote] = True
        elif where:
            args[where] = "Remote"
        days = _pick(props, "posted_within_days", "max_age_days", "days", "since_days", "posted_within", "recency")
        if days and props[days].get("type") in ("integer", "number") and s.get("max_days"):
            args[days] = int(s["max_days"])
        data = client.call("search_jobs", args)
    jobs = []
    for row in yugenox_rows(data):
        place = str(_first(row, "location", "locations", "place", "city", "country"))
        is_remote = bool(row.get("remote") or row.get("is_remote")) or "remote" in place.lower()
        if loc and not (_UK_PLACE.search(place) or loc["name"].lower() in place.lower()):
            continue  # mostly North American postings: keep only ones in the UK
        if not loc and not is_remote:
            continue
        sid = _first(row, "id", "job_id", "slug", "url", "link")
        url = _first(row, "url", "link", "yugenox_url", "job_url", "posting_url")
        apply_url = _first(row, "apply_url", "apply_link", "applyUrl", "application_url", default=url)
        pay = _first(row, "salary", "pay", "compensation", "salary_text")
        jobs.append(_job("yugenox", sid, _first(row, "title", "job_title", "name"),
                         _first(row, "company", "employer", "company_name", "organization"),
                         place or ("Remote" if is_remote else ""), url, apply_url, str(pay or ""),
                         _first(row, "posted", "posted_at", "posted_date", "date_posted", "published_at", default=None),
                         strip_html(str(_first(row, "description", "summary", "snippet"))), remote=is_remote,
                         extra={"yugenox_id": sid}))
    return jobs


def yugenox_detail(job: dict) -> dict:
    client = _yugenox()
    tools = client.connect()
    props = (tools.get("get_job") or {}).get("properties") or {}
    key = _pick(props, "id", "job_id", "slug", "url") or "id"
    data = client.call("get_job", {key: job.get("yugenox_id") or job.get("source_id")})
    row = data if isinstance(data, dict) and not yugenox_rows(data) else (yugenox_rows(data) or [{}])[0]
    text = strip_html(str(_first(row, "description", "full_description", "body", "_text")))
    return {"description": text, "apply_url": _first(row, "apply_url", "apply_link", "applyUrl", default=job["apply_url"])}


class _FeedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: set[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _feed_allowed(newurl, self.hosts):
            raise ValueError("The feed redirected outside its provider.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _rss_items(source: str, url: str, hosts: set[str]) -> list[ET.Element]:
    if not _feed_allowed(url, hosts):
        raise ValueError("Enter an HTTPS feed URL from the source.")
    if MOCK:
        payload = (WEBUI / "fixtures" / "jobs" / f"{source}.xml").read_bytes()
    else:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml"})
        opener = urllib.request.build_opener(_FeedRedirect(hosts))
        with opener.open(req, timeout=25) as response:
            payload = response.read(MAX_FEED_BYTES + 1)
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("The feed exceeds the download size limit.")
    try:
        channel = ET.fromstring(payload).find("channel")
    except ET.ParseError as e:
        raise ValueError("This URL did not return an RSS feed. Check the saved alert link.") from e
    if channel is None:
        raise ValueError("This URL did not return an RSS feed. Check the saved alert link.")
    return channel.findall("item")


def _rss_date(value: str) -> datetime | None:
    try:
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return _parse_dt(value)


def rss_jobs(source: str, url: str, hosts: set[str]) -> list[dict]:
    jobs = []
    is_remote_feed = source == "wwr"
    for item in _rss_items(source, url, hosts):
        raw_link = item.findtext("link", "").strip()
        link = urllib.parse.urljoin(url, raw_link)
        if link.startswith("http://"):
            link = "https://" + link.removeprefix("http://")
        if not raw_link or not _feed_allowed(link, hosts):
            continue
        title = item.findtext("title", "").strip()
        company = item.findtext("company", "").strip()
        region = item.findtext("region", "").strip()
        location = item.findtext("location", "").strip() or region
        if is_remote_feed:
            country = item.findtext("country", "").strip().upper()
            expires = _parse_dt(item.findtext("expires_at", ""))
            if expires and expires <= datetime.now(timezone.utc):
                continue
            eligible = region.casefold() in ("anywhere in the world", "europe only", "uk only", "united kingdom only")
            if not eligible and (region or country not in ("GB", "UK")):
                continue
            if not company and ": " in title:
                company, title = title.split(": ", 1)
        description = strip_html(item.findtext("description", ""))
        posted = _rss_date(item.findtext("pubDate", ""))
        jobs.append(_job(source, item.findtext("guid") or link, title, company, location, link, link, "",
                         posted.date().isoformat() if posted else None, description, full=bool(description),
                         remote=is_remote_feed, extra={"source_links": {source: link}}))
    return jobs


SENIORITY = {"senior", "lead", "principal", "head", "junior", "staff", "chief", "of", "and", "the", "mid", "level"}


def title_matcher(titles: list[str]):
    """A job matches a search title when it contains every core word of it, seniority aside:
    'Data Engineer' matches 'Senior Data Engineer, Platform' but not 'Software Engineer'."""
    cores = []
    for t in titles:
        words = [w for w in re.findall(r"[a-z0-9+#]+", t.lower()) if w not in SENIORITY]
        if words:
            cores.append(words)

    def match(title: str) -> bool:
        tl = (title or "").lower()
        return any(all(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", tl) for w in ws)
                   for ws in cores)
    return match


# Public-sector titles rarely match private-sector ones word for word ("Intelligence Analyst",
# "Information Analyst", "Principal Analyst"), so NHS and Work Hub get a looser, field-based filter.
GENERIC = {"engineer", "developer", "lead", "manager", "specialist", "officer", "consultant", "architect"}
FIELD_WORDS = {"intelligence", "analyst", "analytics", "informatics", "information", "reporting", "insight",
               "insights", "warehouse", "bi", "data", "power"}
NOT_FIELD = {"administrator", "admin", "co-ordinator", "coordinator", "clerk", "assistant", "receptionist",
             "apprentice", "nurse", "clinical", "coder", "secretary", "support worker", "governance", "security",
             "service desk", "infrastructure", "finance", "maintenance"}


def loose_matcher(titles: list[str]):
    strict = title_matcher(titles)
    words = {w for t in titles for w in re.findall(r"[a-z0-9+#]+", t.lower())
             if w not in SENIORITY and w not in GENERIC} | FIELD_WORDS

    def match(title: str) -> bool:
        tl = (title or "").lower()
        if strict(tl):
            return True
        if any(n in tl for n in NOT_FIELD):
            return False
        return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", tl) for w in words)
    return match


def bun_path() -> str | None:
    """bun is often installed per user and missing from the PATH a .bat file sees."""
    found = shutil.which("bun")
    if found:
        return found
    for folder in (Path(sys.executable).parent, WEBUI / ".venv" / "Scripts", Path.home() / ".bun" / "bin"):
        for name in ("bun.exe", "bun") if os.name == "nt" else ("bun",):
            cand = folder / name
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
    return None


def _linkedin_cli_path() -> Path:
    return REPO / ".agents" / "skills" / "linkedin-search" / "cli" / "src" / "cli.ts"


def _bun_cli(args: list[str]) -> str:
    cli = _linkedin_cli_path()
    bun = bun_path()
    if not bun:
        raise RuntimeError("Install Bun to use the optional LinkedIn scanner.")
    if not cli.is_file():
        raise RuntimeError("The repo's LinkedIn search script is missing.")
    p = subprocess.run([bun, "run", str(cli), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=REPO, timeout=90)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-300:] or p.stdout[-300:])
    return p.stdout


def linkedin_search(title: str, loc: dict | None, s: dict) -> list[dict]:
    args = ["search", "-q", title, "-l", loc["name"] + ", United Kingdom" if loc else "United Kingdom",
            "--limit", str(min(int(s.get("linkedin_cap") or 10), 25)), "--format", "json"]
    days = int(s.get("max_days") or 0)
    if days:
        args += ["--jobage", str(min((d for d in (1, 7, 14, 30) if d >= days), default=30))]
    if not loc:
        args += ["--remote", "remote"]
    raw = json.dumps(_mock("linkedin")) if MOCK else _bun_cli(args)
    data = json.loads(raw)
    rows = data.get("results", data) if isinstance(data, dict) else data
    out = []
    for r in rows:
        out.append(_job("linkedin", r.get("id") or r.get("url"), r.get("title"), r.get("company"),
                        r.get("location"), r.get("url"), r.get("url"), r.get("salary") or "",
                        r.get("postedAt") or r.get("listedAt") or r.get("date"), "", remote=not loc))
    return out


def linkedin_detail(job: dict) -> dict:
    raw = json.dumps(_mock("linkedin_detail")) if MOCK else _bun_cli(
        ["detail", job["url"], "--format", "json"])
    d = json.loads(raw)
    return {"description": d.get("description") or d.get("text") or "", "apply_url": job["url"]}


def page_text(url: str) -> str:
    """Full ad text via the repo's fetch_page.py (enforces robots.txt, GET only)."""
    if MOCK:
        return _mock("page")["text"]
    tool = REPO / "tools" / "fetch_page.py"
    p = subprocess.run([sys.executable, str(tool), url, "--text", "--max-chars", "20000"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    out = p.stdout.split("\n", 1)
    if p.returncode != 0 or len(out) < 2 or not out[0].startswith("HTTP 2"):
        raise RuntimeError((p.stdout + p.stderr).strip()[:300] or "fetch failed")
    return out[1].strip()


def _mock(name: str):
    return json.loads((WEBUI / "fixtures" / "jobs" / f"{name}.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- scoring

COMMON_TERMS = [
    "python", "sql", "t-sql", "azure", "aws", "gcp", "power bi", "tableau", "looker", "qlik", "snowflake",
    "databricks", "dbt", "airflow", "spark", "pyspark", "kafka", "terraform", "docker", "kubernetes",
    "microsoft fabric", "fabric", "synapse", "azure data factory", "data factory", "excel", "dax",
    "power query", "etl", "elt", "data warehouse", "data warehousing", "data modelling", "data modeling",
    "data lake", "lakehouse", "git", "ci/cd", "devops", "java", "scala", "r", "sas", "ssis", "ssrs",
    "sql server", "postgresql", "mysql", "bigquery", "redshift", "api", "rest", "react", "javascript",
    "typescript", "c#", ".net", "machine learning", "ai", "llm", "data governance", "gdpr",
    "data quality", "stakeholder", "agile", "scrum", "prince2", "itil", "cyber essentials",
    "iso 27001", "hubspot", "salesforce", "dynamics", "sharepoint", "power automate", "power apps",
    "data visualisation", "reporting", "analytics", "kimball", "star schema", "medallion",
    # software engineering: so adverts asking for these count them as missing, not invisible
    "django", "flask", "fastapi", "golang", "vue", "vue.js", "angular", "node.js", "nodejs", "next.js",
    "frontend", "front-end", "backend", "back-end", "full stack", "full-stack", "microservices",
    "graphql", "grpc", "kotlin", "swift", "rust", "c++", "ruby", "ruby on rails", "php", "laravel",
    "spring boot", "redis", "mongodb", "elasticsearch", "rabbitmq", "kinesis", "lambda", "serverless",
    "html", "css", "tailwind", "jest", "pytest", "unit testing", "tdd", "observability", "on-call",
    "distributed systems", "system design", "event-driven", "oauth", "sso", "saml",
    "pandas", "numpy", "scikit-learn", "pytorch", "tensorflow", "mlops", "nlp", "rag", "generative ai",
    "statistics", "a/b testing", "hadoop", "hive", "informatica", "alteryx", "sap", "oracle",
    "payments", "fintech", "trading", "ledger", "kyc", "aml", "nhs", "epr", "hl7", "fhir",
]
_STOPWORDS = {"and", "or", "if", "the", "a", "an", "of", "to", "in", "on", "for", "with", "by", "as", "at",
              "from", "into", "via", "etc", "is", "was", "are", "be", "it", "its", "this", "that", "i", "we"}


def bank_terms(bank_text: str) -> set[str]:
    low = bank_text.lower()
    terms = {t for t in COMMON_TERMS if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", low)}
    for line in bank_text.splitlines():  # comma lists from a Skills section, not ordinary sentences
        parts = [p.strip().lower().strip(".") for p in line.split(":", 1)[-1].split(",")]
        if len(parts) < 4 or sum(len(p.split()) <= 3 for p in parts) < 0.8 * len(parts):
            continue
        for p in parts:
            if 1 < len(p) <= 30 and len(p.split()) <= 3 and p not in _STOPWORDS:
                terms.add(p)
    return terms


def quick_match(job: dict, mine: set[str], titles: list[str]) -> dict:
    text = f"{job['title']} {job['description']}".lower()
    wanted = {t for t in set(COMMON_TERMS) | mine
              if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text)}
    have = sorted(wanted & mine)
    missing = sorted(wanted - mine)
    title_hit = any(t.lower() in job["title"].lower() for t in titles)
    # Skills only: a matching title no longer lifts this, so 100% means every skill term seen was yours.
    score = round(100 * len(have) / len(wanted)) if wanted else (50 if title_hit else 0)
    return {"score": score, "have": have[:15], "missing": missing[:15], "title_hit": title_hit}


# ----------------------------------------------------------------------------- store & scan

def load_store() -> dict:
    st = load_json(STORE, None) or {"jobs": {}, "last_scan": None}
    for j in (st.get("jobs") or {}).values():  # older scans saved some Work Hub links without the site
        if j.get("apply_url"):
            j["apply_url"] = absolute_url(j["apply_url"], j.get("url", ""))
    return st


def save_store(st: dict) -> None:
    save_json(STORE, st)


def scan(progress, bank_text: str) -> dict:
    s = get_settings()
    st = load_store()
    mine = bank_terms(bank_text)
    title_ok = title_matcher(s["titles"])
    loose_ok = loose_matcher(s["titles"])
    found: list[dict] = []
    errors: list[str] = []
    places: list[dict | None] = list(s["locations"]) + ([None] if s["remote"] else [])
    tasks = []
    if s["sources"].get("reed"):
        tasks += [("Reed", reed_search, t, l) for t in s["titles"] for l in places]
    if s["sources"].get("adzuna"):
        tasks += [("Adzuna", adzuna_search, t, l) for t in s["titles"] for l in places]
    if s["sources"].get("nhs"):
        tasks += [("NHS Jobs", nhs_search, t, l) for t in s["titles"] for l in s["locations"]]
    if s["sources"].get("workhub"):
        tasks += [("Work Hub", workhub_search, t, l) for t in s["titles"] for l in places]
    if s["sources"].get("linkedin"):
        # Low volume on purpose: one query per title for the first location plus remote.
        tasks += [("LinkedIn", linkedin_search, t, l) for t in s["titles"]
                  for l in (places[:1] + ([None] if s["remote"] else []))]
    if s["sources"].get("himalayas") and s["remote"]:
        tasks += [("Himalayas", himalayas_search, title, None) for title in s["titles"]]
    if s["sources"].get("yugenox"):
        tasks += [("Yugenox", yugenox_search, t, l) for t in s["titles"] for l in places]
    if s["sources"].get("ktp"):
        tasks.append(("KTP", ktp_search, "every KTP Associate role", None))  # the whole national list, once
    boards = s["ats_boards"] if s["sources"].get("ats") else []
    skipped = []
    if s["sources"].get("reed") and not os.environ.get("REED_API_KEY") and not MOCK:
        skipped.append("Reed (add a free key)")
        tasks = [t for t in tasks if t[0] != "Reed"]
    if s["sources"].get("adzuna") and not (os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY")) and not MOCK:
        skipped.append("Adzuna (add free keys)")
        tasks = [t for t in tasks if t[0] != "Adzuna"]
    if s["sources"].get("ats") and not boards:
        skipped.append("Company boards (none listed)")
    if s["sources"].get("linkedin") and not MOCK:
        missing = [label for label, ready in (("Bun", bun_path()),
                    ("the repo’s LinkedIn search script", _linkedin_cli_path().is_file())) if not ready]
        if missing:
            skipped.append(f"LinkedIn (missing {' and '.join(missing)})")
            tasks = [task for task in tasks if task[0] != "LinkedIn"]
    if not s["remote"]:
        for source, label in (("himalayas", "Himalayas"), ("wwr", "We Work Remotely")):
            if s["sources"].get(source):
                skipped.append(f"{label} (enable UK remote roles)")
    feeds = []
    if s["sources"].get("wwr") and s["remote"]:
        feeds.append(("wwr", "We Work Remotely", "https://weworkremotely.com/remote-jobs.rss", WWR_HOSTS))
    if s["sources"].get("jobserve"):
        if s["jobserve_rss"]:
            feeds.append(("jobserve", "JobServe", s["jobserve_rss"], JOBSERVE_HOSTS))
        else:
            skipped.append("JobServe (add an RSS alert URL)")
    if s["sources"].get("arbeitnow"):
        progress("Arbeitnow UK (free feed)", 3)
        try:
            found += [r for r in arbeitnow_all(s) if title_ok(r["title"])]
        except Exception as e:
            errors.append(f"Arbeitnow: {e}")
    for source, label, url, hosts in feeds:
        progress(f"{label} RSS", 4)
        try:
            found += [r for r in rss_jobs(source, url, hosts) if not s["titles"] or title_ok(r["title"])]
        except urllib.error.HTTPError as e:
            errors.append(f"{label}: feed returned HTTP {e.code}.")
        except Exception as e:
            errors.append(f"{label}: {e}")
    total = len(tasks) + len(boards) or 1
    failed_sources: set[str] = set()
    nhs_successes = 0
    nhs_missing = 0
    nhs_bad_places: dict[str, int] = {}
    for i, (name, fn, title, loc) in enumerate(tasks):
        if name in failed_sources:
            continue
        progress(f"{name}: {title} · {loc['name'] if loc else 'remote'}", 5 + 80 * i // total)
        try:
            rows = fn(title, loc, s)
            # Keyword searches on NHS Jobs and Work Hub are loose; keep only real title matches.
            if name in ("NHS Jobs", "Work Hub"):
                rows = [r for r in rows if loose_ok(r["title"])]
                if name == "NHS Jobs":
                    nhs_successes += 1
            elif name == "Himalayas":
                rows = [r for r in rows if title_ok(r["title"])]
            elif name == "Yugenox":
                rows = [r for r in rows if loose_ok(r["title"])]
            elif name == "KTP":
                # KTP titles are project-specific ("AI Innovation Lead"), so keep a role if the title
                # matches or the advert asks for at least three skills from your facts.
                rows = [r for r in rows if loose_ok(r["title"]) or
                        sum(1 for term in mine if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])",
                                                             r["description"].lower())) >= 3]
            found += rows
        except Exception as e:  # report once per source and move on
            if name == "NHS Jobs" and isinstance(e, urllib.error.HTTPError) and e.code == 404:
                nhs_missing += 1
                nhs_bad_places.setdefault(loc["name"] if loc else "remote", 0)
                nhs_bad_places[loc["name"] if loc else "remote"] += 1
                if not nhs_successes and nhs_missing >= 3:
                    failed_sources.add(name)
            else:
                failed_sources.add(name)
                errors.append(f"{name}: {e}")
        if not MOCK:
            time.sleep(0.25)
    if nhs_missing:
        titles_n = len(s.get("titles") or []) or 1
        bad = [p for p, n in nhs_bad_places.items() if n >= titles_n]  # every title failed for this place
        if nhs_successes and bad:
            skipped.append("NHS Jobs doesn't recognise the location " + ", ".join(f"'{p}'" for p in bad)
                           + ": check it under Locations (a town or postcode); other NHS searches completed")
        elif nhs_successes:
            skipped.append(f"NHS Jobs ({nhs_missing} searches returned 404; other NHS searches completed)")
        else:
            skipped.append("NHS Jobs (feed returned 404; open NHS Jobs under Browse other boards)")
    for j, board in enumerate(boards):
        progress(f"Company board {board}", 5 + 80 * (len(tasks) + j) // total)
        try:
            found += [r for r in ats_board(board) if title_ok(r["title"])]
        except Exception as e:
            errors.append(f"{board}: {e}")
    if MOCK and len(found) > 0:
        # mocks return the same payload for every query; de-dup handles it like a real overlap
        pass

    progress("De-duplicating and scoring", 90)
    excl = [x.lower() for x in s["exclude"]]
    new = 0
    seen_now = set()
    for job in found:
        if not job["title"] or any(x in job["title"].lower() for x in excl):
            continue
        # KTP roles stay open for weeks and are skipped once their closing date passes instead.
        if s.get("max_days") and job["source"] != "ktp" and job["age_days"] is not None and job["age_days"] > s["max_days"] * 2:
            continue
        key = make_key(job["company"], job["title"], job["url"])
        cur = st["jobs"].get(key)
        if cur:
            srcs = set(cur.get("sources", [cur["source"]])) | {job["source"]}
            cur["sources"] = sorted(srcs)
            if job["full_description"] and not cur.get("full_description"):
                cur.update(description=job["description"], full_description=True)
            if job.get("source_links"):
                cur.setdefault("source_links", {}).update(job["source_links"])
            himalayas_link = (cur.get("source_links") or {}).get("himalayas")
            if himalayas_link:
                cur["apply_url"] = himalayas_link
            elif job["source"] in ("greenhouse", "lever", "ashby", "workable"):
                cur["apply_url"] = job["apply_url"]  # employer's own page beats an aggregator
            cur["last_seen"] = datetime.now().isoformat(timespec="seconds")
        else:
            job["key"] = key
            job["sources"] = [job["source"]]
            job["status"] = "new"
            job["first_seen"] = job["last_seen"] = datetime.now().isoformat(timespec="seconds")
            st["jobs"][key] = job
            new += 1
        st["jobs"][key]["match"] = quick_match(st["jobs"][key], mine, s["titles"])
        seen_now.add(key)
    st["last_scan"] = {"at": datetime.now().isoformat(timespec="seconds"), "found": len(seen_now),
                       "new": new, "errors": errors, "skipped": skipped}
    save_store(st)
    return st["last_scan"]


# ----------------------------------------------------------------------------- work mode & job type
# Read from what the source states (structured fields first, then the advert's own wording).
# Nothing is guessed: if an advert doesn't say, it's "Not stated".

_HYBRID = re.compile(r"\bhybrid\b|#li-hybrid|split(?:ting)? (?:your |their |the )?time between (?:home|the office)"
                     r"|\b\d(?:\s*(?:-|to)\s*\d)? days? (?:a|per) week (?:in|at) (?:the|our|an) office", re.I)
_REMOTE = re.compile(r"\bfully[- ]remote\b|\b100% remote\b|\bremote[- ]first\b"
                     r"|\bwork(?:ing)? from home\b(?! (?:one|two|\d|some|occasionally|on occasion|part of))"
                     r"|\bhome[- ]based\b|\bremote (?:role|position|job|working|opportunity|contract)\b|#li-remote", re.I)
_ONSITE = re.compile(r"\bon[- ]?site\b|\boffice[- ]based\b|\bfully office\b|\bin[- ]office\b|#li-onsite", re.I)
_TYPES = [  # (label, pattern for structured hints / title / location, pattern for the description)
    ("Full-time", r"\bfull[- _]?time\b", r"\bfull[- ]time\b"),
    ("Part-time", r"\bpart[- _]?time\b",  # not "happy to consider part-time" style lines
     r"\bpart[- ]time (?:role|position|post|basis|contract|job|vacancy)\b|\b(?:hours|job type|working pattern)\W{0,4}part[- ]time\b"
     r"|\|\s*part[- ]time\b"),
    ("Permanent", r"\bperm(?:anent)?\b",
     r"\bpermanent (?:role|position|post|contract|basis|opportunity|job|full[- ]time)\b"
     r"|\b(?:job|contract|employment) type\W{0,4}permanent\b|\bfull[- ]time\s*/\s*permanent\b|\|\s*permanent\b"
     r"|\bworking pattern\W{0,4}permanent\b|\bpermanent\s*[,/|&]\s*(?:and\s+)?full[- ]time\b"
     r"|\bfull[- ]time\s*[,/|&]\s*(?:and\s+)?permanent\b|\bthis is a permanent\b"),
    ("Contract", r"\bcontract(?:or)?\b|\bfreelance\b",
     r"\bcontract (?:role|position|basis|opportunity|assignment)\b|\b(?:inside|outside) ir35\b|\bir35\b"
     r"|\bper day\b|\bday rate\b|£\s?\d[\d,.]*\s*(?:p/?d|/day)\b|\b(?:job|contract) type\W{0,4}contract\b"
     r"|\b\d+[- ]months? (?:contract|initial)\b"),
    ("Temporary", r"\btemp(?:orary)?\b|\bfixed[- _]?term\b|\binterim\b|\bsecondment\b|\blocum\b",
     r"\bfixed[- ]term\b|\btemporary (?:basis|contract|role|position|assignment)\b|\bon a temporary basis\b"
     r"|\bsecondment\b|\bmaternity cover\b"),
    ("Bank", r"^bank$", r"(?!x)x"),
    ("Apprenticeship", r"\bapprentice(?:ship)?\b", r"(?!x)x"),
    ("Internship", r"\bintern(?:ship)?\b", r"(?!x)x"),
]
_TYPES = [(l, re.compile(a, re.I), re.compile(b, re.I)) for l, a, b in _TYPES]


_HEDGE = re.compile(r"seeking|consider|open to|welcome|happy to|whether you|interested in|looking for a", re.I)


def _stated(rx: re.Pattern, text: str) -> bool:
    """A match that isn't hedged ("if you're seeking a part-time role, apply anyway")."""
    return any(not _HEDGE.search(text[max(0, m.start() - 45):m.start()]) for m in rx.finditer(text))


def work_info(job: dict) -> dict:
    """{"mode": "Remote" | "Hybrid" | "On-site" | "", "types": [...]} from what the advert states."""
    hints = [str(h) for h in job.get("type_hints") or []]
    head = " | ".join([job.get("title", ""), job.get("location", ""), *hints])
    desc = (job.get("description") or "")[:12000]
    both = head + "\n" + desc
    if _HYBRID.search(both):
        mode = "Hybrid"
    elif job.get("remote") or re.search(r"\bremote\b", head, re.I) or _REMOTE.search(desc):
        mode = "Remote"
    elif _ONSITE.search(both) or any(h.lower().replace("-", "").replace(" ", "") == "onsite" for h in hints):
        mode = "On-site"
    else:
        mode = ""
    types = []
    sal = job.get("salary", "")
    for label, head_re, desc_re in _TYPES:
        hit = (any(head_re.search(h) for h in hints) if label == "Bank" else head_re.search(head)) or _stated(desc_re, desc)
        if label == "Contract" and re.search(r"per day|/day|\bp/?d\b|day rate", sal, re.I):
            hit = True
        if hit:
            types.append(label)
    return {"mode": mode, "types": types}


def flags(job: dict) -> list[str]:
    out = []
    if job.get("age_days") is not None and job["age_days"] > 30:
        out.append(f"posted {job['age_days']} days ago")
    if job.get("salary_estimated"):
        out.append("salary is Adzuna's estimate")
    if not job.get("salary"):
        out.append("no salary stated")
    if len(job.get("sources", [])) > 1:
        out.append("on " + ", ".join(job["sources"]))
    return out


def full_description(job: dict) -> tuple[str, str]:
    """Return (text, apply_url) with the fullest description we can get honestly."""
    text, link = _full_description(job)
    return text, absolute_url(link, job.get("url", ""))


def _full_description(job: dict) -> tuple[str, str]:
    src = job["source"]
    himalayas_link = (job.get("source_links") or {}).get("himalayas")
    if himalayas_link or src in ("manual", "jobserve", "wwr"):
        description = job.get("description", "")
        if len(description.strip()) < 150:
            raise ValueError("This listing has a short preview. Paste the full advert in CV Tailor.")
        return description, himalayas_link or job["apply_url"]
    if job.get("full_description") and len(job.get("description", "")) > 400:
        return job["description"], job["apply_url"]
    if src == "reed" and (os.environ.get("REED_API_KEY") or MOCK):
        d = reed_detail(job)
        return d["description"], d["apply_url"]
    if src == "linkedin":
        d = linkedin_detail(job)
        return d["description"], d["apply_url"]
    if src == "workhub" and not MOCK:
        d = workhub_detail(job)
        return d["description"], d["apply_url"]
    if src == "yugenox" and not MOCK:
        d = yugenox_detail(job)
        if len(d["description"]) > 150:
            return d["description"], d["apply_url"]
    return page_text(job["url"]), job["apply_url"]


# ----------------------------------------------------------------------------- ghost-job risk
# A "ghost job" is an advert with no real live vacancy behind it: evergreen talent-pool ads, roles
# left up for months, the same role reposted again and again. No single signal proves it, so the
# app adds up visible signals and shows every reason. Nothing here is a verdict.

_EVERGREEN = re.compile(r"talent pool|talent community|pipeline of (?:talent|candidates)|future (?:opportunities|vacancies|roles)"
                        r"|always (?:looking|keen to hear)|register your interest|speculative application"
                        r"|general application|expressions? of interest", re.I)
_AGENCY = re.compile(r"recruit|resourcing|staffing|talent|search|people|personnel|consult", re.I)
_CLIENT = re.compile(r"\bour client\b|\bmy client\b|\ba leading\b.{0,40}\b(?:client|organisation|company)\b", re.I)


def norm_company(s: str) -> str:
    s = re.sub(r"\b(ltd|limited|plc|llp|uk|group|holdings|inc|the)\b", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def norm_title(s: str) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", (s or "").lower())
    s = re.sub(r"\b(remote|hybrid|uk|london|contract|permanent|ftc|maternity cover|\d+ months?)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def ghost_risk(job: dict, peers: dict) -> dict:
    """{"level": "low" | "medium" | "high", "points": n, "reasons": [...]}"""
    pts, why = 0, []
    age = job.get("age_days")
    if age is not None and age > 60:
        pts, why = pts + 3, why + [f"Posted {age} days ago"]
    elif age is not None and age > 30:
        pts, why = pts + 2, why + [f"Posted {age} days ago"]
    try:
        first = datetime.fromisoformat(job["first_seen"][:19]) if job.get("first_seen") else None
        last = datetime.fromisoformat(job["last_seen"][:19]) if job.get("last_seen") else None
    except ValueError:
        first = last = None
    if first and last and (last - first).days > 30:
        pts, why = pts + 2, why + [f"Still advertised {(last - first).days} days after your first scan found it"]
    same = peers.get((norm_company(job.get("company", "")), norm_title(job.get("title", ""))), [])
    reposts = {(j.get("posted") or j.get("first_seen", "")[:10]) for j in same} - {""}
    if len(reposts) >= 3:
        pts, why = pts + 2, why + [f"Same role posted {len(reposts)} times on different dates"]
    closing = job.get("closing") or ""
    if closing:
        try:
            if datetime.fromisoformat(closing[:10]).date() < datetime.now().date():
                pts, why = pts + 2, why + [f"Closing date {closing[:10]} has passed but the ad is still up"]
        except ValueError:
            pass
    text = job.get("description", "")
    if _EVERGREEN.search(text) or _EVERGREEN.search(job.get("title", "")):
        pts, why = pts + 3, why + ["Wording suggests a talent pool or future roles, not a live vacancy"]
    if not job.get("salary"):
        pts, why = pts + 1, why + ["No salary stated"]
    if job.get("full_description") and 0 < len(text) < 400:
        pts, why = pts + 1, why + ["Very little detail about the role"]
    if _AGENCY.search(job.get("company", "")) and _CLIENT.search(text):
        pts, why = pts + 1, why + ["Agency advert that doesn't name the employer"]
    level = "high" if pts >= 5 else "medium" if pts >= 3 else "low"
    # The same signals as a checklist: what looks healthy as well as what doesn't.
    checks = []
    if age is not None:
        checks.append({"ok": age <= 30, "text": f"Posted {age} day{'s' if age != 1 else ''} ago" +
                       (": still fresh" if age <= 7 else "" if age <= 30 else ": an old advert")})
    checks.append({"ok": len(reposts) < 3, "text": "Not reposted" if len(reposts) < 3 else f"Posted {len(reposts)} times on different dates"})
    if job.get("salary"):
        checks.append({"ok": True, "text": f"Salary stated: {job['salary']}"})
    else:
        checks.append({"ok": False, "text": "No salary stated"})
    if job.get("full_description") and len(text) >= 400:
        checks.append({"ok": True, "text": "Specific duties and requirements listed"})
    elif job.get("full_description"):
        checks.append({"ok": False, "text": "Light on specifics"})
    if _EVERGREEN.search(text) or _EVERGREEN.search(job.get("title", "")):
        checks.append({"ok": False, "text": "Reads like a talent pool, not a live vacancy"})
    if _AGENCY.search(job.get("company", "")) and _CLIENT.search(text):
        checks.append({"ok": False, "text": "Agency advert that doesn't name the employer"})
    elif job.get("company") and norm_company(job["company"]) not in _BOARD_NAMES and job["company"] != "Unknown employer":
        checks.append({"ok": True, "text": f"Employer named: {job['company']}"})
    if closing:
        past = any("Closing date" in w for w in why)
        checks.append({"ok": not past, "text": f"Closes {closing[:10]}" + (": date has passed" if past else "")})
    return {"level": level, "points": pts, "reasons": why, "checks": checks}


def peer_index(jobs: list[dict]) -> dict:
    idx: dict = {}
    for j in jobs:
        idx.setdefault((norm_company(j.get("company", "")), norm_title(j.get("title", ""))), []).append(j)
    return idx


# ----------------------------------------------------------------------------- fit ranking

def fit(job: dict, x: dict, settings: dict) -> dict:
    """Rank a job against your preferences. Starts from the quick match and adds or takes away
    points for each preference, with every reason shown. x holds work, sponsor, visa_pay, ghost, previous."""
    p = settings.get("prefs") or DEFAULT_SETTINGS["prefs"]
    m = job.get("match") or {}
    score = m.get("score", 0)
    terms = len(m.get("have") or []) + len(m.get("missing") or [])
    if job.get("cv_match") is not None:  # the full assessment from your tailored CV beats a keyword guess
        score = job["cv_match"]
        why = [f"CV match {score}% (full assessment of your tailored CV against the advert)"]
        terms = 99
    else:
        why = [f"Quick skills match {score}% ({terms} skill words found in the advert; the full assessment runs when you tailor)"]
    if m.get("title_hit"):
        why.append("title matches your search (not counted in the score)")
    if terms < 4:  # a title and a line of text can't show a real skills match
        score = min(score, 60)
        why.append(f"only {terms} skill term{'s' if terms != 1 else ''} in the ad text so far, so the skills match is rough (capped at 60)")

    pros, cons = [], []

    def adj(n, text):
        nonlocal score
        score += n
        why.append(f"{'+' if n > 0 else ''}{n} {text}")
        (pros if n > 0 else cons).append(text)
    lo = _salary_floor(job.get("salary", ""))
    want = int(settings.get("salary_min") or 0)
    if want:
        if lo is None:
            adj(-5, "no salary stated")
        elif lo >= want:
            adj(5, "pays your minimum or more")
        elif _salary_top(job.get("salary", "")) and _salary_top(job.get("salary", "")) >= want:
            adj(-5, "only the top of the range pays your minimum")
        else:
            adj(-25, f"pays below your £{want:,} minimum")
    mode = (x.get("work") or {}).get("mode")
    if mode and mode not in p["modes"]:
        adj(-30, f"{mode}, which you've ruled out")
    types = (x.get("work") or {}).get("types") or []
    known = [t for t in types if t in ("Permanent", "Contract", "Temporary", "Part-time")]
    if known and not any(t in p["types"] for t in known):
        adj(-30, f"{', '.join(known)}, which you've ruled out")
    if p.get("needs_sponsorship"):
        sp, vp = x.get("sponsor") or {}, x.get("visa_pay") or {}
        if sp.get("advert") == "no":
            adj(-60, "advert says no sponsorship")
        elif (sp.get("licence") or {}).get("skilled_worker"):
            adj(10, "employer holds a sponsor licence")
        elif x.get("register_loaded"):
            adj(-40, "employer isn't on the sponsor register")
        if vp.get("verdict") == "below":
            adj(-30, f"pay is below the £{vp['needed']:,} sponsorship minimum")
        elif vp.get("verdict") == "range":
            adj(-5, "only the top of the range meets the sponsorship minimum")
        elif vp.get("verdict") == "meets":
            adj(5, "pay meets the sponsorship minimum")
        if vp and not vp.get("degree_level"):
            adj(-10, "occupation only open to sponsorship temporarily")
    if p.get("avoid_agencies") and (x.get("sponsor") or {}).get("agency"):
        adj(-10, "recruitment agency")
    g = (x.get("ghost") or {}).get("level")
    if g == "high":
        adj(-20, "high ghost-job risk")
    elif g == "medium":
        adj(-8, "some ghost-job signals")
    for prev in x.get("previous") or []:
        if prev["kind"] == "same_role":
            adj(-50, "you've already applied")
        elif prev["kind"] == "rejected_recently":
            adj(-15, "rejected by this employer recently")
    final = max(0, min(100, round(score)))
    mode = (x.get("work") or {}).get("mode")
    if mode and mode in p["modes"]:
        pros.insert(0, f"{mode.lower()} suits you")
    return {"score": final, "reasons": why, "label": fit_label(final),
            "why": explain(job, m, terms, pros, cons)}


_BOARD_NAMES = {"nhs jobs", "reed", "indeed", "totaljobs", "adzuna", "work hub", "find a job", "jobserve",
                "government recruitment service", "civil service jobs", "linkedin"}
_SKILL_CASE = {"ci/cd": "CI/CD", "power bi": "Power BI", "sql server": "SQL Server", "t-sql": "T-SQL", "azure data factory": "Azure Data Factory",
               "pyspark": "PySpark", "postgresql": "PostgreSQL", "bigquery": "BigQuery", "github": "GitHub",
               "javascript": "JavaScript", "typescript": "TypeScript", "excel": "Excel", "python": "Python",
               "tableau": "Tableau", "looker": "Looker", "snowflake": "Snowflake", "databricks": "Databricks",
               "airflow": "Airflow", "spark": "Spark", "kafka": "Kafka", "terraform": "Terraform", "docker": "Docker",
               "kubernetes": "Kubernetes", "azure": "Azure", "react": "React", "django": "Django", "hubspot": "HubSpot"}


def _skill(term: str) -> str:
    t = term.strip()
    if t.lower() in _SKILL_CASE:
        return _SKILL_CASE[t.lower()]
    return t.upper() if len(t) <= 4 and t.isalpha() and t.lower() not in ("data", "lead", "agile", "etl") or t.lower() == "etl" else t


def fit_label(score: int) -> str:
    return "Strong match" if score >= 75 else "Good match" if score >= 60 else "Needs work" if score >= 40 else "Low match"


def explain(job: dict, m: dict, terms: int, pros: list[str], cons: list[str]) -> str:
    """Two or three plain sentences on why a job scores as it does, from the same signals as the score."""
    have, miss = m.get("have") or [], m.get("missing") or []
    if job.get("cv_match") is not None:
        first = f"Your tailored CV shows {job['cv_match']}% of what this advert asks for."
    elif terms < 4:
        first = "The advert names few skills, so read the full description before deciding."
    else:
        n = len(have) + len(miss)
        first = (f"You have all {n} skills it names" if not miss else f"You have {len(have)} of the {n} skills it names") + \
                (f" ({', '.join(_skill(t) for t in have[:3])})" if have else "") + "."
        if miss:
            first += f" Missing: {', '.join(_skill(t) for t in miss[:3])}."
    if m.get("title_hit"):
        first += " The title matches your search."
    out = [first]
    if pros:
        out.append(pros[0][:1].upper() + pros[0][1:] + (f", and it {pros[1]}" if len(pros) > 1 and pros[1].startswith("pays") else
                                                       f"; {pros[1]}" if len(pros) > 1 else "") + ".")
    if cons:
        out.append("But " + cons[0] + (f", and {cons[1]}" if len(cons) > 1 else "") + ".")
    return " ".join(out)


def _salary_floor(text: str):
    import sponsors
    lo, _, annual = sponsors._money_range(text)
    return lo if annual else None


def _salary_top(text: str):
    import sponsors
    _, hi, annual = sponsors._money_range(text)
    return hi if annual else None
