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
