"""PostgreSQL storage for CV Tailor.

When DATABASE_URL is set (webui/.env, gitignored), Postgres is the system of record for everything
the app keeps about you:

  documents/**            master CV and facts, skills bank, imported projects, tailored CVs and their
                          versions, job adverts, the job list and its statuses, outcomes, interview
                          packs, ATS work, settings
  job_search_tracker.csv  the application tracker

The files on disk stay as the working copy the app and the repo's Claude Code commands (/apply,
/outcome) read, and are kept in step both ways:

  start-up    anything missing or older on disk is restored from Postgres (a new PC, a wiped folder)
  running     every change on disk is written to Postgres within a few seconds, including changes
              made outside the app, e.g. by /apply in Claude Code
  deletes     a file the app deletes is deleted in Postgres too

Tables:
  cvt_files               path, content, sha256, mtime      (the store itself)
  cvt_jobs                one row per job in the job list   (for SQL / Power BI)
  cvt_applications        one row per tracker row
  cvt_application_events  every status change recorded in the Applications tab
  cvt_facts               your fact bank, numbered as the app numbers it

Without DATABASE_URL the app works exactly as before, on files only.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import threading
import time
from pathlib import Path

from store import DOCS, REPO, TRACKER

EXCLUDE_PARTS = {".gitkeep", "__pycache__"}
EXCLUDE_NAMES = {"sponsors.csv", "sponsor_register.csv"}  # the public register: re-downloadable, large
MAX_BYTES = 25 * 1024 * 1024
INTERVAL = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS cvt_files (
    path        text PRIMARY KEY,
    content     bytea NOT NULL,
    sha256      text NOT NULL,
    size        integer NOT NULL,
    mtime       double precision NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cvt_jobs (
    key text PRIMARY KEY, title text, company text, location text, source text, sources text,
    status text, salary text, posted text, first_seen text, last_seen text, url text,
    match_score integer, description_chars integer, data jsonb
);
CREATE TABLE IF NOT EXISTS cvt_applications (
    row_no integer PRIMARY KEY, date text, company text, sector text, role text, role_type text,
    channel text, status text, contact_person text, fit_rating text, notes text, cv_file text,
    cover_letter_file text, source text, deadline text
);
CREATE TABLE IF NOT EXISTS cvt_application_events (
    slug text, seq integer, date text, status text, note text, PRIMARY KEY (slug, seq)
);
CREATE TABLE IF NOT EXISTS cvt_facts (id text PRIMARY KEY, section text, text text);
CREATE TABLE IF NOT EXISTS cvt_meta (key text PRIMARY KEY, value text);
"""

_state = {"enabled": False, "connected": False, "error": "", "last_sync": None, "files": 0, "bytes": 0,
          "server": "", "restored": 0, "pushed": 0, "deleted": 0}
_known: dict[str, tuple[int, float, str]] = {}   # path -> (size, mtime, sha) as last synced
_seen_local: set[str] = set()                    # paths this process has seen on disk
_wake = threading.Event()
_lock = threading.Lock()
_thread: threading.Thread | None = None


def url() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


def connect(dsn: str | None = None):
    import psycopg
    return psycopg.connect(dsn or url(), connect_timeout=5)


def test(dsn: str) -> dict:
    """Connect, create the tables, report the server version. Raises on failure."""
    with connect(dsn) as c:
        c.execute(SCHEMA)
        v = c.execute("SHOW server_version").fetchone()[0]
        n = c.execute("SELECT count(*), coalesce(sum(size),0) FROM cvt_files").fetchone()
    return {"server": f"PostgreSQL {v}", "files": n[0], "bytes": int(n[1])}


# ----------------------------------------------------------------------------- local files

def _key(p: Path) -> str:
    return p.resolve().relative_to(REPO.resolve()).as_posix()


def _path(key: str) -> Path:
    p = (REPO / key).resolve()
    if REPO.resolve() not in p.parents:
        raise ValueError(f"refusing to write outside the repo: {key}")
    return p


