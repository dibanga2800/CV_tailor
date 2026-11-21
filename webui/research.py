"""Company research for interview prep.

Reads the employer's own website (home, about, values, careers pages) and recent news headlines,
then asks the AI to summarise them. Every point must name its source, and anything without a
source we actually fetched is dropped, so nothing is made up.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import engine

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ai-job-search-webui/1.0 (personal interview prep)"
MAX_BYTES = 1_500_000
PAGE_CHARS = 3500

# Job boards and applicant tracking systems: their domains are never the employer's own site.
NOT_EMPLOYER = (
    "indeed.", "reed.co.uk", "totaljobs", "cv-library", "adzuna", "linkedin.", "jobserve", "glassdoor",
    "jobs.nhs.uk", "trac.jobs", "civilservicejobs", "charityjob", "jobs.ac.uk", "findajob", "workhub",
    "myworkdayjobs", "workday.com", "greenhouse.io", "lever.co", "smartrecruiters", "workable.com",
    "ashbyhq", "teamtailor", "bamboohr", "icims", "taleo", "successfactors", "applytojob", "eploy",
    "tal.net", "jobvite", "recruitee", "pinpointhq", "hirehive", "occupop", "networx", "jobtrain",
    "google.", "bit.ly", "youtube.", "facebook.", "twitter.", "x.com", "instagram.", "tiktok.",
    "gov.uk/apply", "yugenox", "iuk-ktp", "weworkremotely", "simplyhired", "monster.", "guardianjobs",
)
PAGE_WORDS = re.compile(r"about|who-we-are|our-story|story|values|culture|mission|purpose|vision|strategy|"
                        r"careers|working-for-us|work-for-us|join-us|life-at|people|benefits|news|press", re.I)
_URL = re.compile(r"https?://[^\s<>()\"']+", re.I)
_STOP = {"ltd", "limited", "plc", "inc", "llc", "group", "the", "uk", "and", "of", "trust", "nhs", "foundation",
         "services", "company", "co", "holdings", "international"}


# ----------------------------------------------------------------------------- fetching

def _fetch(url: str, timeout: int = 15, accept: str = "text/html,application/xhtml+xml") -> tuple[str, str]:
    """Return (final url, text). Only http(s); size-capped."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("only web addresses can be read")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept, "Accept-Language": "en-GB,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(MAX_BYTES)
        charset = r.headers.get_content_charset() or "utf-8"
        return r.geturl(), raw.decode(charset, "replace")


FETCH = _fetch  # tests swap this for canned pages


def _text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form)\b.*?</\1>", " ", page)
    page = re.sub(r"(?s)<!--.*?-->", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|li|h[1-6]|div|section)>", "\n", page)
    page = html.unescape(re.sub(r"<[^>]+>", " ", page))
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in page.splitlines()]
    return "\n".join(ln for ln in lines if len(ln) > 2)


def _title(page: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:120] if m else ""


def _tokens(company: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", company.lower()) if w not in _STOP and len(w) > 2]


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


def _is_employer_host(host: str) -> bool:
    return bool(host) and not any(b in host for b in NOT_EMPLOYER)


def guess_website(company: str, texts: list[str], links: list[str]) -> str:
    """The employer's own site, from links in the advert or the apply link. Empty if unsure."""
    toks = _tokens(company)
    cands = []
    for u in links + [u for t in texts for u in _URL.findall(t or "")]:
        host = _host(u.rstrip(".,;"))
        if _is_employer_host(host):
            score = sum(t in host.replace("-", "") for t in toks)
            cands.append((score, host))
    cands.sort(key=lambda c: -c[0])
    if cands and (cands[0][0] > 0 or not toks):
        return "https://" + cands[0][1]
    return ""


def _search_website(company: str) -> str:
    """Last resort: a plain web search for the official site, keeping only a domain that carries the name."""
    toks = _tokens(company)
    if not toks:
        return ""
    try:
        _, page = FETCH("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": f"{company} official website UK"}))
    except Exception:  # noqa: BLE001
        return ""
    for raw in re.findall(r'href="([^"]+)"', page):
        u = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlsplit(html.unescape(raw)).query).get("uddg", [raw])[0])
        host = _host(u)
        if u.startswith("http") and _is_employer_host(host) and "duckduckgo" not in host \
                and sum(t in host.replace("-", "") for t in toks) >= min(2, len(toks)):
            return "https://" + host
    return ""


