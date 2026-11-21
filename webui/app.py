"""CV Tailor web UI for the ai-job-search repo.

Run:  python webui/app.py      (then open http://127.0.0.1:8765)
Binds to 127.0.0.1 only: your CV never leaves this machine except in the model call.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import ats  # noqa: E402
import atsfix  # noqa: E402
import checks  # noqa: E402
import db  # noqa: E402
import engine  # noqa: E402
import enhancv  # noqa: E402
import extract  # noqa: E402
import jobs as jobsrc  # noqa: E402
import sponsors  # noqa: E402
import factbank  # noqa: E402
import interview  # noqa: E402
import research  # noqa: E402
import linkedin  # noqa: E402
import outcomes  # noqa: E402
import projects  # noqa: E402
import prompts  # noqa: E402
import render  # noqa: E402
import sendpreview  # noqa: E402
import store  # noqa: E402

engine.load_dotenv()
app = FastAPI(title="CV Tailor")
JOBS: dict[str, dict] = {}
_DRAFT_WRITE_LOCK = threading.RLock()
MAX_UPLOAD = 15 * 1024 * 1024
MASTER_TXT = store.MASTER_DIR / "master_cv.txt"
MASTER_META = store.MASTER_DIR / "master_cv.json"


# ----------------------------------------------------------------------------- helpers

async def _read_upload(f: UploadFile | None) -> tuple[str, bytes] | None:
    if f is None or not f.filename:
        return None
    data = await f.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"{f.filename} is over 15 MB")
    return f.filename, data


def _text_from(upload, pasted: str) -> tuple[str, str]:
    if upload:
        name, data = upload
        try:
            return extract.to_text(name, data), name
        except extract.ExtractError as e:
            raise HTTPException(400, f"{name}: {e}") from e
    return (pasted or "").strip(), "pasted text"


def _guard(text: str) -> dict:
    """Run the repo's own posting integrity gate (tools/posting_guard.py) on untrusted text."""
    if not text.strip():
        return {"verdict": "", "text": "", "findings": {}}
    tool = store.REPO / "tools" / "posting_guard.py"
    if not tool.exists():
        return {"verdict": "posting_guard.py not found; text used as-is", "text": text, "findings": {}}
    proc = subprocess.run([sys.executable, str(tool), "--text", "--json"], input=text,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=60)
    out = proc.stdout
    verdict = out.split("\n", 1)[0].strip()
    findings, cleaned = {}, text
    if "--- text ---" in out:
        head, cleaned = out.split("--- text ---", 1)
        cleaned = cleaned.lstrip("\n")
        if "--- findings ---" in head:
            try:
                findings = json.loads(head.split("--- findings ---", 1)[1])
            except ValueError:
                findings = {}
    return {"verdict": verdict, "text": cleaned, "findings": findings}


VERSION_KEEP = 40


def _snapshot(out: Path, note: str) -> None:
    """Keep the draft as it was before this save (version history)."""
    cur = out / "draft.json"
    if not cur.exists():
        return
    vdir = out / "versions"
    vdir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    data = store.load_json(cur, {})
    data["_version_note"] = data.get("_last_note", "")
    store.save_json(vdir / f"{stamp}.json", data)
    for old in sorted(vdir.glob("*.json"))[:-VERSION_KEEP]:
        old.unlink()


def _recent_snapshot(out: Path, seconds: int) -> bool:
    vdir = out / "versions"
    newest = max(vdir.glob("*.json"), default=None, key=lambda p: p.stat().st_mtime) if vdir.exists() else None
    return bool(newest) and time.time() - newest.stat().st_mtime < seconds


def _bank() -> list[dict]:
    if not MASTER_TXT.exists():
        return []
    return factbank.build(MASTER_TXT.read_text(encoding="utf-8"))


def _finish(draft: dict, slug: str, accent: str, note: str = "") -> dict:
    with _DRAFT_WRITE_LOCK:
        return _rebuild_draft(draft, slug, accent, note)


def _rebuild_draft(draft: dict, slug: str, accent: str, note: str) -> dict:
    """Checks, files, save. Shared by a fresh run and by 'save edits'."""
    bank = _bank()
    adir = store.app_dir(slug)
    posting = _posting_text(adir)
    draft = checks.run(draft, bank, posting)
    out = adir / "tailor"
    out.mkdir(parents=True, exist_ok=True)
    if not (note == "Autosaved" and _recent_snapshot(out, 120)):
        _snapshot(out, note)
    draft["_files"] = render.write_all(draft, slug, out, accent)
    for f in draft["_files"]:
        f["download_name"] = _download_name(draft, f["name"])
    try:  # the dedicated ATS check, run on the Word file you would actually send
        docx_path = render.export_path(out, slug, "docx")
        text, risks, _ = ats.read_cv(_download_name(draft, docx_path.name), docx_path.read_bytes())
        rep = ats.analyse(text, risks, posting, jobsrc.COMMON_TERMS, "\n".join(f["text"] for f in bank),
                          (draft.get("target") or {}).get("role", ""))
        draft["_ats"] = {"parse": rep["parse_score"], "keywords": (rep.get("keywords") or {}).get("score"),
                         "match": rep.get("match_score")}
    except Exception as e:  # never block a save on the extra check
        draft["_ats"] = {"error": str(e)[:200]}
    draft["_slug"] = slug
    draft["_accent"] = accent
    draft["_last_note"] = note or "Saved edits"
    draft["_saved_at"] = time.time()
    store.save_json(out / "draft.json", draft)
    return draft


def _posting_text(adir: Path) -> str:
    return "\n".join((adir / name).read_text(encoding="utf-8") for name in
                     ("job_posting.md", "person_specification.md") if (adir / name).exists())


# ----------------------------------------------------------------------------- status

BUILD = str(max((p.stat().st_mtime_ns for p in list(store.WEBUI.glob("*.py")) + list((store.WEBUI / "static").glob("*"))), default=0))


@app.get("/api/build")
def build():
    """Which version of the app is running, and whether a tailoring job is in progress (so updates wait)."""
    busy = any(j.get("status") == "running" for j in JOBS.values())
    return {"build": BUILD, "busy": busy}


@app.get("/api/status")
def status():
    meta = store.load_json(MASTER_META, {})
    return {
        "engine": engine.available(),
        "profile_setup": factbank.profile_is_setup(),
        "master": meta or None,
        "lualatex": bool(shutil.which("lualatex")),
        "enhancv": enhancv.enabled(),
        "repo": str(store.REPO),
    }


@app.get("/api/engines")
def engines_list():
    return engine.available()


@app.put("/api/engines")
def engines_save(payload: dict):
    try:
        order = payload.get("order", [])
        if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
            raise ValueError("Fallback order must be a list of provider IDs.")
        return engine.save_config(str(payload.get("primary", "auto")), order)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/engines/{name}/test")
def engines_test(name: str):
    if name not in engine.LABELS:
        raise HTTPException(404, "unknown engine")
    r = engine.test(name)
    if not r["ok"] and name == "claude" and ("auth" in r["message"].lower() or "login" in r["message"].lower()):
        r["message"] += "  Fix: open a terminal, run `claude`, and log in when the browser opens."
    return r


@app.post("/api/engines/test-routing")
def engines_test_routing():
    return engine.test()


@app.post("/api/engines/keys")
def engines_keys(payload: dict):
    keys = {provider: str(payload.get(f"{provider.upper()}_API_KEY") or "").strip()
            for provider in ("deepseek", "openai", "anthropic")}
    for provider, value in keys.items():
        if value and not re.fullmatch(r"[A-Za-z0-9_.-]{4,500}", value):
            raise HTTPException(400, f"{engine.LABELS[provider]} key has an invalid format.")
    for provider, value in keys.items():
        if value:
            jobsrc.save_env(f"{provider.upper()}_API_KEY", value)
            engine.reset_health(provider)
    return engine.available()


@app.post("/api/engine/test")
def engine_test():
    """Test the engine that will be used first."""
    first = engine.selected()
    if first == "none":
        return {"ok": False, "engine": "none", "message": "No engine is set up yet. Open the engine menu."}
    return engines_test(first)


# ----------------------------------------------------------------------------- master CV

@app.post("/api/master")
async def upload_master(file: UploadFile | None = File(None), text: str = Form("")):
    up = await _read_upload(file)
    content, source = _text_from(up, text)
    if len(content) < 200:
        raise HTTPException(400, "That CV looks too short to tailor from. Upload the full master CV.")
    store.MASTER_DIR.mkdir(parents=True, exist_ok=True)
    if up:
        ext = Path(up[0]).suffix.lower()
        for old in store.MASTER_DIR.glob("master_cv.*"):
            if old.suffix in (".pdf", ".docx", ".tex", ".md") and old.suffix != ext:
                old.unlink()
        (store.MASTER_DIR / f"master_cv{ext}").write_bytes(up[1])
    MASTER_TXT.write_text(content, encoding="utf-8")
    meta = {"source": source, "chars": len(content), "updated": time.time()}
    store.save_json(MASTER_META, meta)
    bank = factbank.build(content)
    return {"master": meta, "facts": bank}