def _local_files() -> dict[str, Path]:
    out = {}
    if DOCS.exists():
        for p in DOCS.rglob("*"):
            if (p.is_file() and not (set(p.parts) & EXCLUDE_PARTS) and p.name not in EXCLUDE_NAMES
                    and not p.name.endswith((".tmp", ".lock", "~"))):
                try:
                    if p.stat().st_size <= MAX_BYTES:
                        out[_key(p)] = p
                except OSError:
                    pass
    if TRACKER.exists():
        out[_key(TRACKER)] = TRACKER
    return out


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------------- sync

def restore(conn) -> int:
    """Write to disk anything missing locally, or older on disk than in Postgres."""
    n = 0
    rows = conn.execute("SELECT path, sha256, mtime, size FROM cvt_files").fetchall()
    local = _local_files()
    for path, sha, mtime, size in rows:
        p = local.get(path)
        if p is not None:
            st = p.stat()
            if st.st_mtime >= mtime - 1:          # disk is as new or newer: the push pass handles it
                continue
            data = p.read_bytes()
            if _sha(data) == sha:
                _known[path] = (st.st_size, st.st_mtime, sha)
                continue
        content = conn.execute("SELECT content FROM cvt_files WHERE path = %s", (path,)).fetchone()[0]
        target = _path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".restore.tmp")
        tmp.write_bytes(bytes(content))
        os.replace(tmp, target)
        os.utime(target, (mtime, mtime))
        _known[path] = (size, mtime, sha)
        n += 1
    return n


def push(conn) -> tuple[int, int, set[str]]:
    """Upsert changed local files; delete rows for files this process saw and that are now gone."""
    local = _local_files()
    if not _known:  # first pass: learn what Postgres already holds, so unchanged files aren't re-sent
        for path, sha, size, mtime in conn.execute("SELECT path, sha256, size, mtime FROM cvt_files"):
            _known[path] = (size, mtime, sha)
    pushed, changed = 0, set()
    for key, p in local.items():
        try:
            st = p.stat()
        except OSError:
            continue
        _seen_local.add(key)
        k = _known.get(key)
        if k and k[0] == st.st_size and abs(k[1] - st.st_mtime) < 0.001:
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue  # being written; next pass
        sha = _sha(data)
        if k and k[2] == sha:
            _known[key] = (st.st_size, st.st_mtime, sha)
            continue
        conn.execute(
            "INSERT INTO cvt_files (path, content, sha256, size, mtime, updated_at) VALUES (%s,%s,%s,%s,%s,now()) "
            "ON CONFLICT (path) DO UPDATE SET content = EXCLUDED.content, sha256 = EXCLUDED.sha256, "
            "size = EXCLUDED.size, mtime = EXCLUDED.mtime, updated_at = now()",
            (key, data, sha, st.st_size, st.st_mtime))
        _known[key] = (st.st_size, st.st_mtime, sha)
        pushed += 1
        changed.add(key)
    gone = [k for k in list(_seen_local) if k not in local]
    for key in gone:
        conn.execute("DELETE FROM cvt_files WHERE path = %s", (key,))
        _seen_local.discard(key)
        _known.pop(key, None)
        changed.add(key)
    return pushed, len(gone), changed


