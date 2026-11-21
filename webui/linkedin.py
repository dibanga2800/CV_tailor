"""LinkedIn profile checklist.

You paste your headline, About, skills and experience. This checks them against the jobs you're
targeting (the skills that keep coming up in your scanned jobs) and against your own facts, so it
only ever suggests adding things you can back up. Nothing is posted to LinkedIn.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter

import engine
from store import DOCS, load_json, save_json

STATE = DOCS / "postings" / "linkedin.json"  # gitignored, like the job store
HEADLINE_MAX, ABOUT_MAX, ABOUT_FOLD = 220, 2600, 265
NUM = re.compile(r"(?<![\w.])(?:£|\$|€)?\d[\d,.]*(?:%|k|m|x)?(?![\w])", re.I)
BUZZ = re.compile(r"\b(passionate|guru|ninja|rock ?star|wizard|visionary|seeking (new )?opportunities|"
                  r"unemployed|looking for work|open to work|hard[- ]working|team player|results[- ]driven)\b", re.I)
CTA = re.compile(r"\b(get in touch|contact me|message me|reach out|email me|connect with me|happy to (chat|talk)|"
                 r"open to (conversations|new|roles|opportunities)|let'?s (talk|connect|chat)|drop me)\b", re.I)
ROLE_NOUNS = ("engineer", "developer", "analyst", "scientist", "architect", "manager", "lead", "consultant",
              "specialist", "administrator", "officer", "director", "head", "designer", "tester", "owner")

MANUAL = [
    ("photo", "A clear, recent photo", "Profiles with a photo get far more views from recruiters."),
    ("banner", "A banner that says what you do", "A simple background image with your role or a line of proof."),
    ("location", "Location set to where you want to work", "Recruiters filter by location first."),
    ("open", "Open to Work: recruiters only", "Turn on Open to Work with 'Recruiters only' so your employer doesn't see a badge."),
    ("featured", "Featured: one piece of real work", "Pin a project, case study or portfolio link (for example a system you built)."),
    ("recs", "Two or more recent recommendations", "Ask a manager or senior colleague who knows your work."),
]


def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower())


def _has(term: str, text: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text))


def demand(jobs: list[dict], bank_terms: set[str], limit: int = 20) -> list[dict]:
    """The skills that come up most in the jobs you're looking at, and whether your facts back each one."""
    count = Counter()
    for j in jobs:
        if j.get("status") in ("hidden", "rejected"):
            continue
        m = j.get("match") or {}
        count.update(set(m.get("have", []) + m.get("missing", [])))
    return [{"term": t, "jobs": n, "in_bank": t in bank_terms} for t, n in count.most_common(limit)]


def _targets(titles: list[str], jobs: list[dict]) -> list[str]:
    """Role phrases you're aiming for: your search titles, else the commonest job-title nouns you've saved."""
    out = [t for t in titles if t.strip()]
    if not out:
        c = Counter()
        for j in jobs:
            words = re.findall(r"[a-z]+", (j.get("title") or "").lower())
            for i, w in enumerate(words):
                if w in ROLE_NOUNS and i:
                    c[f"{words[i - 1]} {w}"] += 1
        out = [p for p, _ in c.most_common(3)]
    return out


def _role_hit(text: str, targets: list[str]) -> str:
    low = _fold(text)
    for t in targets:
        words = re.findall(r"[a-z]+", t.lower())
        noun = next((w for w in reversed(words) if w in ROLE_NOUNS), words[-1] if words else "")
        phrase = " ".join(words[-2:]) if len(words) > 1 else noun
        if phrase and phrase in low:
            return t
        if noun and _has(noun, low) and any(_has(w, low) for w in words[:-1] if len(w) > 2):
            return t
    return ""