@app.get("/api/master")
def get_master():
    kind = next((x for x in ("docx", "pdf") if (store.MASTER_DIR / f"master_cv.{x}").exists()), None)
    return {"master": store.load_json(MASTER_META), "facts": _bank(), "master_file": kind}


@app.post("/api/facts")
def add_fact(payload: dict):
    try:
        with _DRAFT_WRITE_LOCK:
            factbank.add_extra_fact(str(payload.get("text", "")))
            return {"facts": _bank()}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/facts/{fid}")
def remove_fact(fid: str):
    """Remove a fact you added in the app (X…). Facts from your master CV file change by editing that file."""
    m = re.fullmatch(r"X(\d{3,5})", fid)
    if not m:
        raise HTTPException(400, "Only facts added in the app (X…) can be removed here. To change a fact from your master CV, edit and re-upload it.")
    try:
        with _DRAFT_WRITE_LOCK:
            text = factbank.remove_extra_fact(int(m.group(1)))
    except ValueError as e:
        raise HTTPException(404, f"{fid}: {e}") from e
    except OSError as e:
        raise HTTPException(404, f"{fid} not found") from e
    return {"removed": fid, "text": text, "facts": _bank()}


# ----------------------------------------------------------------------------- projects import

def _imported_projects() -> set[str]:
    return {re.match(r"Project: (.+?)(?: \(|:)", f["text"]).group(1).lower()
            for f in factbank._extra_facts() if re.match(r"Project: (.+?)(?: \(|:)", f["text"])}


@app.get("/api/projects/github")
def projects_github(user: str):
    try:
        owner, repo = projects.owner_from(user)
        repos = projects.list_repos(owner)
    except projects.ImportError_ as e:
        raise HTTPException(400, str(e)) from e
    done = _imported_projects()
    facts_text = " ".join(f["text"] for f in factbank._extra_facts()).lower()
    for r in repos:
        r["imported"] = r["name"].lower() in done or r["url"].lower().replace("https://", "") in facts_text
        r["picked"] = bool(repo) and r["name"].lower() == repo.lower()
    return {"owner": owner, "repos": repos}


def _draft_projects(job_id: str, repos: list[str], text: str):
    job = JOBS[job_id]
    out, errors = [], []
    try:
        steps = len(repos) + (1 if text else 0)
        for i, full in enumerate(repos):
            job.update(stage=f"Reading {full} ({i + 1} of {steps})", progress=5 + int(90 * i / max(steps, 1)))
            try:
                ev = projects.repo_evidence(full)
                drafted, _ = projects.draft(ev["text"], {"name": ev["name"], "url": ev["url"], "dates": ev["dates"]})
                for p in drafted:
                    p["source"] = full
                out += drafted
            except (projects.ImportError_, engine.EngineError) as e:
                errors.append(f"{full}: {e}")
        if text:
            job.update(stage="Reading the text you pasted", progress=90)
            try:
                drafted, _ = projects.draft(text)
                for p in drafted:
                    p["source"] = "pasted text"
                out += drafted
            except engine.EngineError as e:
                errors.append(f"Pasted text: {e}")
        job.update(status="done", progress=100, stage="Done", result={"projects": out, "errors": errors})
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=str(e))


@app.post("/api/projects/draft")
def projects_draft(payload: dict):
    repos = [str(r) for r in (payload.get("repos") or []) if re.fullmatch(r"[\w.-]+/[\w.-]+", str(r))][:15]
    text = str(payload.get("text", "")).strip()[:30000]
    if not repos and len(text) < 40:
        raise HTTPException(400, "Pick at least one repo, or paste some text about your projects.")
    if engine.selected() == "none":
        raise HTTPException(400, "No AI engine is set up to draft the facts.")
    return {"job_id": _start(_draft_projects, repos, text)}


@app.post("/api/projects/save")
def projects_save(payload: dict):
    """Save the facts the candidate confirmed (and edited)."""
    n = 0
    for p in payload.get("projects") or []:
        for text in p.get("facts") or []:
            text = str(text).strip()
            if text:
                factbank.add_extra_fact(projects.fact_line(p, text))
                n += 1
    if not n:
        raise HTTPException(400, "Tick at least one fact to save.")
    return {"saved": n, "facts": _bank()}


# ----------------------------------------------------------------------------- tailoring

def _run_job(job_id: str, jd: str, ps: str, opts: dict):
    job = JOBS[job_id]
    try:
        job.update(stage="Checking the documents for hidden text", progress=10)
        g_jd, g_ps = _guard(jd), _guard(ps)
        bank = _bank()
        if not bank:
            raise RuntimeError("Upload your master CV first.")
        job.update(stage="Mapping requirements and tailoring your CV", progress=30)
        system = prompts.system_prompt(opts.get("statement", False),
                                       prompts.TONES.get(opts.get("tone"), prompts.TONES["balanced"]),
                                       opts.get("cover", False))
        user = prompts.user_prompt(factbank.as_prompt(bank), g_jd["text"], g_ps["text"],
                                   opts.get("notes", ""))
        job["attempts"] = []

        def show_attempt(name):
            label = engine.LABELS[name]
            stage = f"Switching to {label}" if job["attempts"] else f"Tailoring with {label}"
            job.update(stage=stage, progress=35)

        def record_failure(name, reason, elapsed):
            job["attempts"] = [*job["attempts"], {"provider": engine.LABELS[name],
                                                "reason": reason[:200], "seconds": elapsed}]

        draft, used, failed = engine.complete_json(
            system, user, expected={"target": dict, "cv": dict, "requirements": list},
            on_try=show_attempt, on_failure=record_failure, prefer_drafts=True, mode="tailor")
        job.update(stage="Checking every line against your master CV", progress=80)
        draft["_engine"] = {"used": used, "label": engine.LABELS.get(used, used), "fallbacks": failed}
        t = draft.get("target", {})
        slug = store.app_slug(t.get("company") or "company", t.get("role") or "role")
        adir = store.app_dir(slug)
        adir.mkdir(parents=True, exist_ok=True)
        # Archive like /apply Step 6b: cleaned text, counts only, never overwrite an existing copy.
        for fname, g, label in (("job_posting.md", g_jd, "Job description"),
                                ("person_specification.md", g_ps, "Person specification")):
            if g["text"] and not (adir / fname).exists():
                counts = {k: len(v) if isinstance(v, list) else v
                          for k, v in (g.get("findings") or {}).items()
                          if k not in ("verdict",) and isinstance(v, (list, int))}
                (adir / fname).write_text(
                    f"# {label}\n\n{g['text'].strip()}\n\n## posting_guard\n\n{g['verdict']}\n\n"
                    + "\n".join(f"- {k}: {v}" for k, v in counts.items()) + "\n", encoding="utf-8")
        draft["_guard"] = {"jd": g_jd["verdict"], "ps": g_ps["verdict"],
                           "jd_findings": g_jd.get("findings", {}), "ps_findings": g_ps.get("findings", {})}
        draft["_inputs"] = {"jd_source": opts.get("jd_source"), "ps_source": opts.get("ps_source"),
                            "source_url": opts.get("source_url", ""),
                            "apply_url": opts.get("apply_url") or opts.get("source_url", ""),
                            "job_key": opts.get("job_key", "")}
        with _DRAFT_WRITE_LOCK:
            draft = _finish(draft, slug, opts.get("accent", "#1f4e79"), f"Tailored by {engine.LABELS[used]}")
            engine.record_draft_success(used)
            note = ""
            if opts.get("track", True):
                note = store.record_in_tracker(
                    t.get("company", ""), t.get("role", ""), draft["_scores"]["match"] or 0,
                    f"cv/main_{slug}.tex", deadline=t.get("deadline", "") if re.fullmatch(
                        r"\d{4}-\d\d-\d\d", t.get("deadline", "") or "") else "",
                    source=opts.get("source_url", ""))
            draft["_tracker"] = note
            store.save_json(store.app_dir(slug) / "tailor" / "draft.json", draft)
        if opts.get("job_key"):
            st = jobsrc.load_store()
            if opts["job_key"] in st["jobs"]:
                st["jobs"][opts["job_key"]].update(status="tailored", slug=slug)
                jobsrc.save_store(st)
        job.update(stage="Done", progress=100, status="done", slug=slug)
    except Exception as e:  # surfaced to the UI
        traceback.print_exc()
        job.update(status="error", error=str(e) or e.__class__.__name__)


@app.post("/api/tailor")
async def tailor(jd_file: UploadFile | None = File(None), jd_text: str = Form(""),
                 ps_file: UploadFile | None = File(None), ps_text: str = Form(""),
                 options: str = Form("{}")):
    jd, jd_src = _text_from(await _read_upload(jd_file), jd_text)
    ps, ps_src = _text_from(await _read_upload(ps_file), ps_text)
    if len(jd) + len(ps) < 150:
        raise HTTPException(400, "Add a job description or person specification (at least a few lines).")
    if not MASTER_TXT.exists():
        raise HTTPException(400, "Upload your master CV first.")
    if engine.selected() == "none":
        raise HTTPException(400, "No AI engine is set up. Open the engine menu (top right) to connect "
                                 "Claude Code, ChatGPT (Codex), Grok or DeepSeek.")
    try:
        opts = json.loads(options or "{}")
    except ValueError:
        opts = {}
    opts.update(jd_source=jd_src, ps_source=ps_src)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "stage": "Starting", "progress": 2, "started": time.time()}
    threading.Thread(target=_run_job, args=(job_id, jd, ps, opts), daemon=True).start()
    return {"job": job_id}