def refresh_reporting(conn, changed: set[str] | None = None) -> None:
    """Rebuild the queryable tables when their source files changed (or always if changed is None)."""
    def touched(pred):
        return changed is None or any(pred(c) for c in changed)
    if touched(lambda c: c.endswith("postings/webui_jobs.json")):
        import jobs as jobsrc
        st = jobsrc.load_store()
        conn.execute("DELETE FROM cvt_jobs")
        for k, j in st.get("jobs", {}).items():
            conn.execute(
                "INSERT INTO cvt_jobs VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (k, j.get("title"), j.get("company"), j.get("location"), j.get("source"),
                 ",".join(j.get("sources") or []), j.get("status"), j.get("salary"), j.get("posted"),
                 j.get("first_seen"), j.get("last_seen"), j.get("url"), (j.get("match") or {}).get("score"),
                 len(j.get("description") or ""), json.dumps({x: y for x, y in j.items() if x != "description"})))
    if touched(lambda c: c.endswith("job_search_tracker.csv")):
        conn.execute("DELETE FROM cvt_applications")
        if TRACKER.exists():
            with TRACKER.open(newline="", encoding="utf-8") as f:
                for i, r in enumerate(csv.DictReader(f), 1):
                    conn.execute(
                        "INSERT INTO cvt_applications VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (i, *[r.get(c, "") for c in ("date", "company", "sector", "role", "role_type", "channel",
                                                       "status", "contact_person", "fit_rating", "notes",
                                                       "cv_file", "cover_letter_file", "source", "deadline")]))
    if touched(lambda c: c.endswith("/outcome.json")):
        conn.execute("DELETE FROM cvt_application_events")
        apps = DOCS / "applications"
        for f in apps.glob("*/outcome.json") if apps.exists() else []:
            try:
                o = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for i, e in enumerate(o.get("events", []), 1):
                conn.execute("INSERT INTO cvt_application_events VALUES (%s,%s,%s,%s,%s)",
                             (f.parent.name, i, e.get("date"), e.get("status"), e.get("note")))
    if touched(lambda c: c.startswith("documents/cv/")):
        try:
            import factbank
            from store import MASTER_DIR
            txt = MASTER_DIR / "master_cv.txt"
            bank = factbank.build(txt.read_text(encoding="utf-8")) if txt.exists() else []
        except Exception:  # noqa: BLE001 - reporting must never break syncing
            bank = None
        if bank is not None:
            conn.execute("DELETE FROM cvt_facts")
            for f in bank:
                conn.execute("INSERT INTO cvt_facts VALUES (%s,%s,%s)", (f["id"], f["section"], f["text"]))


def sync_once(full_reporting: bool = False) -> dict:
    with _lock:
        if not url():
            _state.update(enabled=False, connected=False)
            return status()
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                if _state["last_sync"] is None:   # first pass in this process: restore before pushing
                    _state["restored"] += restore(conn)
                    conn.commit()
                pushed, deleted, changed = push(conn)
                if full_reporting or changed:
                    refresh_reporting(conn, None if full_reporting or _state["last_sync"] is None else changed)
                conn.commit()
                n = conn.execute("SELECT count(*), coalesce(sum(size),0) FROM cvt_files").fetchone()
                if not _state["server"]:
                    _state["server"] = "PostgreSQL " + conn.execute("SHOW server_version").fetchone()[0]
            _state.update(enabled=True, connected=True, error="", last_sync=time.time(), files=n[0],
                          bytes=int(n[1]), pushed=_state["pushed"] + pushed, deleted=_state["deleted"] + deleted)
        except Exception as e:  # noqa: BLE001 - keep the app running on files if Postgres is down
            _state.update(enabled=True, connected=False, error=str(e).strip().splitlines()[0][:300])
    return status()


def status() -> dict:
    return {**_state, "configured": bool(url())}


def nudge() -> None:
    """Ask the sync thread to run soon (after the app saves something)."""
    _wake.set()


def _loop():
    while True:
        _wake.wait(INTERVAL)
        _wake.clear()
        time.sleep(0.3)  # let a burst of writes finish
        sync_once()


def start() -> None:
    """Restore and start background sync, if a database is configured."""
    global _thread
    if not url():
        return
    sync_once(full_reporting=True)
    if _thread is None:
        _thread = threading.Thread(target=_loop, daemon=True, name="cvt-db-sync")
        _thread.start()


def reset_session() -> None:
    """After the connection settings change: treat the next pass as a first pass."""
    with _lock:
        _known.clear()
        _state.update(last_sync=None, server="", error="")