def check(profile: dict, bank_text: str, bank_terms: set[str], jobs: list[dict], titles: list[str]) -> dict:
    head = (profile.get("headline") or "").strip()
    about = (profile.get("about") or "").strip()
    skills_raw = profile.get("skills") or ""
    skills = [s.strip() for s in re.split(r"[\n,;•·|]+", skills_raw) if s.strip()]
    exp = (profile.get("experience") or "").strip()
    ticked = set(profile.get("ticked") or [])
    everything = _fold(" ".join([head, about, " ".join(skills), exp]))
    dem = demand(jobs, bank_terms)
    for d in dem:
        d["on_profile"] = _has(d["term"], everything)
    targets = _targets(titles, jobs)
    wanted = [d for d in dem if d["in_bank"]][:12]  # in demand AND you can back them up
    items = []

    def add(group, iid, title, status, detail="", fix=""):
        items.append({"group": group, "id": iid, "title": title, "status": status, "detail": detail, "fix": fix})

    # Headline: the line shown next to your name in every search result
    if not head:
        add("Headline", "h_len", "Headline written", "fail", "No headline pasted.", "Paste the line under your name.")
    else:
        n = len(head)
        add("Headline", "h_len", "Uses the space well", "fail" if n > HEADLINE_MAX else "warn" if n < 60 else "pass",
            f"{n} of {HEADLINE_MAX} characters.",
            "LinkedIn cuts it at 220 characters." if n > HEADLINE_MAX else "Use more of it: role, two strongest skills, one result." if n < 60 else "")
        hit = _role_hit(head, targets)
        add("Headline", "h_role", "Says the role you want", "pass" if hit or not targets else "fail",
            f"Matches '{hit}'." if hit else f"Doesn't name a role you're searching for ({', '.join(targets[:3])}).",
            "" if hit else "Start with the role you want recruiters to find you for, if your experience backs it up.")
        present = [d["term"] for d in wanted if _has(d["term"], _fold(head))]
        add("Headline", "h_skills", "Carries in-demand skills you have", "pass" if len(present) >= 2 else "warn",
            f"Has {', '.join(present) or 'none'}.",
            "" if len(present) >= 2 else "Add two of: " + ", ".join([d["term"] for d in wanted if d["term"] not in present][:4]))
        generic = bool(re.fullmatch(r"[\w &/.-]{2,60}\s+(?:at|@)\s+[\w &/.-]{2,60}", head, re.I))
        buzz = BUZZ.findall(head)
        add("Headline", "h_plain", "More than 'Title at Company'", "warn" if generic or buzz else "pass",
            ("Just a job title and employer. " if generic else "") + (f"Words recruiters skip: {', '.join(sorted({b[0] for b in buzz}))}." if buzz else ""),
            "Swap filler for proof: a skill, a sector or a result." if generic or buzz else "")

    # About: the first ~265 characters show before 'see more'
    if not about:
        add("About", "a_len", "About section written", "fail", "No About pasted.", "Write 3 to 5 short paragraphs in the first person.")
    else:
        n = len(about)
        add("About", "a_len", "A good length", "fail" if n > ABOUT_MAX else "warn" if n < 600 else "pass",
            f"{n:,} of {ABOUT_MAX:,} characters.",
            "Over LinkedIn's limit." if n > ABOUT_MAX else "Aim for 1,200 to 2,000 characters." if n < 600 else "")
        fold = about[:ABOUT_FOLD]
        hook = bool(_role_hit(fold, targets)) or any(_has(d["term"], _fold(fold)) for d in wanted[:6])
        add("About", "a_hook", "The opening lines sell you", "pass" if hook else "warn",
            "Only about the first 265 characters show before 'see more'.",
            "" if hook else "Open with who you are, the role you do and your strongest result.")
        nums = len(NUM.findall(about))
        add("About", "a_number", "Has results with numbers", "pass" if nums >= 2 else "warn" if nums else "fail",
            f"{nums} number{'s' if nums != 1 else ''} found.", "" if nums >= 2 else "Add one or two results from your facts, exactly as they are.")
        buzz = sorted({b[0].lower() for b in BUZZ.findall(about)})
        add("About", "a_plain", "No filler phrases", "warn" if buzz else "pass", f"Found: {', '.join(buzz)}." if buzz else "",
            "Replace each with something you did and what it achieved." if buzz else "")
        add("About", "a_cta", "Ends with a way to reach you", "pass" if CTA.search(about[-400:]) else "warn", "",
            "" if CTA.search(about[-400:]) else "Finish with one line saying what you're open to and how to get in touch.")

    # Skills
    add("Skills", "s_count", "Enough skills listed", "pass" if len(skills) >= 15 else "warn" if skills else "fail",
        f"{len(skills)} listed (LinkedIn allows up to 100).", "" if len(skills) >= 15 else "List at least 15, the in-demand ones first.")
    missing_have = [d for d in wanted if not d["on_profile"]]
    add("Skills", "s_add", "In-demand skills you have are on the profile", "pass" if not missing_have else "fail" if len(missing_have) > 4 else "warn",
        f"{len(wanted) - len(missing_have)} of {len(wanted)} on your profile." if wanted else "Scan some jobs first so I know what's in demand.",
        ("Add: " + ", ".join(f"{d['term']} ({d['jobs']} jobs)" for d in missing_have[:8])) if missing_have else "")

    # Experience
    if not exp:
        add("Experience", "e_present", "Experience pasted", "warn", "Paste your Experience section to check it.", "")
    else:
        lines = [l for l in exp.splitlines() if l.strip()]
        numbered = sum(bool(NUM.search(l)) for l in lines)
        add("Experience", "e_numbers", "Roles show results", "pass" if numbered >= 3 else "warn",
            f"{numbered} line{'s' if numbered != 1 else ''} with a number.", "" if numbered >= 3 else "Copy your strongest CV bullets (with numbers) into each recent role.")
        add("Experience", "e_keywords", "Skills shown in use, not just listed",
            "pass" if sum(_has(d["term"], _fold(exp)) for d in wanted) >= 3 else "warn",
            "Recruiter search weighs skills that appear in your roles.",
            "" if sum(_has(d["term"], _fold(exp)) for d in wanted) >= 3 else "Mention the tools you used in each role's description.")

    # Things only you can see
    url = re.search(r"linkedin\.com/in/([A-Za-z0-9_-]+)", bank_text or "")
    if url:
        tidy = not re.search(r"-[0-9a-f]{5,}$|\d{4,}$", url.group(1))
        add("Settings", "url", "A tidy custom URL", "pass" if tidy else "warn", f"linkedin.com/in/{url.group(1)}",
            "" if tidy else "Edit it in LinkedIn to just your name, then update your CV.")
    for iid, title, fix in MANUAL:
        add("Settings", iid, title, "pass" if iid in ticked else "manual", "", fix)

    auto = [i for i in items if i["status"] != "manual"]
    score = round(100 * sum({"pass": 1, "warn": .5}.get(i["status"], 0) for i in auto) / len(auto)) if auto else 0
    return {"score": score, "items": items, "demand": dem, "targets": targets, "checked_at": time.time()}