@app.get("/api/tasks/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return {**job, "elapsed": round(time.time() - job["started"])}


# ----------------------------------------------------------------------------- drafts

def _draft_path(slug: str) -> Path:
    try:
        return store.app_dir(slug) / "tailor" / "draft.json"
    except ValueError as e:
        raise HTTPException(400, "bad slug") from e


@app.get("/api/drafts")
def drafts():
    return store.history()


def _rescore_if_old(slug: str, d: dict) -> dict:
    """A CV checked by older rules is rescored (and its files rebuilt) so every CV reflects the latest checks."""
    if d.get("_checks_version") == checks.CHECKS_VERSION or not d.get("cv") or not MASTER_TXT.exists():
        return d
    try:
        return _finish(d, slug, d.get("_accent", "#1f4e79"), "Rescored with the latest checks")
    except Exception as e:  # noqa: BLE001 - an old CV that can't be rescored still opens
        print(f"  Couldn't rescore {slug}: {e}")
        return d


@app.get("/api/drafts/{slug}")
def get_draft(slug: str):
    d = store.load_json(_draft_path(slug))
    if not d:
        raise HTTPException(404, "not found")
    d = _rescore_if_old(slug, d)
    for f in d.get("_files") or []:
        f["download_name"] = _download_name(d, f["name"])
    inp = d.get("_inputs") or {}
    if inp.get("apply_url"):  # older Work Hub CVs saved the Apply link without the site address
        inp["apply_url"] = jobsrc.absolute_url(inp["apply_url"], inp.get("source_url", ""))
    t = d.get("target") or {}
    d["_previous"] = [p for p in store.previous_for(t.get("company", ""), t.get("role", ""), store.previous_index())
                      if p["kind"] != "drafted"]
    return d


@app.put("/api/drafts/{slug}")
def save_draft(slug: str, payload: dict):
    """Save edits from the editor: re-check, re-render every file."""
    with _DRAFT_WRITE_LOCK:
        return _save_draft(slug, payload)


@app.post("/api/drafts/{slug}/writing-feedback")
def writing_feedback(slug: str, payload: dict):
    """Preview local check results for one unsaved field without changing the draft."""
    draft = store.load_json(_draft_path(slug))
    if not draft:
        raise HTTPException(404, "not found")
    cv, path = payload.get("cv"), payload.get("path")
    if not isinstance(cv, dict) or not isinstance(path, str):
        raise HTTPException(400, "Send the current CV and an editable field.")
    draft["cv"] = cv
    scored = checks.run(draft, _bank(), _posting_text(store.app_dir(slug)))
    return {"notes": list(dict.fromkeys(
        item["note"] for check in scored["_checks"] for item in check["items"]
        if item["path"] == path and item.get("note")
    ))[:4]}


def _save_draft(slug: str, payload: dict) -> dict:
    current = store.load_json(_draft_path(slug))
    if not current:
        raise HTTPException(404, "not found")
    if "cv" in payload:
        current["cv"] = payload["cv"]
    if "statement" in payload:
        current["statement"] = payload["statement"]
    if "cover_letter" in payload:
        current["cover_letter"] = payload["cover_letter"]
    if "dismissed_keywords" in payload:
        current["dismissed_keywords"] = [str(k) for k in payload["dismissed_keywords"]][:200]
    if isinstance(payload.get("review"), dict):  # issues you marked Done or Ignore in the review
        current["review"] = {str(k)[:400]: v for k, v in list(payload["review"].items())[:500]
                             if v in ("done", "ignored")}
    if payload.get("template") in render.TEMPLATES:
        current["_template"] = payload["template"]
    if "design" in payload:  # your own font, size, spacing and margins (None = the template's own)
        current["_design"] = render.clean_design(payload["design"])
    accent = payload.get("accent") or current.get("_accent", "#1f4e79")
    return _finish(current, slug, accent, str(payload.get("note") or "Saved edits")[:80])


# ----------------------------------------------------------------------------- version history

def _vdir(slug: str) -> Path:
    return store.app_dir(slug) / "tailor" / "versions"


@app.get("/api/drafts/{slug}/versions")
def versions(slug: str):
    out = []
    for f in sorted(_vdir(slug).glob("*.json"), reverse=True) if _vdir(slug).exists() else []:
        d = store.load_json(f, {})
        s = d.get("_scores", {})
        out.append({"id": f.stem, "saved_at": d.get("_saved_at") or f.stat().st_mtime,
                    "note": d.get("_last_note", ""), "match": s.get("match"),
                    "quality": s.get("quality"), "keywords": s.get("keywords"), "words": s.get("words")})
    return out


@app.get("/api/drafts/{slug}/versions/{vid}")
def get_version(slug: str, vid: str):
    if not re.fullmatch(r"\d{8}-\d{6}(?:-\d{6})?", vid):
        raise HTTPException(400, "bad version id")
    d = store.load_json(_vdir(slug) / f"{vid}.json")
    if not d:
        raise HTTPException(404, "not found")
    return d


@app.post("/api/drafts/{slug}/versions/{vid}/restore")
def restore_version(slug: str, vid: str):
    with _DRAFT_WRITE_LOCK:
        return _restore_version(slug, vid)


def _restore_version(slug: str, vid: str) -> dict:
    old = get_version(slug, vid)
    current = store.load_json(_draft_path(slug))
    if not current:
        raise HTTPException(404, "not found")
    for k in ("cv", "statement", "cover_letter", "dismissed_keywords"):
        if k in old:
            current[k] = old[k]
    stamp = time.strftime("%d %b %H:%M", time.localtime(old.get("_saved_at") or time.time()))
    return _finish(current, slug, old.get("_accent") or current.get("_accent", "#1f4e79"),
                   f"Restored version from {stamp}")


# ----------------------------------------------------------------------------- assistant

LINE_PATH = re.compile(r"^(summary\.\d+|achievements\.\d+|experience\.\d+\.bullets\.\d+|projects\.\d+\.bullets\.\d+|"
                       r"certifications\.\d+|extras\.\d+\.items\.\d+)$")


def _line(cv: dict, path: str):
    try:
        node = cv
        for k in path.split("."):
            node = node[int(k)] if k.isdigit() else node[k]
        return node if isinstance(node, dict) and "text" in node else None
    except (KeyError, IndexError, TypeError):
        return None


def _mock_assist(cv: dict, focus: dict | None, message: str) -> str:
    path = (focus or {}).get("path")
    line = _line(cv, path) if path else None
    edits = []
    if line and "improve" in message.lower():
        edits.append({"path": path, "text": line["text"].rstrip(".") + ", which recruiters can verify.",
                      "facts": line.get("facts", []), "why": "mock edit for UI testing"})
    return json.dumps({"reply": "Mock assistant: here is a suggestion for that line." if edits else
                       "Mock assistant: the rest reads cleanly to a recruiter.",
                       "edits": edits, "suggestions": ["Make the summary shorter",
                                                       "Which essential criterion is weakest?"]})


@app.post("/api/drafts/{slug}/requirements/{rid}")
def requirement_answer(slug: str, rid: str, payload: dict):
    """Confirm evidence, optionally add it to the CV, or set the criterion aside."""
    with _DRAFT_WRITE_LOCK:
        return _update_requirement(slug, rid, payload)


def _update_requirement(slug: str, rid: str, payload: dict) -> dict:
    current = store.load_json(_draft_path(slug))
    if not current:
        raise HTTPException(404, "not found")
    r = next((x for x in current.get("requirements", []) if x.get("id") == rid), None)
    if not r:
        raise HTTPException(404, "no such requirement")
    action = payload.get("action")
    if action == "have":
        text = re.sub(r"\s+", " ", str(payload.get("text", ""))).strip()
        if len(text) < 12:
            raise HTTPException(400, "Say in one line how you've done this, so it holds up at interview.")
        text = text[:400]
        path = payload.get("cv_path")
        if path is not None:
            cv = current["cv"]
            match = re.fullmatch(r"experience\.(\d{1,5})\.bullets", str(path))
            if path == "summary":
                section, lines = "summary", cv.setdefault("summary", [])
            elif match and int(match[1]) < len(cv.get("experience", [])):
                section, lines = "experience", cv["experience"][int(match[1])].setdefault("bullets", [])
            else:
                raise HTTPException(400, "Choose the profile or an existing role.")
        fid = next((f["id"] for f in _bank() if f["text"] == text), None)
        if fid is None:
            factbank.add_extra_fact(text)
            fid = _bank()[-1]["id"]
        r.update(covered="yes", fact_ids=list(dict.fromkeys([*(r.get("fact_ids") or []), fid])),
                 confirmed=True, acknowledged=False,
                 evidence=f"You confirmed: {text[:200]}")
        if path is not None:
            existing = next((line for line in lines if line.get("text") == text), None)
            if existing is None:
                lines.append({"text": text, "facts": [fid]})
            else:
                existing["facts"] = list(dict.fromkeys([*(existing.get("facts") or []), fid]))
            cv["hidden_sections"] = [name for name in (cv.get("hidden_sections") or []) if name != section]
        note = f"Added {rid} to CV" if path is not None else f"Confirmed {rid} with fact {fid}"
    elif action == "dont":
        r["acknowledged"] = True
        note = f"Set {rid} aside for interview prep"
    elif action == "undo":
        r["acknowledged"] = False
        note = f"Reopened {rid}"
    else:
        raise HTTPException(400, "action must be have, dont or undo")
    return _finish(current, slug, current.get("_accent", "#1f4e79"), note)


@app.post("/api/drafts/{slug}/assist")
def assist(slug: str, payload: dict):
    draft = store.load_json(_draft_path(slug))
    if not draft:
        raise HTTPException(404, "not found")
    if engine.selected() == "none":
        raise HTTPException(400, "Connect an AI provider first.")
    cv = payload.get("cv") or draft["cv"]          # include unsaved edits
    message = str(payload.get("message", "")).strip()[:4000]  # room for "Fix issues", which lists every item
    if not message:
        raise HTTPException(400, "empty message")
    focus = payload.get("focus") if isinstance(payload.get("focus"), dict) else None
    history = [m for m in payload.get("history", []) if isinstance(m, dict)][-6:]
    bank = _bank()
    ids = {f["id"]: f["text"] for f in bank}
    adir = store.app_dir(slug)

    def read(name):
        p = adir / name
        return p.read_text(encoding="utf-8").split("## posting_guard", 1)[0] if p.exists() else ""

    reqs_text = "\n".join(f"{r['id']} [{r.get('status')}, {r.get('covered')}] {r.get('requirement')}"
                          for r in draft.get("requirements", []))
    user = prompts.assist_user(factbank.as_prompt(bank), json.dumps(cv, ensure_ascii=False),
                               reqs_text, read("job_posting.md"), read("person_specification.md"),
                               focus, history, message)
    try:
        out, used, _ = engine.complete_json(prompts.ASSIST_SYSTEM, user,
                                            mock=lambda: _mock_assist(cv, focus, message),
                                            expected={"reply": str, "edits": list}, mode="rewrite")
    except engine.EngineError as e:
        raise HTTPException(502, str(e)) from e
    def check_line(text: str, facts: list[str]) -> list[str]:
        warnings = []
        cited = " ".join(ids[f] for f in facts)
        cited_nums = {checks._num_key(n) for n in checks.NUM.findall(cited)}
        for tok in checks.NUM.findall(text):
            if checks._num_key(tok) not in cited_nums:
                warnings.append(f"'{tok.strip()}' is not in the cited facts")
        if not facts:
            warnings.append("no valid source fact")
        if checks.DASHES.search(text):
            warnings.append("contains an em or en dash")
        return warnings

    edits = []
    for e in out.get("edits", []) or []:
        why = str(e.get("why", ""))[:200]
        if e.get("add") == "project":
            bullets = []
            for b in (e.get("bullets") or [])[:5]:
                facts = [f for f in (b.get("facts") or []) if f in ids]
                text = str(b.get("text", "")).strip()
                if text:
                    bullets.append({"text": text, "facts": facts, "warnings": check_line(text, facts)})
            if bullets and str(e.get("name", "")).strip():
                edits.append({"add": "project", "name": str(e["name"]).strip()[:100],
                              "stack": str(e.get("stack", "")).strip()[:160], "bullets": bullets, "why": why,
                              "warnings": [w for b in bullets for w in b["warnings"]]})
            continue
        if e.get("add") == "bullet":
            path = str(e.get("path", ""))
            m = re.fullmatch(r"(experience|projects)\.(\d+)\.bullets", path)
            if not m or int(m.group(2)) >= len(cv.get(m.group(1)) or []):
                continue
            facts = [f for f in e.get("facts", []) if f in ids]
            text = str(e.get("text", "")).strip()
            if text:
                edits.append({"add": "bullet", "path": path, "text": text, "facts": facts, "why": why,
                              "warnings": check_line(text, facts)})
            continue
        path = str(e.get("path", ""))
        if path == "subtitle":  # the headline: plain text, checked against the titles you've held
            text = re.sub(r"\s+", " ", str(e.get("text", ""))).strip()[:160]
            if text:
                edits.append({"path": "subtitle", "before": cv.get("subtitle", ""), "text": text, "facts": [],
                              "delete": False, "why": why, "warnings": checks.headline_problems(
                                  text, [x.get("title", "") for x in cv.get("experience") or []])})
            continue
        line = _line(cv, path) if LINE_PATH.match(path) else None
        if line is None:
            continue
        facts = [f for f in e.get("facts", []) if f in ids]
        text = str(e.get("text", "")).strip()
        warnings = [] if e.get("delete") else check_line(text, facts)
        edits.append({"path": path, "before": line.get("text", ""), "text": text, "facts": facts,
                      "delete": bool(e.get("delete")), "why": why, "warnings": warnings})
    return {"reply": str(out.get("reply", ""))[:2000], "edits": edits, "engine": engine.LABELS.get(used, used),
            "suggestions": [str(x)[:80] for x in (out.get("suggestions") or [])][:3]}


# ----------------------------------------------------------------------------- re-check match with AI

RECHECK_SYSTEM = """You re-assess how well a candidate's evidence covers each job requirement.
For each requirement, decide "covered" using ONLY the fact bank:
  "yes" = a fact clearly evidences it; "partial" = adjacent evidence only; "listed" = the term appears only
  as a bare skill name; "no" = nothing in the bank. fact_ids must be IDs that exist in the bank.
Be strict: a related tool or a similar-sounding duty is "partial", not "yes". Never invent evidence.
Return JSON only: {"requirements": [{"id": "C1", "covered": "yes", "fact_ids": ["F003"], "why": "one short line"}]}"""


def _recheck_job(job_id: str, slug: str):
    job = JOBS[job_id]
    try:
        d = store.load_json(_draft_path(slug))
        bank = _bank()
        ids = {f["id"] for f in bank}
        reqs = d.get("requirements") or []
        todo = [r for r in reqs if not r.get("confirmed")]  # what you confirmed yourself stays as you set it
        if not todo:
            job.update(status="done", progress=100, stage="Done", result={"changed": []})
            return
        user = ("<requirements>\n" + "\n".join(f"{r['id']} [{r.get('status')}] {r.get('requirement')}" for r in todo)
                + "\n</requirements>\n\n<fact_bank>\n" + factbank.as_prompt(bank) + "\n</fact_bank>")
        job.update(stage="Checking each requirement against your facts", progress=30)
        out, _, _ = engine.complete_json(RECHECK_SYSTEM, user, expected={"requirements": list},
                                         mock=lambda: json.dumps({"requirements": [
                                             {"id": r["id"], "covered": r.get("covered", "no"), "fact_ids": r.get("fact_ids", [])} for r in todo]}))
        with _DRAFT_WRITE_LOCK:
            d = store.load_json(_draft_path(slug))
            by_id = {r["id"]: r for r in d.get("requirements") or []}
            changed = []
            for a in out.get("requirements") or []:
                r = by_id.get(a.get("id"))
                if not r or r.get("confirmed") or a.get("covered") not in ("yes", "partial", "listed", "no"):
                    continue
                facts = [f for f in a.get("fact_ids") or [] if f in ids]
                covered = a["covered"] if (facts or a["covered"] == "no") else "no"  # no evidence, no credit
                if covered != r.get("covered") or sorted(facts) != sorted(r.get("fact_ids") or []):
                    changed.append({"id": r["id"], "requirement": r.get("requirement", ""), "from": r.get("covered"),
                                    "to": covered, "why": str(a.get("why", ""))[:200]})
                    r.update(covered=covered, fact_ids=facts)
            note = f"Re-checked match: {len(changed)} requirement{'s' if len(changed) != 1 else ''} changed"
            _rebuild_draft(d, slug, d.get("_accent", "#1f4e79"), note)
        job.update(status="done", progress=100, stage="Done", result={"changed": changed})
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=str(e))