def read_site(website: str, max_pages: int = 5) -> tuple[list[dict], str]:
    """Home page plus the most useful inner pages. Returns (pages, problem)."""
    try:
        home_url, home = FETCH(website)
    except Exception as e:  # noqa: BLE001
        return [], f"Couldn't open {website}: {e}"
    host = _host(home_url)
    pages = [{"url": home_url, "title": _title(home) or host, "text": _text(home)[:PAGE_CHARS]}]
    seen, picks = {home_url.rstrip("/")}, []
    for href in re.findall(r'(?i)href="([^"#]+)"', home):
        u = urllib.parse.urljoin(home_url, html.unescape(href)).split("#")[0].rstrip("/")
        if _host(u) != host or u in seen or re.search(r"\.(pdf|jpe?g|png|zip|docx?)$", u, re.I):
            continue
        path = urllib.parse.urlsplit(u).path
        m = PAGE_WORDS.search(path)
        if m and path.count("/") <= 3:
            seen.add(u)
            picks.append((0 if re.search(r"about|values|culture|mission|who-we-are", path, re.I) else 1, len(path), u))
    for _, _, u in sorted(picks)[:max_pages - 1]:
        try:
            final, page = FETCH(u)
            txt = _text(page)[:PAGE_CHARS]
            if len(txt) > 200:
                pages.append({"url": final, "title": _title(page) or urllib.parse.urlsplit(final).path, "text": txt})
        except Exception:  # noqa: BLE001
            continue
    return pages, ""


