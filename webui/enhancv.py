"""Optional: send a tailored CV to Enhancv and fetch their designed PDF.

Uses the Enhancv public API (developers.enhancv.com): POST /resumes to create a resume from
structured data, GET /resumes/{id}/pdf to export it. Needs ENHANCV_API_KEY in webui/.env,
and Enhancv only issues API keys on its Business plan. Limits: 60 requests/minute,
1,000/hour. Cover letters are not supported by their API.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from checks import parse_range

BASE = "https://api.enhancv.com/api/v1"
LAYOUTS = ["single", "classic", "minimal", "elegant", "ivyleague", "polished", "double", "condensed"]


class EnhancvError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.environ.get("ENHANCV_API_KEY"))


def _req(method: str, path: str, body: dict | None = None, timeout: int = 60):
    key = os.environ.get("ENHANCV_API_KEY")
    if not key:
        raise EnhancvError("Add ENHANCV_API_KEY to webui/.env (Enhancv Business plan only).")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "ai-job-search-webui/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", "")
        except Exception:
            msg = ""
        hint = {401: "API key missing or invalid", 403: "your Enhancv plan's resume limit is reached",
                429: "rate limit hit, wait a minute", 504: "PDF generation timed out, try again"}.get(e.code, "")
        raise EnhancvError(f"Enhancv {e.code}: {msg or hint}") from e


def _date_range(text: str) -> dict | None:
    r = parse_range(text or "")
    if not r:
        return None
    (a, b) = r
    ongoing = any(w in (text or "").lower() for w in ("present", "current", "now", "ongoing"))
    out = {"fromYear": (a - 1) // 12, "fromMonth": (a - 1) % 12, "isOngoing": ongoing}  # months 0-based
    if not ongoing:
        out.update(toYear=(b - 1) // 12, toMonth=(b - 1) % 12)
    return out


def to_enhancv(draft: dict, layout: str = "single") -> dict:
    cv = draft["cv"]
    c = cv.get("contact", {})
    hidden = set(cv.get("hidden_sections") or [])
    order = {k: i for i, k in enumerate(cv.get("section_order", []))}
    links = [l.get("url") for l in c.get("links", []) if l.get("url")]
    sections: dict = {}
    if cv.get("summary") and "summary" not in hidden:
        sections["summaries"] = {"name": "Summary", "column": 0, "order": order.get("summary", 0),
                                 "items": [{"text": " ".join(s.get("text", "") for s in cv["summary"])}]}
    if cv.get("experience") and "experience" not in hidden:
        sections["experiences"] = {"name": "Experience", "column": 0, "order": order.get("experience", 2),
                                   "items": [{k: v for k, v in {
                                       "workplace": e.get("company", ""), "position": e.get("title", ""),
                                       "location": e.get("location", ""),
                                       "dateRange": _date_range(e.get("dates", "")),
                                       "bullets": [b.get("text", "") for b in e.get("bullets", [])]}.items() if v}
                                       for e in cv["experience"]]}
    if cv.get("education") and "education" not in hidden:
        sections["educations"] = {"name": "Education", "column": 0, "order": order.get("education", 4),
                                  "items": [{k: v for k, v in {
                                      "institution": e.get("institution", ""), "degree": e.get("qualification", ""),
                                      "location": e.get("location", ""), "dateRange": _date_range(e.get("dates", "")),
                                      "bullets": [e["detail"]] if e.get("detail") else None}.items() if v}
                                      for e in cv["education"]]}
    if cv.get("skills") and "skills" not in hidden:
        sections["skills"] = {"name": "Skills", "column": 0, "order": order.get("skills", 1),
                              "items": [{"title": k.get("category", ""), "tags": k.get("items", [])}
                                        for k in cv["skills"]]}
    if cv.get("certifications") and "certifications" not in hidden:
        sections["certificates"] = {"name": "Certifications", "column": 0, "order": order.get("certifications", 5),
                                    "items": [{"title": x.get("text", "")} for x in cv["certifications"]]}
    if cv.get("projects") and "projects" not in hidden:
        sections["projects"] = {"name": "Projects", "column": 0, "order": order.get("projects", 3),
                                "items": [{"title": p.get("name", ""), "description": p.get("stack", ""),
                                           "bullets": [b.get("text", "") for b in p.get("bullets", [])]}
                                          for p in cv["projects"]]}
    t = draft.get("target", {})
    return {
        "title": f"{t.get('role', 'CV')} - {t.get('company', '')}".strip(" -"),
        "header": {k: v for k, v in {"name": cv.get("name", ""), "title": cv.get("subtitle", ""),
                                     "email": c.get("email", ""), "phone": c.get("phone", ""),
                                     "location": c.get("location", ""),
                                     "website": links[0] if links else ""}.items() if v},
        "sections": sections,
        "style": {"layout": layout if layout in LAYOUTS else "single", "isLetterSize": False},
    }


def push(draft: dict, layout: str = "single") -> str:
    raw, _ = _req("POST", "/resumes", to_enhancv(draft, layout))
    rid = json.loads(raw.decode()).get("id")
    if not rid:
        raise EnhancvError("Enhancv did not return a resume id")
    return rid


def pdf(resume_id: str) -> bytes:
    raw, ctype = _req("GET", f"/resumes/{resume_id}/pdf", timeout=90)
    if "pdf" not in ctype:
        raise EnhancvError("Enhancv did not return a PDF")
    return raw