@app.post("/api/drafts/{slug}/recheck")
def recheck_match(slug: str):
    if not store.load_json(_draft_path(slug)):
        raise HTTPException(404, "not found")
    if engine.selected() == "none":
        raise HTTPException(400, "No AI engine is set up.")
    return {"job_id": _start(_recheck_job, slug)}


# ----------------------------------------------------------------------------- LinkedIn checklist

def _linkedin_inputs():
    bank = _bank()
    text = MASTER_TXT.read_text(encoding="utf-8") if MASTER_TXT.exists() else ""
    terms = jobsrc.bank_terms(factbank.as_prompt(bank) + "\n" + text)
    jobs = list((jobsrc.load_store().get("jobs") or {}).values())
    return bank, text, terms, jobs, jobsrc.get_settings().get("titles") or []


def _clean_profile(payload: dict) -> dict:
    lim = {"headline": 400, "about": 4000, "skills": 4000, "experience": 15000}
    p = {k: str(payload.get(k) or "")[:n] for k, n in lim.items()}
    p["ticked"] = [str(t)[:20] for t in (payload.get("ticked") or [])][:20]
    return p


@app.get("/api/linkedin")
def linkedin_get():
    return linkedin.load()


@app.post("/api/linkedin/check")
def linkedin_check(payload: dict):
    profile = _clean_profile(payload)
    bank, text, terms, jobs, titles = _linkedin_inputs()
    result = linkedin.check(profile, text, terms, jobs, titles)
    linkedin.save(profile, result)
    return result