def news(company: str, limit: int = 8, days: int = 365) -> tuple[list[dict], str]:
    """Recent headlines from Google News (UK edition) that name the company."""
    q = urllib.parse.urlencode({"q": f'"{company}"', "hl": "en-GB", "gl": "GB", "ceid": "GB:en"})
    try:
        _, xml = FETCH("https://news.google.com/rss/search?" + q, accept="application/rss+xml,application/xml")
        root = ET.fromstring(xml.encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return [], f"Couldn't fetch news: {e}"
    toks, out, cutoff = _tokens(company), [], time.time() - days * 86400
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        try:
            when = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if when.timestamp() < cutoff or (toks and not any(t in title.lower() for t in toks)):
            continue
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        out.append({"title": title, "source": src, "date": when.date().isoformat(), "url": (it.findtext("link") or "").strip()})
        if len(out) >= limit:
            break
    out.sort(key=lambda n: n["date"], reverse=True)
    return out, ""


# ----------------------------------------------------------------------------- summary

SYSTEM = """You are preparing a UK job candidate for an interview. Summarise what is known about the employer.
Use ONLY the numbered sources given. Every statement ends with the source ids it rests on, e.g. [W1] or
[N2, A]. Never use your own knowledge of the company, never guess, and never invent news, figures or
names. If the sources don't cover something, leave that list empty. Write in UK English, plainly.

Return JSON only:
{"overview": {"text": "2 to 3 sentences: what they do, for whom, how big", "sources": ["W1"]},
 "values": [{"text": "a value or way of working, in their words where possible", "sources": ["W2"]}],
 "recent_news": [{"id": "N1", "why_it_matters": "one line on why this could come up at interview"}],
 "hiring_process": [{"text": "a stage or date", "sources": ["A"]}],
 "talking_points": [{"text": "something the candidate can mention to show they've done their homework", "sources": ["W1"]}],
 "questions_to_ask": [{"text": "a sharp question to ask the panel, grounded in the sources", "sources": ["N1"]}]}
Keep each list to at most 5 items. recent_news may only use ids of the news items given."""


def _clean(items, ids: set[str], n: int = 5) -> list[dict]:
    out = []
    for it in items or []:
        if not isinstance(it, dict) or not str(it.get("text", "")).strip():
            continue
        srcs = [s for s in (it.get("sources") or []) if s in ids]
        if srcs:  # unsourced points are dropped
            out.append({"text": str(it["text"]).strip(), "sources": srcs})
    return out[:n]


def research(company: str, website: str, advert: str, person_spec: str, links: list[str]) -> dict:
    company = company.strip()
    website = (website or "").strip()
    notes = []
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website
    if not website:
        website = guess_website(company, [advert, person_spec], links) or _search_website(company)
    pages, problem = read_site(website) if website else ([], "")
    if problem:
        notes.append(problem)
    if not website:
        notes.append("Couldn't find the employer's website. Add it and research again.")
    items, problem = news(company) if company else ([], "")
    if problem:
        notes.append(problem)

    sources = {"A": {"kind": "advert", "title": "The job advert", "url": ""}}
    blocks = ["[A] The job advert\n" + (advert[:5000] + ("\n\nPerson specification:\n" + person_spec[:2500] if person_spec else ""))]
    for i, p in enumerate(pages, 1):
        sid = f"W{i}"
        sources[sid] = {"kind": "website", "title": p["title"], "url": p["url"]}
        blocks.append(f"[{sid}] {p['title']} ({p['url']})\n{p['text']}")
    for i, n in enumerate(items, 1):
        sid = f"N{i}"
        sources[sid] = {"kind": "news", "title": n["title"], "url": n["url"], "date": n["date"], "publisher": n["source"]}
        blocks.append(f"[{sid}] {n['date']} · {n['source']}: {n['title']}")

    user = f"Employer: {company}\n\n" + "\n\n".join(blocks)
    out, used, _ = engine.complete_json(SYSTEM, user, mock=_mock, expected={"overview": dict})
    ids = set(sources)
    news_ids = {k for k in ids if k.startswith("N")}
    ov = out.get("overview") or {}
    ov_src = [s for s in ov.get("sources") or [] if s in ids]
    rn = []
    for it in out.get("recent_news") or []:
        if isinstance(it, dict) and it.get("id") in news_ids:
            rn.append({**sources[it["id"]], "id": it["id"], "why_it_matters": str(it.get("why_it_matters", "")).strip()})
    return {
        "company": company, "website": website, "researched_at": time.time(),
        "engine": engine.LABELS.get(used, used),
        "overview": {"text": str(ov.get("text", "")).strip(), "sources": ov_src} if ov_src else None,
        "values": _clean(out.get("values"), ids),
        "recent_news": rn[:5],
        "headlines": items,  # everything fetched, in case the summary skipped something
        "hiring_process": _clean(out.get("hiring_process"), ids),
        "talking_points": _clean(out.get("talking_points"), ids),
        "questions_to_ask": _clean(out.get("questions_to_ask"), ids),
        "sources": sources, "notes": notes,
    }


def _mock() -> str:
    return json.dumps({
        "overview": {"text": "A housing association managing homes across the north west.", "sources": ["W1"]},
        "values": [{"text": "Tenants first", "sources": ["W2"]}, {"text": "Made up value", "sources": []}],
        "recent_news": [{"id": "N1", "why_it_matters": "Shows investment in data."}, {"id": "N9", "why_it_matters": "x"}],
        "hiring_process": [{"text": "Panel interview", "sources": ["A"]}],
        "talking_points": [{"text": "Their new data strategy", "sources": ["W2", "N1"]}],
        "questions_to_ask": [{"text": "How will the new data platform change reporting?", "sources": ["N1"]}],
    })


def path(slug: str):
    import store
    return store.app_dir(slug) / "company_research.json"


def load(slug: str) -> dict | None:
    p = path(slug)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


if os.environ.get("CV_TAILOR_RESEARCH_FIXTURES"):  # e2e tests: canned pages instead of the internet
    _FIX = json.loads(open(os.environ["CV_TAILOR_RESEARCH_FIXTURES"], encoding="utf-8").read())

    def FETCH(url, timeout=15, accept=""):  # noqa: F811,N802
        for key, body in _FIX.items():
            if key in url:
                return url, body
        raise OSError("not found")
