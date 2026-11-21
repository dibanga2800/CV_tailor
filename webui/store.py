"""Paths, naming and the tracker - kept in step with the rest of the repo.

* Master CV lives in documents/cv/ (gitignored), the same folder /setup Path A reads.
* Each tailoring run lives in documents/applications/<company>_<role>/ (gitignored),
  the archive folder /outcome and /interview already use.
* Tailored CVs are written to cv/main_<company>_<role>.<ext> (gitignored by cv/main_*.*).
* job_search_tracker.csv gets the same row /apply Step 6b would write.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import date
from pathlib import Path

WEBUI = Path(__file__).resolve().parent
REPO = WEBUI.parent
DOCS = REPO / "documents"
MASTER_DIR = DOCS / "cv"
APPS_DIR = DOCS / "applications"
CV_DIR = REPO / "cv"
TRACKER = REPO / "job_search_tracker.csv"
WINDOWS_PATH_BUDGET = 240 if os.name == "nt" else None
SKILL_DIR = REPO / ".claude" / "skills" / "job-application-assistant"

TRACKER_HEADER = [
    "date", "company", "sector", "role", "role_type", "channel", "status", "contact_person",
    "fit_rating", "notes", "cv_file", "cover_letter_file", "source", "deadline",
]
FINAL_STATUSES = {
    "rejected", "withdrawn", "no_response", "no response", "offer_declined", "offer declined", "hired",
}


def slug_part(text: str) -> str:
    """documents/README.md 'Subfolder naming': lowercase, underscores, [a-z0-9_] only."""
    s = (text or "").lower().replace(" ", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def windows_path_length(path: Path) -> int:
    return len(str(path.absolute()).encode("utf-16-le")) // 2


def app_slug(company: str, role: str) -> str:
    slug = slug_part(f"{company} {role}")
    if not slug:
        raise ValueError("Company and role need at least one letter or digit")
    if WINDOWS_PATH_BUDGET is not None:
        # Allow room for version snapshots and temporary files in the application folder.
        snapshot = APPS_DIR / "x" / "tailor" / "versions" / "20000101-000000-000000.json"
        room = WINDOWS_PATH_BUDGET - windows_path_length(snapshot) + 1
        if len(slug) > room:
            digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:12]
            if room <= len(digest):
                raise ValueError("The workspace path is too long to save applications. Move it closer to the drive root.")
            slug = f"{slug[:room - len(digest) - 1].rstrip('_')}_{digest}"
    return slug


def app_dir(slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_]+", slug):
        raise ValueError("bad slug")
    return APPS_DIR / slug


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".json-", suffix=".tmp", delete=False)
    tmp = Path(handle.name)
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        for attempt in range(5):
            try:
                tmp.replace(path)
                break
            except PermissionError as error:
                if getattr(error, "winerror", None) not in (5, 32) or attempt == 4:
                    raise
                time.sleep(0.025 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)
    _nudge_db()


def _nudge_db() -> None:
    """Tell the Postgres sync (if configured) that something changed, so it saves within a second."""
    import sys
    db = sys.modules.get("db")
    if db is not None:
        db.nudge()


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def history() -> list[dict]:
    out = []
    if not APPS_DIR.exists():
        return out
    for d in APPS_DIR.iterdir():
        draft = load_json(d / "tailor" / "draft.json")
        if not draft:
            continue
        t = draft.get("target", {})
        out.append({
            "slug": d.name,
            "company": t.get("company", ""),
            "role": t.get("role", ""),
            "match": draft.get("_scores", {}).get("match"),
            "quality": draft.get("_scores", {}).get("quality"),
            "updated": (d / "tailor" / "draft.json").stat().st_mtime,
        })
    out.sort(key=lambda r: r["updated"], reverse=True)
    return out


def record_in_tracker(company: str, role: str, fit: int, cv_file: str, deadline: str = "",
                      source: str = "") -> str:
    """Append or refresh a row the way /apply Step 6b does. Returns a short note."""
    rows: list[dict] = []
    header = list(TRACKER_HEADER)
    if TRACKER.exists():
        with TRACKER.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or header
            rows = list(reader)
        if "deadline" not in header:
            header.append("deadline")
    today = date.today().isoformat()
    open_match = None
    for r in rows:
        if (r.get("company", "").strip().lower() == company.strip().lower()
                and r.get("role", "").strip().lower() == role.strip().lower()
                and r.get("status", "").strip().lower() not in FINAL_STATUSES):
            open_match = r
    if open_match is not None:
        open_match["cv_file"] = cv_file
        open_match["fit_rating"] = str(fit)
        if deadline:
            open_match["deadline"] = deadline
        notes = open_match.get("notes", "")
        if "redrafted" not in notes.split("; ")[-1:]:
            open_match["notes"] = (notes + "; redrafted").lstrip("; ")
        if open_match.get("status", "").strip().lower() == "drafted":
            open_match["date"] = today
        note = "updated the open tracker row"
    else:
        rows.append({
            "date": today, "company": company, "role": role, "status": "drafted",
            "fit_rating": str(fit), "cv_file": cv_file, "source": source, "deadline": deadline,
            "notes": "tailored in web UI",
        })
        note = "added a tracker row (status drafted)"
    with TRACKER.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})
    return note


def mark_applied(company: str, role: str) -> str:
    """Move the open row for this company+role from drafted to applied (the /outcome vocabulary)."""
    if not TRACKER.exists():
        return "no tracker file"
    with TRACKER.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or TRACKER_HEADER
        rows = list(reader)
    hit = None
    for r in rows:
        if (r.get("company", "").strip().lower() == company.strip().lower()
                and r.get("role", "").strip().lower() == role.strip().lower()
                and r.get("status", "").strip().lower() not in FINAL_STATUSES):
            hit = r
    if hit is None:
        return "no open tracker row found"
    if hit.get("status") in ("applied", "interview", "offer"):
        return f"already {hit['status']}"
    today = date.today().isoformat()
    hit["status"] = "applied"
    hit["date"] = today
    hit["notes"] = (hit.get("notes", "") + f"; applied {today} via web UI").lstrip("; ")
    with TRACKER.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})
    return "marked applied in the tracker"


def tracker_rows() -> list[dict]:
    if not TRACKER.exists():
        return []
    with TRACKER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def previous_index() -> dict:
    """Tracker rows by normalised company, for 'already applied' / 'rejected before' warnings."""
    import jobs as jobsrc
    idx: dict = {}
    for r in tracker_rows():
        idx.setdefault(jobsrc.norm_company(r.get("company", "")), []).append(r)
    return idx


def previous_for(company: str, title: str, idx: dict) -> list[dict]:
    """Warnings about earlier applications to this employer (same role, or a recent rejection)."""
    import jobs as jobsrc
    from datetime import datetime, timedelta
    out = []
    t = jobsrc.norm_title(title)
    for r in idx.get(jobsrc.norm_company(company), []):
        status = (r.get("status") or "").strip().lower()
        when = (r.get("date") or "")[:10]
        same_role = jobsrc.norm_title(r.get("role", "")) == t
        if same_role and status not in ("", "drafted"):
            out.append({"kind": "same_role", "status": status, "date": when,
                        "text": f"You already applied for this role ({status.replace('_', ' ')}, {when})"})
        elif same_role:
            out.append({"kind": "drafted", "status": status, "date": when, "text": f"You drafted a CV for this role on {when}"})
        elif status == "rejected":
            try:
                recent = datetime.fromisoformat(when) > datetime.now() - timedelta(days=183)
            except ValueError:
                recent = False
            if recent:
                out.append({"kind": "rejected_recently", "status": status, "date": when,
                            "text": f"This employer rejected you for {r.get('role', 'another role')} on {when}"})
    return out


def set_status(company: str, role: str, status: str, note: str = "", when: str = "") -> str:
    """Record a new status for company+role in the tracker (open row first, else the latest row).
    The date column keeps the date applied; each change is appended to notes as a dated entry."""
    rows = tracker_rows()
    header = TRACKER_HEADER
    if TRACKER.exists():
        with TRACKER.open(newline="", encoding="utf-8") as f:
            header = csv.DictReader(f).fieldnames or TRACKER_HEADER
    match = [r for r in rows if r.get("company", "").strip().lower() == company.strip().lower()
             and r.get("role", "").strip().lower() == role.strip().lower()]
    open_rows = [r for r in match if r.get("status", "").strip().lower() not in FINAL_STATUSES]
    hit = (open_rows or match or [None])[-1]
    when = (when or date.today().isoformat())[:10]
    if hit is None:
        hit = {k: "" for k in header}
        hit.update(company=company, role=role, date=when, channel="web UI")
        rows.append(hit)
    if status == "applied" or not hit.get("date") or hit.get("status") == "drafted":
        hit["date"] = when
    hit["status"] = status
    entry = f"{when} {status.replace('_', ' ')}" + (f": {note.strip()}" if note.strip() else "")
    hit["notes"] = (hit.get("notes", "") + "; " + entry).lstrip("; ")
    with TRACKER.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})
    return f"tracker set to {status.replace('_', ' ')}"