# ----------------------------------------------------------------------------- AI drafts

SUGGEST = """You help a UK job seeker improve their LinkedIn profile. Use ONLY the facts given: never add a
skill, employer, title, number or claim that isn't there, and never change a number. Write in UK English.

Return JSON only:
{"headlines": ["three options, each at most 220 characters: target role | 2-3 strongest relevant skills | one result"],
 "about": "1,200 to 2,000 characters, first person, short paragraphs. The first 265 characters say who they are, the role and their strongest result. Middle: what they do and the tools, with 2-3 results from the facts. End with one line on what they're open to and how to get in touch.",
 "facts_used": ["F003"]}"""


def suggest(bank_prompt: str, fact_text: dict, targets: list[str], wanted: list[str]) -> dict:
    user = (f"Target roles: {', '.join(targets) or 'not set'}\nIn-demand skills the facts support: {', '.join(wanted)}\n\n"
            f"<fact_bank>\n{bank_prompt}\n</fact_bank>")
    out, used, _ = engine.complete_json(SUGGEST, user, mock=_mock, expected={"headlines": list, "about": str})
    cited = " ".join(fact_text.get(f, "") for f in out.get("facts_used") or []) or " ".join(fact_text.values())
    text = " ".join(out.get("headlines") or []) + " " + (out.get("about") or "")
    warns = sorted({n.strip(".,") for n in NUM.findall(text) if n.strip(".,£$€%kmx") and n.strip(".,") not in cited
                    and n.strip(".,£$€%") not in cited})
    return {"headlines": [h[:HEADLINE_MAX] for h in out.get("headlines") or []][:3], "about": (out.get("about") or "")[:ABOUT_MAX],
            "warnings": [f"'{n}' isn't in your facts: check it before using" for n in warns],
            "engine": engine.LABELS.get(used, used)}


def _mock() -> str:
    return json.dumps({"headlines": ["Data Engineer | Azure, Python, SQL | Built reporting used across 40 sites"],
                       "about": "I build data platforms and reporting that people use every day. " * 20,
                       "facts_used": ["F005"]})


