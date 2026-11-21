"""CV Tailor for Claude: an MCP server (stdio) so Claude Code or the Claude desktop app can read and
edit your CVs, facts, jobs and interview packs.

It talks to the CV Tailor app that's already running on this PC (start_webui.bat), so every edit goes
through the same checks, file rebuilds and version history as an edit in the browser. Any edit can be
undone from the History tab. It can't apply for jobs, change an application's status or touch API keys.

Add it to Claude Code:
    claude mcp add cv-tailor -- python "C:\\path\\to\\ai-job-search\\webui\\mcp_server.py"
Or to the Claude desktop app (Settings > Developer > Edit config), under "mcpServers":
    "cv-tailor": {"command": "python", "args": ["C:\\path\\to\\ai-job-search\\webui\\mcp_server.py"]}
Set CV_TAILOR_URL if the app isn't on http://127.0.0.1:8765.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("CV_TAILOR_URL", "http://127.0.0.1:8765").rstrip("/")
if urllib.parse.urlsplit(BASE).hostname not in ("127.0.0.1", "localhost", "::1"):
    BASE = "http://127.0.0.1:8765"  # only ever your own PC
VERSION = "1.0.0"
LINE = re.compile(r"^(subtitle|summary\.\d+|achievements\.\d+|experience\.\d+\.bullets\.\d+|projects\.\d+\.bullets\.\d+|"
                  r"certifications\.\d+|extras\.\d+\.items\.\d+)$")
LIST = re.compile(r"^(summary|achievements|certifications|experience\.\d+\.bullets|projects\.\d+\.bullets|extras\.\d+\.items)$")
SLUG = re.compile(r"^[a-z0-9_]{1,160}$")


class Problem(Exception):
    pass


def call(method: str, path: str, body=None, timeout: int = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:  # noqa: BLE001
            detail = e.reason
        raise Problem(f"CV Tailor said: {detail}") from None
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        raise Problem(f"CV Tailor isn't running at {BASE}. Start it with start_webui.bat, then try again.") from None


def _slug(a) -> str:
    s = str(a.get("slug", ""))
    if not SLUG.fullmatch(s):
        raise Problem("Give the application's slug, as list_applications shows it.")
    return s


def _node(cv: dict, path: str):
    """(parent, key) for a dotted path into the CV."""
    parts = path.split(".")
    cur = cv
    for p in parts[:-1]:
        cur = cur[int(p)] if p.isdigit() else cur.get(p)
        if cur is None:
            raise Problem(f"No line at {path}.")
    last = parts[-1]
    if last.isdigit() and not (isinstance(cur, list) and int(last) < len(cur)):
        raise Problem(f"No line at {path}.")
    return cur, (int(last) if last.isdigit() else last)


def _cv_text(cv: dict) -> str:
    """The CV as plain lines, each with the path you use to edit it and its fact IDs."""
    out = [f"{cv.get('name', '')}", f"[subtitle] {cv.get('subtitle', '')}"]
    hidden = set(cv.get("hidden_sections") or [])
    for key in cv.get("section_order") or []:
        rows = cv.get(key) or []
        if not rows:
            continue
        out.append(f"\n## {key}" + (" (hidden)" if key in hidden else ""))
        if key in ("summary", "achievements", "certifications"):
            out += [f"[{key}.{i}] {r.get('text', '')}  {r.get('facts', [])}" for i, r in enumerate(rows)]
        elif key == "skills":
            out += [f"{r.get('category', '')}: {', '.join(r.get('items', []))}" for r in rows]
        elif key in ("experience", "projects"):
            for i, r in enumerate(rows):
                out.append(f"### {r.get('title') or r.get('name', '')} | {r.get('company') or r.get('stack', '')} | {r.get('dates', '')}")
                out += [f"[{key}.{i}.bullets.{j}] {b.get('text', '')}  {b.get('facts', [])}" for j, b in enumerate(r.get("bullets") or [])]
        elif key == "education":
            out += [f"{r.get('qualification', '')} | {r.get('institution', '')} | {r.get('dates', '')}" for r in rows]
        elif key == "extras":
            for i, x in enumerate(rows):
                out.append(f"### {x.get('heading', '')}")
                out += [f"[extras.{i}.items.{j}] {it.get('text', '')}  {it.get('facts', [])}" for j, it in enumerate(x.get("items") or [])]
    return "\n".join(out)


def _scores(d: dict) -> str:
    s = d.get("_scores") or {}
    return f"Match {s.get('match', '?')}% · Review {s.get('quality', '?')}% · Keywords {s.get('keywords', '?')}%"


def _issues(d: dict, only_path: str | None = None) -> list[str]:
    out = []
    for c in d.get("_checks") or []:
        if c.get("status") == "pass":
            continue
        for it in c.get("items") or []:
            if only_path and it.get("path") != only_path:
                continue
            out.append(f"- [{c.get('status')}] {c.get('label')}: {it.get('path', '')} {it.get('note', '')}".rstrip())
        if not c.get("items") and not only_path:
            out.append(f"- [{c.get('status')}] {c.get('label')}: {c.get('detail', '')}")
    return out


def _fact_ids() -> set[str]:
    return {f["id"] for f in (call("GET", "/api/master").get("facts") or [])}


def _save(slug: str, d: dict, note: str) -> dict:
    body = {"cv": d["cv"], "statement": d.get("statement"), "cover_letter": d.get("cover_letter"),
            "note": f"From Claude: {note}"[:80]}
    return call("PUT", f"/api/drafts/{slug}", body)


# ----------------------------------------------------------------------------- tools

def t_list_applications(a):
    rows = call("GET", "/api/drafts") or []
    if not rows:
        return "No tailored CVs yet."
    return "\n".join(f"{r['slug']} · {r.get('company', '')} · {r.get('role', '')} · match {r.get('match', '?')}% · review {r.get('quality', '?')}%"
                     for r in rows[:int(a.get("limit") or 30)])


def t_get_cv(a):
    d = call("GET", f"/api/drafts/{_slug(a)}")
    t = d.get("target") or {}
    reqs = "\n".join(f"{r.get('id')} [{r.get('status')}, covered: {r.get('covered')}] {r.get('requirement')}"
                     for r in d.get("requirements") or [])
    return (f"{t.get('role', '')} at {t.get('company', '')}\n{_scores(d)}\n\n{_cv_text(d['cv'])}\n\n"
            f"## Requirements from the advert\n{reqs}")


def t_get_review(a):
    d = call("GET", f"/api/drafts/{_slug(a)}")
    issues = _issues(d)
    return f"{_scores(d)}\n" + ("\n".join(issues) if issues else "No open issues.")


def t_list_facts(a):
    facts = call("GET", "/api/master").get("facts") or []
    q = str(a.get("query") or "").lower()
    rows = [f for f in facts if not q or q in f["text"].lower()]
    return "\n".join(f"{f['id']}: {f['text']}" for f in rows[:int(a.get("limit") or 200)]) or "No matching facts."


def _check_facts(facts) -> list[str]:
    facts = [str(f) for f in (facts or [])]
    unknown = sorted(set(facts) - _fact_ids())
    if unknown:
        raise Problem(f"Unknown fact IDs: {', '.join(unknown)}. Use list_facts to find the right ones.")
    return facts


def t_edit_line(a):
    slug, path, text = _slug(a), str(a.get("path", "")), re.sub(r"\s+", " ", str(a.get("text", ""))).strip()
    if not LINE.fullmatch(path):
        raise Problem("path must be a line path such as summary.0, achievements.1, experience.0.bullets.2 or subtitle.")
    if not text or len(text) > 400:
        raise Problem("Give the new text (1 to 400 characters).")
    d = call("GET", f"/api/drafts/{slug}")
    parent, key = _node(d["cv"], path)
    if path == "subtitle":
        parent[key] = text
    else:
        line = parent[key]
        line["text"] = text
        if a.get("facts") is not None:
            line["facts"] = _check_facts(a["facts"])
    d = _save(slug, d, f"edited {path}")
    left = _issues(d, path)
    return f"Saved {path}. {_scores(d)}\n" + ("Still flagged on this line:\n" + "\n".join(left) if left else "No issues on this line.")


def t_add_line(a):
    slug, where, text = _slug(a), str(a.get("list_path", "")), re.sub(r"\s+", " ", str(a.get("text", ""))).strip()
    if not LIST.fullmatch(where):
        raise Problem("list_path must be summary, achievements, certifications, experience.N.bullets, projects.N.bullets or extras.N.items.")
    if not text or len(text) > 400:
        raise Problem("Give the text (1 to 400 characters).")
    facts = _check_facts(a.get("facts"))
    if not facts:
        raise Problem("Every new line needs at least one fact ID it rests on, so nothing is invented. Use list_facts, or add_fact first if the user told you something new.")
    d = call("GET", f"/api/drafts/{slug}")
    parent, key = _node(d["cv"], where)
    lst = parent[key] if isinstance(key, int) else parent.setdefault(key, [])
    pos = a.get("position")
    pos = len(lst) if pos is None else max(0, min(int(pos), len(lst)))
    lst.insert(pos, {"text": text, "facts": facts})
    if where == "achievements":
        d["cv"]["hidden_sections"] = [s for s in d["cv"].get("hidden_sections") or [] if s != "achievements"]
    d = _save(slug, d, f"added to {where}")
    return f"Added as {where}.{pos}. {_scores(d)}"


def t_delete_line(a):
    slug, path = _slug(a), str(a.get("path", ""))
    if not LINE.fullmatch(path) or path == "subtitle":
        raise Problem("path must be a line path such as experience.0.bullets.2.")
    d = call("GET", f"/api/drafts/{slug}")
    parent, key = _node(d["cv"], path)
    gone = parent.pop(key)
    d = _save(slug, d, f"removed {path}")
    return f"Removed: {gone.get('text', '')[:120]}. {_scores(d)} (Undo from the History tab.)"


def t_add_fact(a):
    text = re.sub(r"\s+", " ", str(a.get("text", ""))).strip()
    if len(text) < 12:
        raise Problem("Write the fact in at least 12 characters, in the user's own words.")
    facts = call("POST", "/api/facts", {"text": text[:400]}).get("facts") or []
    new = next((f for f in reversed(facts) if f["text"] == text[:400]), None)
    return f"Saved as {new['id']}: {new['text']}" if new else "Saved."


def t_list_jobs(a):
    r = call("GET", "/api/jobs")
    jobs = r.get("jobs") if isinstance(r, dict) else r
    q = str(a.get("query") or "").lower()
    rows = [j for j in jobs or [] if j.get("status") != "hidden" and (not q or q in f"{j.get('title', '')} {j.get('company', '')} {j.get('location', '')}".lower())]
    rows.sort(key=lambda j: -((j.get("fit") or {}).get("score") or 0))
    return "\n".join(f"{(j.get('fit') or {}).get('score', '?')}/100 · {j.get('title', '')} · {j.get('company', '')} · {j.get('location', '')} · {j.get('salary') or ''} · key {j.get('key')}"
                     for j in rows[:int(a.get("limit") or 20)]) or "No jobs match."


def t_get_interview_pack(a):
    r = call("GET", f"/api/drafts/{_slug(a)}/interview")
    p, res = r.get("pack"), r.get("research")
    if not p and not res:
        return "No interview pack yet. Make one on the Interview tab."
    out = []
    if res and res.get("overview"):
        out.append(f"About the employer: {res['overview']['text']}")
        out += [f"- In the news: {n['title']} ({n.get('publisher', '')}, {n.get('date', '')})" for n in res.get("recent_news") or []]
    for q in (p or {}).get("questions") or []:
        out.append(f"\nQ{q.get('id', '')}: {q.get('question', '')}")
        if q.get("answer"):
            out.append(f"Suggested answer: {q['answer']}")
    return "\n".join(out)


TOOLS = {
    "list_applications": (t_list_applications, "List the tailored CVs (one per job application) with their slugs and match scores.",
                          {"limit": {"type": "integer", "description": "How many to list (default 30)"}}, []),
    "get_cv": (t_get_cv, "Read a tailored CV line by line. Each line shows the path used to edit it and the fact IDs it rests on, plus the advert's requirements.",
               {"slug": {"type": "string"}}, ["slug"]),
    "get_review": (t_get_review, "The open review issues for a tailored CV (honesty, first 10 seconds, ATS, spelling and so on), with the line path for each.",
                   {"slug": {"type": "string"}}, ["slug"]),
    "list_facts": (t_list_facts, "The user's fact bank: everything a CV line is allowed to rest on, with IDs. Search with query.",
                   {"query": {"type": "string"}, "limit": {"type": "integer"}}, []),
    "edit_cv_line": (t_edit_line, "Rewrite one CV line. Keep every number exactly as the facts state it and never add a skill, employer or result the facts don't support. Saves a new version (undo from History) and rechecks the CV.",
                     {"slug": {"type": "string"}, "path": {"type": "string", "description": "e.g. summary.0, achievements.1, experience.0.bullets.2, subtitle"},
                      "text": {"type": "string"}, "facts": {"type": "array", "items": {"type": "string"}, "description": "Fact IDs this line rests on (optional: keeps the current ones)"}},
                     ["slug", "path", "text"]),
    "add_cv_line": (t_add_line, "Add a line to a CV section. It must cite at least one fact ID, so nothing is invented.",
                    {"slug": {"type": "string"}, "list_path": {"type": "string", "description": "summary, achievements, certifications, experience.N.bullets, projects.N.bullets or extras.N.items"},
                     "text": {"type": "string"}, "facts": {"type": "array", "items": {"type": "string"}}, "position": {"type": "integer"}},
                    ["slug", "list_path", "text", "facts"]),
    "delete_cv_line": (t_delete_line, "Remove one CV line (undo from the History tab).",
                       {"slug": {"type": "string"}, "path": {"type": "string"}}, ["slug", "path"]),
    "add_fact": (t_add_fact, "Save something the user has told you about their own experience to their fact bank, in their own words. Only use this for things the user said, never for your own suggestions.",
                 {"text": {"type": "string"}}, ["text"]),
    "list_jobs": (t_list_jobs, "Jobs found by the job scanner, best fit first. Filter with query (title, company or place).",
                  {"query": {"type": "string"}, "limit": {"type": "integer"}}, []),
    "get_interview_pack": (t_get_interview_pack, "The interview pack for an application: company research, likely questions and suggested answers.",
                           {"slug": {"type": "string"}}, ["slug"]),
}

INSTRUCTIONS = ("CV Tailor holds the user's tailored CVs, fact bank and job list. Every CV line must rest on a fact ID; "
                "keep numbers exactly as the facts state them and write in UK English. Read with get_cv and get_review before editing. "
                "Edits are saved as new versions the user can undo. You can't apply for jobs or change application status.")


def handle(msg: dict):
    m, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if m == "initialize":
        return {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "cv-tailor", "version": VERSION}, "instructions": INSTRUCTIONS}
    if m == "ping":
        return {}
    if m == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": props, "required": req}}
                          for n, (_, d, props, req) in TOOLS.items()]}
    if m == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return {"content": [{"type": "text", "text": f"Unknown tool {name}."}], "isError": True}
        try:
            return {"content": [{"type": "text", "text": TOOLS[name][0](args)}]}
        except Problem as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except (KeyError, IndexError, TypeError, ValueError) as e:
            return {"content": [{"type": "text", "text": f"That didn't work: {e}"}], "isError": True}
    if mid is not None:
        raise LookupError(m)
    return None


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), flush=True)
            continue
        mid = msg.get("id")
        try:
            result = handle(msg)
        except LookupError as e:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {e}"}}), flush=True)
            continue
        if mid is not None and result is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}), flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    main()
