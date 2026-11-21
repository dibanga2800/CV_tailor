"""Application outcomes: what happened to each application, and what the CV looked like when sent.

The tracker CSV (job_search_tracker.csv) stays the source of truth for status, in the repo's
vocabulary. Each application tailored here also keeps documents/applications/<slug>/outcome.json:

  {"snapshot": {...what was sent: CV version, scores, job facts...},
   "events": [{"date": "2026-11-20", "status": "applied", "note": ""}, ...]}

From these the app reports interview and offer rates overall and by role type, source, work mode,
sponsorship, match score band and CV features. Small groups are labelled as such: with a handful of
applications any difference can be chance.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime

import store

STATUSES = ["drafted", "applied", "interview", "offer", "hired", "rejected", "no_response", "withdrawn",
            "offer_declined"]
INTERVIEWED = {"interview", "offer", "hired", "offer_declined"}
OFFERED = {"offer", "hired", "offer_declined"}
LABEL = {"no_response": "no response", "offer_declined": "offer declined"}


def _path(slug: str):
    return store.app_dir(slug) / "outcome.json"


def load(slug: str) -> dict:
    return store.load_json(_path(slug), None) or {"events": [], "snapshot": {}}


def record(slug: str, status: str, note: str = "", when: str = "", snapshot: dict | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status}")
    o = load(slug)
    if snapshot:
        o["snapshot"] = snapshot
    o["events"].append({"date": (when or date.today().isoformat())[:10], "status": status,
                        "note": (note or "").strip()[:300], "at": time.time()})
    store.save_json(_path(slug), o)
    return o


def role_family(title: str) -> str:
    t = (title or "").lower()
    if re.search(r"\b(head|director|manager)\b", t):
        return "Manager / head"
    if re.search(r"\blead\b|\bprincipal\b|\barchitect\b", t):
        return "Lead / principal"
    if re.search(r"engineer|developer|platform", t):
        return "Engineer"
    if re.search(r"\bbi\b|power bi|business intelligence|reporting", t):
        return "BI / reporting"
    if re.search(r"analyst|analytics|insight|information", t):
        return "Analyst"
    if re.search(r"scien", t):
        return "Data scientist"
    return "Other"


def seniority(title: str) -> str:
    t = (title or "").lower()
    if re.search(r"\b(head|director|principal|lead|manager)\b", t):
        return "Lead and above"
    if re.search(r"\b(senior|sr)\b", t):
        return "Senior"
    if re.search(r"\b(junior|graduate|trainee|entry|apprentice)\b", t):
        return "Junior"
    return "Mid / unstated"


def band(v, edges=(60, 75, 85)) -> str:
    if v is None:
        return "not scored"
    lo = 0
    for e in edges:
        if v < e:
            return f"{lo}-{e - 1}%"
        lo = e
    return f"{edges[-1]}%+"


def _reached(events: list[dict], status: str) -> set[str]:
    seen = {e["status"] for e in events}
    seen.add(status)
    return seen


def rows(drafts: list[dict], job_index: dict) -> list[dict]:
    """One row per application: tracker status joined with the snapshot taken when it was sent."""
    tracker = store.tracker_rows()
    out, used = [], set()
    import jobs as jobsrc

    def find_row(company, role):
        best = None
        for i, r in enumerate(tracker):
            if (jobsrc.norm_company(r.get("company", "")) == jobsrc.norm_company(company)
                    and jobsrc.norm_title(r.get("role", "")) == jobsrc.norm_title(role)):
                best = i
        return best

    for d in drafts:
        o = load(d["slug"])
        i = find_row(d["company"], d["role"])
        tr = tracker[i] if i is not None else {}
        if i is not None:
            used.add(i)
        status = (tr.get("status") or (o["events"][-1]["status"] if o["events"] else "drafted")).strip().lower()
        status = status.replace(" ", "_")
        snap = o.get("snapshot") or {}
        reached = _reached(o["events"], status)
        applied_date = next((e["date"] for e in o["events"] if e["status"] == "applied"), "") or \
            (tr.get("date", "") if status != "drafted" else "")
        out.append({
            "slug": d["slug"], "company": d["company"], "role": d["role"], "status": status,
            "applied": applied_date, "events": o["events"], "snapshot": snap, "external": False,
            "interviewed": bool(reached & INTERVIEWED), "offered": bool(reached & OFFERED),
            "sent": status != "drafted" or "applied" in reached,
            "last_note": next((e["note"] for e in reversed(o["events"]) if e.get("note")), ""),
            "match": snap.get("match", d.get("match")),
        })
    for i, r in enumerate(tracker):  # applications made outside this app
        if i in used:
            continue
        status = (r.get("status") or "").strip().lower().replace(" ", "_")
        out.append({"slug": "", "company": r.get("company", ""), "role": r.get("role", ""), "status": status,
                    "applied": r.get("date", ""), "events": [], "snapshot": {}, "external": True,
                    "interviewed": status in INTERVIEWED, "offered": status in OFFERED,
                    "sent": status not in ("", "drafted"), "last_note": (r.get("notes") or "")[-200:],
                    "match": None})
    out.sort(key=lambda r: r["applied"] or "", reverse=True)
    return out


def _group(rs: list[dict], key) -> list[dict]:
    groups: dict = {}
    for r in rs:
        groups.setdefault(key(r), []).append(r)
    out = []
    for k, g in groups.items():
        n, i, o = len(g), sum(r["interviewed"] for r in g), sum(r["offered"] for r in g)
        out.append({"group": k, "sent": n, "interviews": i, "offers": o,
                    "interview_rate": round(100 * i / n) if n else None, "small": n < 5})
    out.sort(key=lambda x: (-x["sent"], x["group"]))
    return out


def analytics(rs: list[dict]) -> dict:
    sent = [r for r in rs if r["sent"] and r["status"] not in ("withdrawn",)]
    decided = [r for r in sent if r["status"] != "applied" or r["interviewed"]]
    n, i, o = len(sent), sum(r["interviewed"] for r in sent), sum(r["offered"] for r in sent)
    snap = lambda r, k, default="unknown": (r["snapshot"] or {}).get(k) or default  # noqa: E731
    tailored = [r for r in sent if not r["external"]]
    by = {
        "Role type": _group(sent, lambda r: role_family(r["role"])),
        "Seniority": _group(sent, lambda r: seniority(r["role"])),
        "Where you found it": _group(tailored, lambda r: snap(r, "source")),
        "Work mode": _group(tailored, lambda r: snap(r, "mode", "not stated")),
        "Employer can sponsor": _group(tailored, lambda r: {True: "licensed sponsor", False: "not on register"}.get(
            (r["snapshot"] or {}).get("sponsor_licensed"), "unknown")),
        "Job match when sent": _group(tailored, lambda r: band(r["match"])),
        "ATS keyword score when sent": _group(tailored, lambda r: band(snap(r, "ats_keywords", None))),
        "Cover letter": _group(tailored, lambda r: "with cover letter" if snap(r, "cover_letter", False) else "CV only"),
        "Projects section": _group(tailored, lambda r: "had a projects section" if snap(r, "has_projects", False) else "no projects section"),
    }
    days = []
    for r in sent:
        a = next((e["date"] for e in r["events"] if e["status"] == "applied"), r["applied"])
        first = next((e["date"] for e in r["events"] if e["status"] in INTERVIEWED | {"rejected"}), "")
        try:
            if a and first:
                days.append((datetime.fromisoformat(first) - datetime.fromisoformat(a)).days)
        except ValueError:
            pass
    waiting = [r for r in sent if r["status"] == "applied" and r["applied"]]
    stale = []
    for r in waiting:
        try:
            age = (datetime.now() - datetime.fromisoformat(r["applied"][:10])).days
        except ValueError:
            continue
        if age >= 21:
            stale.append({"company": r["company"], "role": r["role"], "days": age, "slug": r["slug"]})
    return {
        "totals": {"sent": n, "interviews": i, "offers": o, "decided": len(decided),
                   "interview_rate": round(100 * i / n) if n else None,
                   "offer_rate": round(100 * o / n) if n else None,
                   "median_days_to_reply": sorted(days)[len(days) // 2] if days else None},
        "by": by, "stale": sorted(stale, key=lambda x: -x["days"]),
        "note": "Groups under 5 applications are marked: differences there can easily be chance.",
    }