def load() -> dict:
    return load_json(STATE, None) or {}


def save(profile: dict, result: dict | None = None) -> None:
    data = load()
    data["profile"] = profile
    if result is not None:
        data["result"] = result
    save_json(STATE, data)


# ----------------------------------------------------------------------------- guided coach
# One step at a time, like a conversation: each step checks one part of the profile, can offer AI options
# written only from your facts, and adds anything left to do to a running to-do list.

STEPS = [
    ("url", "Profile link"), ("target", "Target role"), ("strengths", "Strengths to lead with"),
    ("headline", "Headline"), ("photo", "Profile photo"), ("banner", "Banner"), ("featured", "Featured work"),
    ("spelling", "Spelling & grammar"), ("cv_match", "Matches your CV"), ("about", "About"),
    ("experience", "Experience"), ("skills", "Skills"), ("recs", "Recommendations"),
    ("age", "Age-bias signals"), ("todo", "Your to-do list"),
]
OLD_TECH = re.compile(r"\b(cobol|fortran|vb6|visual basic 6|lotus notes|foxpro|windows (?:xp|nt|2000)|novell|"
                      r"netware|frontpage|flash|silverlight|dos)\b", re.I)


def _url_check(url: str) -> list[dict]:
    m = re.search(r"linkedin\.com/in/([A-Za-z0-9_%-]+)", url or "")
    if not m:
        return [{"status": "fail", "title": "Profile link added", "detail": "Paste your profile link, e.g. linkedin.com/in/firstname-lastname.", "fix": ""}]
    slug = m.group(1)
    tidy = not re.search(r"-[0-9a-f]{5,}$|\d{4,}$", slug)
    return [{"status": "pass" if tidy else "warn", "title": "Custom link", "detail": f"linkedin.com/in/{slug}",
             "fix": "" if tidy else "In LinkedIn: Edit public profile & URL, then set it to your name (and put it on your CV)."}]


def _spelling(profile: dict, vocab_texts: list[str]) -> list[dict]:
    import spelling
    vocab = spelling._vocabulary(*vocab_texts)
    out = []
    for field in ("headline", "about", "experience"):
        for i, line in enumerate(l for l in (profile.get(field) or "").splitlines() if l.strip()):
            spell, grammar = spelling.check_line(f"{field}.{i}", line, vocab)
            for it in spell + grammar:
                fix = it.get("fix") or {}
                out.append({"status": "warn", "title": f"{field.title()}: {it.get('note') or it.get('label') or 'check this'}",
                            "detail": line[:140], "fix": f"Change “{fix['from']}” to “{fix['to']}”." if fix.get("from") else ""})
    return out[:20]


def _cv_roles(master_text: str) -> list[dict]:
    try:
        import ats
        return ats.parse_jobs([l for l in master_text.splitlines() if l.strip()])
    except Exception:  # noqa: BLE001
        return []


def _cv_match(profile: dict, master_text: str, fact_text: str) -> list[dict]:
    exp = _fold(profile.get("experience") or "")
    if not exp:
        return [{"status": "open", "title": "Paste your Experience section (in the Experience step) to compare it with your CV", "detail": "", "fix": ""}]
    out = []
    for r in _cv_roles(master_text)[:5]:
        title, firm = (r.get("title") or "").strip(), (r.get("company") or r.get("employer") or "").strip()
        title = re.sub(r"\s*\(.*?\)", "", title).strip()
        if not title and not firm:
            continue
        has_t = bool(title) and _fold(title) in exp
        has_f = bool(firm) and _fold(firm) in exp
        st = "pass" if has_t and (has_f or not firm) else "warn"
        out.append({"status": st, "title": f"{title or 'Role'}{' at ' + firm if firm else ''}",
                    "detail": "On LinkedIn as on your CV." if st == "pass" else ("Employer found, title differs." if has_f else "Not found on LinkedIn."),
                    "fix": "" if st == "pass" else "Use the same job title and employer on LinkedIn as on your CV; recruiters compare them."})
    facts = _fold(fact_text)
    stray = sorted({n for n in NUM.findall(" ".join([profile.get("about") or "", profile.get("experience") or ""]))
                    if len(n.strip("£$€%kmx.,")) >= 2 and n.strip(".,") not in facts})
    if stray:
        out.append({"status": "warn", "title": "Numbers not in your facts", "detail": ", ".join(stray[:8]),
                    "fix": "Check each one; if it's true, add it to your facts so your CV and LinkedIn agree."})
    return out or [{"status": "open", "title": "Couldn't read roles from your master CV", "detail": "", "fix": ""}]