@app.post("/api/linkedin/suggest")
def linkedin_suggest():
    if engine.selected() == "none":
        raise HTTPException(400, "No AI engine is set up.")
    bank, text, terms, jobs, titles = _linkedin_inputs()
    targets = linkedin._targets(titles, jobs)
    wanted = [d["term"] for d in linkedin.demand(jobs, terms) if d["in_bank"]][:10]
    try:
        return linkedin.suggest(factbank.as_prompt(bank), {f["id"]: f["text"] for f in bank}, targets, wanted)
    except engine.EngineError as e:
        raise HTTPException(502, str(e)) from e


# ----------------------------------------------------------------------------- interview prep

def _posting(slug: str) -> tuple[str, str]:
    adir = store.app_dir(slug)

    def read(name):
        p = adir / name
        return p.read_text(encoding="utf-8").split("## posting_guard", 1)[0] if p.exists() else ""
    return read("job_posting.md"), read("person_specification.md")


@app.get("/api/drafts/{slug}/interview")
def interview_get(slug: str):
    return {"pack": interview.load(slug), "research": research.load(slug)}


def _research_job(job_id: str, slug: str, website: str):
    job = JOBS[job_id]
    try:
        d = store.load_json(_draft_path(slug))
        jd, ps = _posting(slug)
        inp, t = d.get("_inputs") or {}, d.get("target") or {}
        links = [u for u in (inp.get("apply_url", ""), inp.get("source_url", "")) if u]
        job.update(stage="Reading their website and recent news", progress=20)
        out = research.research(t.get("company", ""), website, jd, ps, links)
        store.save_json(research.path(slug), out)
        job.update(status="done", progress=100, stage="Done")
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=str(e))


@app.post("/api/drafts/{slug}/research")
def research_make(slug: str, payload: dict | None = None):
    """Research the employer: their own website and recent news, summarised with a source on every point."""
    if not store.load_json(_draft_path(slug)):
        raise HTTPException(404, "not found")
    if engine.selected() == "none":
        raise HTTPException(400, "No AI engine is set up.")
    website = str((payload or {}).get("website", "")).strip()[:300]
    if website and not re.fullmatch(r"(https?://)?[\w.-]+\.[a-z]{2,}(/[^\s]*)?", website, re.I):
        raise HTTPException(400, "That doesn't look like a web address.")
    return {"job_id": _start(_research_job, slug, website)}


def _interview_job(job_id: str, slug: str, mode: str = "expertise"):
    job = JOBS[job_id]
    try:
        d = store.load_json(_draft_path(slug))
        jd, ps = _posting(slug)
        reqs = "\n".join(f"{r['id']} [{r.get('status')}, {r.get('covered')}] {r.get('requirement')}"
                         for r in d.get("requirements", []))
        t = d.get("target") or {}
        job.update(stage="Writing likely questions and practice exercises", progress=30)
        pack, _ = interview.generate(factbank.as_prompt(_bank()), jd, ps, reqs, t.get("role", ""), t.get("company", ""),
                                     mode, {f["id"]: f["text"] for f in _bank()})
        old = interview.load(slug) or {}
        pack["attempts"] = old.get("attempts", {})
        store.save_json(interview.path(slug), pack)
        job.update(status="done", progress=100, stage="Done")
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=str(e))


@app.post("/api/drafts/{slug}/interview")
def interview_make(slug: str, payload: dict | None = None):
    if not store.load_json(_draft_path(slug)):
        raise HTTPException(404, "not found")
    if engine.selected() == "none":
        raise HTTPException(400, "No AI engine is set up.")
    mode = (payload or {}).get("mode", "expertise")
    return {"job_id": _start(_interview_job, slug, mode if mode in interview.MODES else "expertise")}


@app.post("/api/drafts/{slug}/interview/more")
def interview_more(slug: str):
    """Five more likely questions, added to the pack (none repeat what's there)."""
    d, pack = store.load_json(_draft_path(slug)), interview.load(slug)
    if not d or not pack:
        raise HTTPException(404, "Prepare the interview pack first.")
    jd, ps = _posting(slug)
    reqs = "\n".join(f"{r['id']} [{r.get('status')}, {r.get('covered')}] {r.get('requirement')}" for r in d.get("requirements", []))
    t = d.get("target") or {}
    try:
        new = interview.more_questions(factbank.as_prompt(_bank()), jd, ps, reqs, t.get("role", ""), t.get("company", ""),
                                       pack, {f["id"]: f["text"] for f in _bank()})
    except engine.EngineError as e:
        raise HTTPException(502, str(e)) from e
    pack = interview.load(slug) or pack  # practice answers saved meanwhile are kept
    pack["questions"] = (pack.get("questions") or []) + new
    store.save_json(interview.path(slug), pack)
    return {"pack": pack, "added": len(new)}


def _item(pack: dict, item_id: str) -> dict:
    for it in (pack.get("questions") or []) + (pack.get("technical") or []):
        if it.get("id") == item_id:
            return it
    raise HTTPException(404, "That question isn't in the pack.")


@app.post("/api/drafts/{slug}/interview/run")
def interview_run(slug: str, payload: dict):
    pack = interview.load(slug) or {}
    it = _item(pack, str(payload.get("id", "")))
    if it.get("kind") != "sql" or not it.get("runnable"):
        raise HTTPException(400, "This exercise can't be run.")
    return interview.compare(it["setup_sql"], str(payload.get("query", "")), it["solution_sql"])


@app.post("/api/drafts/{slug}/interview/grade")
def interview_grade(slug: str, payload: dict):
    pack = interview.load(slug)
    if not pack:
        raise HTTPException(404, "Prepare the interview pack first.")
    it = _item(pack, str(payload.get("id", "")))
    answer = str(payload.get("answer", "")).strip()
    if len(answer) < 20:
        raise HTTPException(400, "Write a fuller answer first (a few sentences, or your query).")
    extra = ""
    if it.get("kind") == "sql" and it.get("runnable"):
        c = interview.compare(it["setup_sql"], answer, it["solution_sql"])
        extra = ("correct" if c["correct"] else "not correct") + f": {c['why']}"
    try:
        g = interview.grade(factbank.as_prompt(_bank()), it, answer, extra)
    except engine.EngineError as e:
        raise HTTPException(502, str(e)) from e
    pack.setdefault("attempts", {}).setdefault(it["id"], []).append({"answer": answer, "feedback": g})
    pack["attempts"][it["id"]] = pack["attempts"][it["id"]][-5:]
    store.save_json(interview.path(slug), pack)
    return g


@app.post("/api/drafts/{slug}/compile")
def compile_pdf(slug: str):
    tex = render.export_path(store.app_dir(slug) / "tailor", slug, "tex", existing=True)
    if not tex.exists():
        raise HTTPException(404, "no .tex yet")
    result = render.compile_pdf(tex)
    pdf = tex.with_suffix(".pdf")
    if pdf.exists():
        shutil.copy2(pdf, store.CV_DIR / f"main_{slug}.pdf")
    return result


def _download_name(d: dict, name: str) -> str:
    """What a recruiter or an ATS sees: David_Ibanga_CV_Lovable.docx, not main_lovable_staff_….docx."""
    def part(v: str, cap: int) -> str:
        words = re.findall(r"[A-Za-z0-9]+", v or "")
        return "_".join(w if w.isupper() else w[:1].upper() + w[1:] for w in words)[:cap].strip("_")
    person = part((d.get("cv") or {}).get("name", ""), 40) or "CV"
    company = part(re.sub(r"\b(ltd|limited|plc|inc|llc|group|nhs foundation trust)\b\.?", "", (d.get("target") or {}).get("company", ""), flags=re.I), 30)
    kind = "Cover_Letter" if name.startswith("cover") else "Supporting_Statement" if name.startswith("statement") else "CV"
    stem, ext = name.rsplit(".", 1)
    extra = "_source" if ext in ("tex", "md") else ""
    return f"{person}_{kind}{'_' + company if company else ''}{extra}.{ext}"


@app.get("/api/drafts/{slug}/files/{name}")
def download(slug: str, name: str):
    if not re.fullmatch(r"[a-z0-9_]+\.(docx|tex|md|pdf)", name):
        raise HTTPException(400, "bad file name")
    path = store.app_dir(slug) / "tailor" / name
    if not path.exists():
        raise HTTPException(404, "not found")
    d = store.load_json(_draft_path(slug)) or {}
    return FileResponse(path, filename=_download_name(d, name))