def _age_signals(profile: dict) -> list[dict]:
    text = " ".join(profile.get(k) or "" for k in ("headline", "about", "experience"))
    out, year = [], time.localtime().tm_year
    years = [int(y) for y in re.findall(r"\b(19[5-9]\d|20[0-2]\d)\b", text)]
    if years and min(years) <= year - 20:
        out.append({"status": "warn", "title": f"Dates going back to {min(years)}",
                    "detail": "Showing more than 15 to 20 years can invite age bias.",
                    "fix": "Keep detail for the last 15 years; group older roles under 'Earlier career' without dates."})
    m = re.search(r"\b([2-4]\d)\+?\s*years?(?:'|’)?\s*(?:of\s*)?experience", text, re.I)
    if m:
        out.append({"status": "warn", "title": f"“{m.group(0)}”", "detail": "A big year count can read as 'too senior' or 'expensive'.",
                    "fix": "Lead with what you achieved instead, or say '15+ years'."})
    old = sorted({o.lower() for o in OLD_TECH.findall(text)})
    if old:
        out.append({"status": "warn", "title": "Older technologies named", "detail": ", ".join(old),
                    "fix": "Drop them unless the job asks for them; keep your current stack up front."})
    if re.search(r"\b(born|date of birth|dob|aged \d{2})\b", text, re.I):
        out.append({"status": "fail", "title": "Age or date of birth mentioned", "detail": "", "fix": "Remove it: UK employers shouldn't see it."})
    return out or [{"status": "pass", "title": "No age-bias signals found", "detail": "", "fix": ""}]


def _featured(fact_bank: list[dict]) -> list[dict]:
    wins = [f["text"] for f in fact_bank if NUM.search(f["text"]) and len(f["text"]) > 30][:3]
    return [{"status": "open", "title": "Ideas to feature", "detail": " · ".join(w[:90] for w in wins) or "Add a project or case study with a link.",
             "fix": "Pin a project, case study or portfolio link that shows one of these results."}]


def coach(profile: dict, state: dict, bank: list[dict], bank_text: str, terms: set[str], jobs: list[dict],
          titles: list[str], master_text: str) -> dict:
    """Every step's result, the score, and the running to-do list."""
    titles = [state.get("target")] if (state.get("target") or "").strip() else titles
    base = check({**profile, "ticked": state.get("ticked") or []}, bank_text, terms, jobs, titles)
    by_group = {}
    for it in base["items"]:
        by_group.setdefault(it["group"], []).append(it)
    ticked = set(state.get("ticked") or [])
    fact_text = " ".join(f["text"] for f in bank)
    manual = lambda iid, title, fix: [{"status": "pass" if iid in ticked else "open", "title": title, "detail": "", "fix": fix, "tick": iid}]  # noqa: E731
    items = {
        "url": _url_check(state.get("url") or ""),
        "target": [{"status": "pass" if (state.get("target") or "").strip() else "open",
                    "title": state.get("target") or "Say which role you want to be found for", "detail": "", "fix": ""}],
        "strengths": [{"status": "pass" if (state.get("strengths") or "").strip() else "open",
                       "title": state.get("strengths") or "Pick 2 or 3 strengths to lead with", "detail": "", "fix": ""}],
        "headline": by_group.get("Headline", []),
        "photo": manual("photo", "A clear, recent photo", "Head and shoulders, plain background, good light."),
        "banner": manual("banner", "A banner that says what you do", "A simple image with your role or one line of proof."),
        "featured": manual("featured", "Something pinned in Featured", "") + _featured(bank),
        "spelling": _spelling(profile, [bank_text, master_text]) or [{"status": "pass", "title": "No spelling or grammar issues found", "detail": "", "fix": ""}],
        "cv_match": _cv_match(profile, master_text, fact_text),
        "about": by_group.get("About", []),
        "experience": by_group.get("Experience", []),
        "skills": by_group.get("Skills", []),
        "recs": manual("recs", "Two or more recent recommendations", "Ask a manager or senior colleague; the draft message below helps."),
        "age": _age_signals(profile),
    }
    steps, todo = [], []
    for sid, title in STEPS:
        its = items.get(sid, [])
        if sid == "todo":
            continue
        worst = "open" if not its else min((i["status"] for i in its), key=["fail", "open", "warn", "manual", "pass"].index)
        steps.append({"id": sid, "title": title, "status": {"fail": "todo", "warn": "todo", "open": "open"}.get(worst, "done"), "items": its})
        todo += [{"step": sid, "text": i.get("fix") or i.get("detail") or i["title"]} for i in its if i["status"] in ("fail", "warn")]
    done = sum(s["status"] == "done" for s in steps)
    steps.append({"id": "todo", "title": "Your to-do list", "status": "done" if not todo else "todo", "items": []})
    return {"steps": steps, "todo": todo, "score": round(100 * done / (len(STEPS) - 1)), "demand": base["demand"],
            "targets": base["targets"], "checked_at": time.time()}


OPTIONS = {
    "headline": ("4 headline options, each at most 220 characters, in the shape: target role | 2 or 3 strongest relevant "
                 "skills | one result or unique value, all from the facts.", 4),
    "about": ("2 About sections, each 1,200 to 2,000 characters, first person, short paragraphs. The first 265 characters "
              "say who they are, the role they want and their strongest result. End with what they're open to and how to "
              "get in touch.", 2),
    "experience": ("3 versions of the description for their most recent role: 3 to 5 short lines, each a result with its "
                   "number where the facts give one, naming the tools used.", 3),
    "strengths": ("3 options, each a comma-separated list of 3 strengths to lead with, worded as a recruiter would search "
                  "for them and matching the target role.", 3),
    "recs": ("2 short, friendly LinkedIn messages asking the named person for a recommendation, saying which 2 or 3 "
             "things from shared work they could mention (only from the facts).", 2),
    "banner": ("3 banner lines, each at most 60 characters: the role and one line of proof.", 3),
}
OPTIONS_SYSTEM = """You coach a UK job seeker through improving their LinkedIn profile, one part at a time.
Use ONLY the facts given: never add a skill, employer, title, number or claim that isn't there, and never
change a number. UK English. Plain, confident, no filler ('passionate', 'results-driven', 'team player').
Task: {task}
Return JSON only: {{"options": ["..."], "facts_used": ["F003"]}}"""


def options(step: str, bank_prompt: str, fact_text: dict, state: dict, profile: dict, wanted: list[str],
            emphasis: str = "", person: str = "") -> dict:
    task, n = OPTIONS[step]
    user = "\n".join([
        f"Target role: {state.get('target') or 'not set'}",
        f"Strengths they chose: {state.get('strengths') or 'not set'}",
        f"In-demand skills their facts support: {', '.join(wanted) or 'none known'}",
        f"Current {step}: {profile.get(step) or state.get(step) or '(empty)'}"[:3000],
        f"Person to ask: {person}" if person else "",
        f"Put more emphasis on: {emphasis}" if emphasis else "",
        f"<fact_bank>\n{bank_prompt}\n</fact_bank>"])
    out, used, _ = engine.complete_json(OPTIONS_SYSTEM.format(task=task), user, expected={"options": list},
                                        mock=lambda: json.dumps({"options": [f"Option {i + 1} for {step}: Data Engineer | Python, SQL, Azure | reporting used across 40 sites"
                                                                             for i in range(n)], "facts_used": ["F005"]}))
    opts = [str(o).strip() for o in out.get("options") or [] if str(o).strip()][:n]
    cited = " ".join(fact_text.get(f, "") for f in out.get("facts_used") or []) or " ".join(fact_text.values())
    warn = []
    for o in opts:
        bad = sorted({x.strip(".,") for x in NUM.findall(o) if len(x.strip(".,£$€%kmx")) >= 2
                      and x.strip(".,") not in cited and x.strip(".,£$€%") not in cited})
        warn.append([f"'{b}' isn't in your facts: check it before using" for b in bad])
    if step == "headline":
        opts = [o[:HEADLINE_MAX] for o in opts]
    return {"step": step, "options": opts, "warnings": warn, "engine": engine.LABELS.get(used, used)}