@app.post("/api/drafts/{slug}/send-preview")
def build_send_preview(slug: str, payload: dict):
    fmt = payload.get("format", "word")
    with _DRAFT_WRITE_LOCK:
        out = store.app_dir(slug) / "tailor"
        word = render.export_path(out, slug, "docx", existing=True)
        if not word.is_file():
            raise HTTPException(404, "Save the tailored CV before previewing its exports.")
        pdfs = [render.export_path(out, slug, "pdf", kind=kind, existing=True) for kind in ("main", "enhancv")]
        current_pdfs = {path.name: path for path in pdfs
                        if path.is_file() and path.stat().st_mtime_ns >= word.stat().st_mtime_ns}
        source = word if fmt in ("word", "pdf") else current_pdfs.get(str(fmt))
        if source is None:
            raise HTTPException(409, "That PDF is older than the current CV. Choose PDF from Word to create a fresh export.")
        data = source.read_bytes()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "This export is too large to preview.")
    try:
        report = sendpreview.build(slug, _download_name(store.load_json(_draft_path(slug)) or {}, source.name), data,
                                   convert=fmt == "pdf")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    report["pdfs"] = list(current_pdfs)
    return report


@app.get("/api/drafts/{slug}/send-preview/{token}/{asset}")
def send_preview_asset(slug: str, token: str, asset: str):
    entry = sendpreview.snapshot(slug, token)
    if not entry:
        raise HTTPException(404, "This preview expired. Open Final preview again.")
    headers = {"Cache-Control": "no-store"}
    if asset == "download":
        media = "application/pdf" if entry.name.endswith(".pdf") else (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        headers["Content-Disposition"] = f'attachment; filename="{entry.name}"'
        return Response(entry.file, media_type=media, headers=headers)
    if asset == "layout" and entry.layout:
        return Response(entry.layout, media_type="application/pdf", headers=headers)
    if re.fullmatch(r"page-[1-8]", asset):
        index = int(asset.split("-")[1]) - 1
        if index < len(entry.pages):
            return Response(entry.pages[index], media_type="image/png", headers=headers)
    raise HTTPException(404, "Preview page unavailable.")


@app.get("/api/drafts/{slug}/apply-prompt")
def apply_prompt(slug: str):
    """Hand-off to the full Claude Code /apply pipeline (cover letter, reviewer, PDF loop)."""
    adir = store.app_dir(slug)
    parts = []
    for fname in ("job_posting.md", "person_specification.md"):
        p = adir / fname
        if p.exists():
            parts.append(p.read_text(encoding="utf-8").split("## posting_guard", 1)[0].strip())
    return JSONResponse({"prompt": "/apply " + "\n\n".join(parts)})


# ----------------------------------------------------------------------------- job scanner

def _start(fn, *args) -> str:
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "stage": "Starting", "progress": 2, "started": time.time()}
    threading.Thread(target=fn, args=(job_id, *args), daemon=True).start()
    return job_id


@app.get("/api/jobs/settings")
def jobs_settings():
    return {"settings": jobsrc.get_settings(), "keys": jobsrc.key_status()}


@app.put("/api/jobs/settings")
def jobs_settings_save(payload: dict):
    try:
        return {"settings": jobsrc.save_settings(payload), "keys": jobsrc.key_status()}
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/jobs/keys")
def jobs_keys(payload: dict):
    try:
        return {"keys": jobsrc.save_keys(payload)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _scan_job(job_id: str):
    job = JOBS[job_id]
    try:
        bank_text = "\n".join(f["text"] for f in _bank())
        res = jobsrc.scan(lambda stage, pct: job.update(stage=stage, progress=pct), bank_text)
        job.update(status="done", progress=100, stage="Done", result=res)
    except Exception as e:
        traceback.print_exc()
        job.update(status="error", error=str(e))


@app.post("/api/jobs/scan")
def jobs_scan():
    s = jobsrc.get_settings()
    if not any(s["sources"].values()):
        raise HTTPException(400, "Turn on at least one source.")
    return {"job": _start(_scan_job)}


@app.get("/api/jobs/yugenox/check")
def yugenox_check(title: str = "Data Engineer"):
    """Connect to Yugenox Jobs, list its tools and run one remote search: confirms the source works."""
    try:
        tools = jobsrc._yugenox().connect()
        rows = jobsrc.yugenox_search(title, None, jobsrc.get_settings())
        return {"ok": True, "tools": {k: list((v.get("properties") or {}).keys()) for k, v in tools.items()},
                "remote_results": len(rows), "sample": rows[:3]}
    except Exception as e:  # noqa: BLE001 - shown to you as the reason
        return {"ok": False, "error": str(e)}


@app.get("/api/jobs")
def jobs_list(status: str = "active"):
    st = jobsrc.load_store()
    peers = jobsrc.peer_index(list(st["jobs"].values()))
    prev = store.previous_index()
    settings = jobsrc.get_settings()
    reg = bool(sponsors.status().get("loaded"))
    try:  # rescore with today's master CV and added facts, so new skills count straight away
        mine = jobsrc.bank_terms("\n".join(f["text"] for f in _bank()))
    except Exception:  # noqa: BLE001 - no master CV yet: keep the scores from the scan
        mine = None
    rows = []
    for j in st["jobs"].values():
        if status == "active" and j.get("status") == "hidden":
            continue
        if status not in ("active", "all") and j.get("status") != status:
            continue
        j = dict(j)
        if mine is not None:
            j["match"] = jobsrc.quick_match(j, mine, settings["titles"])
        if j.get("slug"):
            d = store.load_json(_draft_path(j["slug"])) if re.fullmatch(r"[\w-]+", j["slug"]) else None
            j["cv_match"] = ((d or {}).get("_scores") or {}).get("match")
        x = {"sponsor": sponsors.annotate(j), "work": jobsrc.work_info(j), "ghost": jobsrc.ghost_risk(j, peers),
             "previous": store.previous_for(j.get("company", ""), j.get("title", ""), prev),
             "visa_pay": sponsors.salary_check(j)}
        rows.append({**j, "description": j.get("description", "")[:6000], "flags": jobsrc.flags(j), **x,
                     "fit": jobsrc.fit(j, {**x, "register_loaded": reg}, settings)})
    rows.sort(key=lambda r: (-(r["cv_match"] if r.get("cv_match") is not None else (r.get("match") or {}).get("score", 0)),
                             r.get("age_days") or 999))
    return {"jobs": rows, "last_scan": st.get("last_scan"), "sponsors": sponsors.status()}


# ----------------------------------------------------------------------------- dedicated ATS check

@app.post("/api/ats")
async def ats_check(cv_file: UploadFile | None = File(None), slug: str = Form(""),
                    jd_text: str = Form(""), jd_file: UploadFile | None = File(None),
                    job_key: str = Form(""), title: str = Form(""), session: str = Form(""),
                    use_master: str = Form("")):
    """Check any CV file (or a tailored draft's Word file) against any JD, in code.
    A Word file starts a fix-and-recheck session (v0 = the untouched original)."""
    if cv_file is not None and cv_file.filename:
        data = await cv_file.read()
        name = cv_file.filename
    elif slug:
        p = render.export_path(store.app_dir(slug) / "tailor", slug, "docx", existing=True)
        if not p.exists():
            raise HTTPException(404, "That tailored CV has no Word file yet.")
        data, name = p.read_bytes(), p.name
    elif session:
        try:
            p = atsfix.version_path()
        except FileNotFoundError as e:
            raise HTTPException(400, str(e)) from e
        data, name = p.read_bytes(), p.name
    elif use_master:
        mp = store.MASTER_DIR / "master_cv.docx"
        if not mp.exists():
            mp = store.MASTER_DIR / "master_cv.pdf"
        if not mp.exists():
            raise HTTPException(400, "Your master CV isn't saved as Word or PDF. Upload it in Tailor & apply, or upload a file here.")
        source = re.sub(r"\s*\(ATS version \d+\)$", "", (store.load_json(MASTER_META, {}) or {}).get("source", ""))
        name = source if source.lower().endswith(mp.suffix) else f"Master_CV{mp.suffix}"
        data = mp.read_bytes()
    else:
        raise HTTPException(400, "Upload a CV file or pick a tailored CV.")
    jd = jd_text.strip()
    up = await _read_upload(jd_file)
    if up:
        jd = _text_from(up, "")[0]
    jd, title = _ats_jd(jd, job_key, slug, title)
    if not session and name.lower().endswith(".docx"):
        atsfix.start(name, data)
    if name.lower().endswith(".docx"):  # remember the job, so Apply & recheck scores against it
        sess = atsfix.session()
        sess.update(jd=jd, title=title, job_key=job_key)
        atsfix._save_session(sess)
    return _ats_report(name, data, jd, title)


def _ats_jd(jd: str, job_key: str, slug: str, title: str) -> tuple[str, str]:
    if not jd and job_key:
        st = jobsrc.load_store()
        j = st["jobs"].get(job_key)
        if j:
            desc = j.get("description", "")
            if len(desc) < 600:  # search results often carry no ad text (LinkedIn): fetch the full advert
                try:
                    text, apply_url = jobsrc.full_description(j)
                    if len(text) > len(desc):
                        desc = text
                        st["jobs"][job_key].update(description=text, full_description=True,
                                                   apply_url=apply_url or j.get("apply_url", ""))
                        jobsrc.save_store(st)
                except Exception:  # keep what we have; the report says the ad text is too short
                    pass
            jd, title = f"{j['title']}\n{desc}", title or j["title"]
    if not jd and slug:
        adir = store.app_dir(slug)
        jd = "\n".join((adir / f).read_text(encoding="utf-8").split("## posting_guard", 1)[0]
                        for f in ("job_posting.md", "person_specification.md") if (adir / f).exists())
        d = store.load_json(_draft_path(slug)) or {}
        title = title or (d.get("target") or {}).get("role", "")
    return jd, title


def _ats_report(name: str, data: bytes, jd: str, title: str) -> dict:
    try:
        text, risks, kind = ats.read_cv(name, data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    bank_text = "\n".join(f["text"] for f in _bank())
    rep = ats.analyse(text, risks, jd, jobsrc.COMMON_TERMS, bank_text, title)
    rep.update(file=name, kind=kind, has_jd=bool(jd.strip()) and not rep.get("jd_too_short"))
    if kind == "Word":
        sess = atsfix.session()
        lines = atsfix.skills_lines(data)
        rows = (rep.get("keywords") or {}).get("rows", [])
        rep["fix"] = {
            "session": {k: sess.get(k) for k in ("name", "current", "versions")},
            "lines": lines,
            "blocks": atsfix.blocks(data),
            "suggest": atsfix.suggestions(lines, rows, jd, factbank.skills(), jobsrc.COMMON_TERMS),
            "bank": factbank.skills(),
            "phone": atsfix.profile().get("phone", ""),
            "dash_ranges": rep["parsed"]["unicode_dash_ranges"],
            "font_risk": any(r["item"] == "Unusual fonts" for r in risks),
        }
        atsfix.record_score(sess.get("current", 0), rep["parse_score"], (rep.get("keywords") or {}).get("score"),
                            rep.get("match_score"))
        rep["fix"]["session"] = {k: atsfix.session().get(k) for k in ("name", "current", "versions")}
    return rep


@app.post("/api/ats/apply")
def ats_apply(payload: dict):
    """Apply fixes / skill edits to the current version, save it as a new version, recheck."""
    try:
        src = atsfix.version_path()
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e
    spec = payload.get("spec") or {}
    if spec.get("phone"):
        atsfix.save_profile(phone=spec["phone"].strip())
    new, changes = atsfix.apply(src.read_bytes(), spec)
    if not changes:
        raise HTTPException(400, "Nothing to change: tick a fix or edit a skills line first.")
    sess = atsfix.save_new_version(new, "; ".join(changes)[:300])
    rep = _ats_report(src.name, new, sess.get("jd", ""), sess.get("title", ""))
    rep["changes"] = changes
    return rep


@app.post("/api/ats/use/{v}")
def ats_use(v: int):
    try:
        atsfix.use(v)
        p = atsfix.version_path()
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    sess = atsfix.session()
    return _ats_report(p.name, p.read_bytes(), sess.get("jd", ""), sess.get("title", ""))


@app.get("/api/ats/file/{v}")
def ats_file(v: int):
    try:
        p = atsfix.version_path(v)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return FileResponse(p, filename=p.name,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.post("/api/ats/master/{v}")
def ats_make_master(v: int):
    """Use an improved version as the master CV that tailoring starts from."""
    try:
        p = atsfix.version_path(v)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    content, source = _text_from((p.name, p.read_bytes()), "")
    store.MASTER_DIR.mkdir(parents=True, exist_ok=True)
    for old in store.MASTER_DIR.glob("master_cv.*"):
        if old.suffix in (".pdf", ".docx", ".tex", ".md"):
            old.unlink()
    (store.MASTER_DIR / "master_cv.docx").write_bytes(p.read_bytes())
    MASTER_TXT.write_text(content, encoding="utf-8")
    meta = {"source": f"{p.name} (ATS version {v})", "chars": len(content), "updated": time.time()}
    store.save_json(MASTER_META, meta)
    return {"master": meta, "facts": factbank.build(content)}


@app.get("/api/skills")
def skills_get():
    return {"skills": factbank.skills()}


@app.post("/api/skills")
def skills_post(payload: dict):
    if payload.get("remove"):
        factbank.remove_skill(str(payload["remove"]))
    names = payload.get("add") or []
    if isinstance(names, str):
        names = re.split(r"[,;\n]", names)
    return {"skills": factbank.add_skills(names)}


# ----------------------------------------------------------------------------- Enhancv export

def _enhancv_job(job_id: str, slug: str, layout: str):
    job = JOBS[job_id]
    try:
        d = store.load_json(_draft_path(slug))
        job.update(stage="Creating the resume in Enhancv", progress=20)
        rid = enhancv.push(d, layout)
        job.update(stage="Enhancv is rendering the PDF (5-15 seconds)", progress=55)
        data = enhancv.pdf(rid)
        out = render.export_path(store.app_dir(slug) / "tailor", slug, "pdf", kind="enhancv")
        out.write_bytes(data)
        d = store.load_json(_draft_path(slug))
        d["_enhancv"] = {"id": rid, "layout": layout, "at": time.time()}
        d["_files"] = [f for f in d.get("_files", []) if f["name"] != out.name] + [
            {"name": out.name, "kind": "PDF (Enhancv design)"}]
        store.save_json(_draft_path(slug), d)
        job.update(status="done", progress=100, stage="Done", result={"file": out.name, "id": rid})
    except Exception as e:
        job.update(status="error", error=str(e))


@app.post("/api/drafts/{slug}/enhancv")
def to_enhancv(slug: str, payload: dict):
    if not store.load_json(_draft_path(slug)):
        raise HTTPException(404, "not found")
    if not enhancv.enabled():
        raise HTTPException(400, "Add your Enhancv API key first (Business plan only).")
    return {"job": _start(_enhancv_job, slug, str(payload.get("layout") or "single"))}


# ----------------------------------------------------------------------------- sponsor register

def _sponsor_job(job_id: str):
    job = JOBS[job_id]
    try:
        job.update(stage="Downloading the Home Office sponsor register from GOV.UK", progress=20)
        res = sponsors.refresh()
        job.update(status="done", progress=100, stage="Done", result=res)
    except Exception as e:
        job.update(status="error", error=f"{e}. You can download the CSV from GOV.UK "
                                         "('Register of licensed sponsors: workers') and use Upload instead.")


@app.get("/api/sponsors/status")
def sponsors_status():
    return sponsors.status()


@app.post("/api/sponsors/refresh")
def sponsors_refresh():
    return {"job": _start(_sponsor_job)}


@app.post("/api/sponsors/upload")
async def sponsors_upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 60 * 1024 * 1024:
        raise HTTPException(413, "file too large")
    try:
        return sponsors.save_csv(data, f"uploaded {file.filename}")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/jobs/manual")
def jobs_manual(payload: dict):
    try:
        j = jobsrc.add_manual(str(payload.get("title", ""))[:200], str(payload.get("company", ""))[:200],
                              str(payload.get("location", ""))[:200], str(payload.get("url", ""))[:1000],
                              str(payload.get("text", ""))[:60000],
                              "\n".join(f["text"] for f in _bank()))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {**j, "flags": jobsrc.flags(j), "sponsor": sponsors.annotate(j)}


@app.patch("/api/jobs/{key}")
def jobs_update(key: str, payload: dict):
    st = jobsrc.load_store()
    if key not in st["jobs"]:
        raise HTTPException(404, "unknown job")
    if payload.get("status") in ("new", "saved", "hidden", "tailored", "applied"):
        st["jobs"][key]["status"] = payload["status"]
    jobsrc.save_store(st)
    return st["jobs"][key]


def _tailor_from_job(job_id: str, key: str, ps: str, opts: dict):
    job = JOBS[job_id]
    st = jobsrc.load_store()
    j = st["jobs"][key]
    try:
        job.update(stage="Fetching the full job description", progress=6)
        text, apply_url = jobsrc.full_description(j)
    except Exception as e:
        text, apply_url = j.get("description", ""), j.get("apply_url", "")
        if len(text) < 600:
            job.update(status="error", error=f"Couldn't fetch the full advert ({e}). Open it, copy the "
                                               "text and use Tailor > Paste on the left instead.")
            return
    header = f"{j['title']}\n{j['company']} | {j['location']} | {j.get('salary', '')}\n\n"
    opts.update(source_url=j["url"], apply_url=apply_url, job_key=key,
                jd_source=f"{j['source']} listing", ps_source="pasted text" if ps else "")
    st = jobsrc.load_store()
    st["jobs"][key].update(description=text, full_description=True, apply_url=apply_url)
    jobsrc.save_store(st)
    _run_job(job_id, header + text, ps, opts)


@app.post("/api/jobs/{key}/tailor")
def jobs_tailor(key: str, payload: dict):
    st = jobsrc.load_store()
    if key not in st["jobs"]:
        raise HTTPException(404, "unknown job")
    if not MASTER_TXT.exists():
        raise HTTPException(400, "Upload your master CV first.")
    if engine.selected() == "none":
        raise HTTPException(400, "Connect an AI provider first.")
    opts = payload.get("options") or {}
    return {"job": _start(_tailor_from_job, key, str(payload.get("ps_text", ""))[:30000], opts)}


@app.post("/api/drafts/{slug}/applied")
def mark_applied(slug: str):
    d = store.load_json(_draft_path(slug))
    if not d:
        raise HTTPException(404, "not found")
    t = d.get("target", {})
    note = store.mark_applied(t.get("company", ""), t.get("role", ""))
    key = (d.get("_inputs") or {}).get("job_key")
    if key:
        st = jobsrc.load_store()
        if key in st["jobs"]:
            st["jobs"][key]["status"] = "applied"
            jobsrc.save_store(st)
    d["_applied_at"] = time.time()
    store.save_json(_draft_path(slug), d)
    outcomes.record(slug, "applied", snapshot=_sent_snapshot(slug, d))
    return {"note": note}


def _sent_snapshot(slug: str, d: dict) -> dict:
    """What was sent: CV version and scores, and the facts about the job, for outcome analysis."""
    t, s, a, inp = d.get("target") or {}, d.get("_scores") or {}, d.get("_ats") or {}, d.get("_inputs") or {}
    job = jobsrc.load_store()["jobs"].get(inp.get("job_key") or "", {}) if inp.get("job_key") else {}
    vers = sorted(_vdir(slug).glob("*.json")) if _vdir(slug).exists() else []
    cv = d.get("cv") or {}
    work = jobsrc.work_info(job) if job else {}
    sp = sponsors.annotate(job or {"company": t.get("company", ""), "description": "", "title": t.get("role", "")}) or {}
    cover = d.get("cover_letter") or {}
    return {
        "cv_version": vers[-1].stem if vers else "", "title": t.get("role", ""),
        "match": s.get("match"), "quality": s.get("quality"), "keywords": s.get("keywords"), "words": s.get("words"),
        "ats_parse": a.get("parse"), "ats_keywords": a.get("keywords"), "ats_match": a.get("match"),
        "source": (job.get("source") if job else "") or ("pasted advert" if not inp.get("job_key") else ""),
        "location": job.get("location") or t.get("location", ""), "salary": job.get("salary", ""),
        "mode": work.get("mode") or "", "types": work.get("types") or [],
        "sponsor_licensed": (bool((sp.get("licence") or {}).get("skilled_worker")) if sp.get("licence")
                             else (False if sponsors.status().get("loaded") else None)),
        "cover_letter": bool(cover.get("paragraphs")),
        "has_projects": bool(cv.get("projects")) and "projects" not in (cv.get("hidden_sections") or []),
        "template": d.get("_template", ""), "engine": (d.get("_engine") or {}).get("used", ""),
    }


@app.get("/api/outcomes")
def outcomes_list():
    rs = outcomes.rows(store.history(), {})
    return {"rows": rs, "analytics": outcomes.analytics(rs), "statuses": outcomes.STATUSES}


@app.post("/api/outcomes")
def outcomes_set(payload: dict):
    """Record what happened: interview, offer, rejection... (tracker + outcome history)."""
    status = str(payload.get("status", "")).strip()
    if status not in outcomes.STATUSES:
        raise HTTPException(400, "Unknown status.")
    note, when, slug = str(payload.get("note", "")), str(payload.get("date", "")), str(payload.get("slug", ""))
    if slug:
        d = store.load_json(_draft_path(slug))
        if not d:
            raise HTTPException(404, "not found")
        t = d.get("target") or {}
        company, role = t.get("company", ""), t.get("role", "")
        snap = _sent_snapshot(slug, d) if status == "applied" and not outcomes.load(slug).get("snapshot") else None
        outcomes.record(slug, status, note, when, snapshot=snap)
        key = (d.get("_inputs") or {}).get("job_key")
        if key:
            st = jobsrc.load_store()
            if key in st["jobs"] and status != "drafted":
                st["jobs"][key]["status"] = "applied"
                jobsrc.save_store(st)
    else:
        company, role = str(payload.get("company", "")).strip(), str(payload.get("role", "")).strip()
        if not company or not role:
            raise HTTPException(400, "Company and role are needed.")
    msg = store.set_status(company, role, status, note, when)
    return {"note": msg, **outcomes_list()}


# ----------------------------------------------------------------------------- PostgreSQL

def _repair_master_text() -> None:
    """Older versions read Word files without the gaps that tabs make ("LondonApr 2025"). Re-read
    the saved master CV once, but only if the change is spaces alone on the same lines, so every
    fact keeps its number and existing CVs' citations stay valid."""
    src = store.MASTER_DIR / "master_cv.docx"
    if not (src.exists() and MASTER_TXT.exists()):
        return
    try:
        old = MASTER_TXT.read_text(encoding="utf-8")
        new = extract.to_text("master_cv.docx", src.read_bytes())
    except Exception:  # noqa: BLE001 - never block start-up over this
        return
    same_lines = len(old.splitlines()) == len(new.splitlines())
    same_words = re.sub(r"\s", "", old) == re.sub(r"\s", "", new)
    if new != old and same_lines and same_words:
        MASTER_TXT.write_text(new, encoding="utf-8")
        print("  Master CV text re-read: gaps from tabs restored (fact numbers unchanged).")


@app.on_event("startup")
def _repair_on_start():
    _repair_master_text()

    def rescore_all():
        n = 0
        for d in sorted(store.APPS_DIR.iterdir()) if store.APPS_DIR.exists() else []:
            draft = store.load_json(d / "tailor" / "draft.json")
            if draft and draft.get("_checks_version") != checks.CHECKS_VERSION:
                before = _rescore_if_old(d.name, draft)
                n += before.get("_checks_version") == checks.CHECKS_VERSION
        if n:
            print(f"  Rescored {n} CV{'s' if n != 1 else ''} with the latest checks.")
    threading.Thread(target=rescore_all, daemon=True).start()


@app.on_event("startup")
def _db_start():
    try:
        db.start()
    except Exception as e:  # noqa: BLE001 - never stop the app over the database
        print(f"  Database: {e}")


@app.get("/api/db")
def db_status():
    return db.status()


@app.post("/api/db")
def db_connect(payload: dict):
    """Test a connection URL; if it works, save it to webui/.env and start syncing."""
    dsn = str(payload.get("url", "")).strip()
    if payload.get("disconnect"):
        jobsrc.save_env("DATABASE_URL", "")
        db.reset_session()
        return db.status()
    if not re.match(r"^postgres(?:ql)?://", dsn):
        raise HTTPException(400, "Use a URL like postgresql://cvtailor:PASSWORD@localhost:5432/cvtailor")
    try:
        info = db.test(dsn)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Couldn't connect: {str(e).strip().splitlines()[0][:300]}") from e
    jobsrc.save_env("DATABASE_URL", dsn)
    db.reset_session()
    db.start()
    return {**db.status(), "test": info}


@app.post("/api/db/sync")
def db_sync():
    if not db.url():
        raise HTTPException(400, "No database connected.")
    return db.sync_once(full_reporting=True)


@app.middleware("http")
async def _always_fresh_page(request, call_next):
    """The page, script and styles change with every update: make the browser check for a new copy each time,
    or it keeps running an old script after the app restarts."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")


def _code_stamp() -> tuple:
    return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in store.WEBUI.glob("*.py")))


def _busy(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/build", timeout=3) as r:
            return bool(json.loads(r.read()).get("busy"))
    except Exception:  # noqa: BLE001 - not up yet, or already gone
        return False


def _supervise(port: int) -> None:
    """Run the app in a child process and restart it when its code changes (after any tailoring finishes),
    so updates apply without closing this window."""
    while True:
        stamp = _code_stamp()
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve"])
        try:
            while child.poll() is None:
                time.sleep(2)
                if _code_stamp() != stamp:
                    time.sleep(2)  # let a multi-file update finish landing
                    waited = 0
                    while _busy(port) and waited < 900:
                        if waited == 0:
                            print("  Update ready: restarting once the current tailoring finishes...")
                        time.sleep(5); waited += 5
                    print("  Code updated: restarting the app...")
                    child.terminate()
                    try:
                        child.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        child.kill()
                    break
            else:  # the app stopped by itself (a crash, or a bad update): wait for a fix, then retry
                print(f"  The app stopped (code {child.returncode}). Waiting for a code change to restart...")
                while _code_stamp() == stamp:
                    time.sleep(2)
        except KeyboardInterrupt:
            child.terminate()
            return


if __name__ == "__main__":
    port = int(os.environ.get("CV_TAILOR_PORT", "8765"))
    if "--serve" in sys.argv or os.environ.get("CV_TAILOR_NO_RELOAD") == "1":
        import uvicorn
        print(f"\n  CV Tailor running at http://127.0.0.1:{port}\n  Engine: {engine.selected()}\n")
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    else:
        _supervise(port)
