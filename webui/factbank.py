"""Build a numbered fact bank from the master CV (and the repo profile, once /setup has run).

Every tailored bullet must cite fact IDs from this bank, the same contract the
jd-resume-tailor skill uses ("a bullet without a source tag is invalid"). IDs are
assigned deterministically, so the grounding check never depends on the model.

  F### - a line or bullet from the uploaded master CV
  P### - a line from .claude/skills/job-application-assistant/01-candidate-profile.md
  X### - a fact you added in the UI (stored in documents/cv/extra_facts.md)
"""

from __future__ import annotations

import re

from store import MASTER_DIR, SKILL_DIR

BULLET = re.compile(r"^\s*([•●▪◦‣⁃*\-–—]|\d+[.)])\s+")
HEADER_WORDS = {
    "profile", "summary", "personal statement", "professional summary", "experience",
    "work experience", "professional experience", "employment history", "employment",
    "education", "qualifications", "skills", "key skills", "technical skills", "core skills",
    "core competencies", "certifications", "certificates", "projects", "achievements",
    "languages", "interests", "references", "volunteering", "awards", "publications",
    "training", "career history", "key achievements", "additional information",
}
EXTRA_FACTS = MASTER_DIR / "extra_facts.md"


def _is_header(line: str) -> bool:
    s = line.strip().strip(":").strip()
    if not s or len(s) > 40:
        return False
    if s.lower().lstrip("# ") in HEADER_WORDS:
        return True
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 4 and all(c.isupper() for c in letters) and len(s.split()) <= 5


def split_facts(text: str) -> list[dict]:
    facts: list[dict] = []
    section = "Header"
    buf = ""

    def flush():
        nonlocal buf
        t = re.sub(r"\s+", " ", buf).strip()
        if len(t) >= 3:
            facts.append({"section": section, "text": t})
        buf = ""

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if _is_header(line):
            flush()
            section = line.strip().strip(":").lstrip("# ").title()
            continue
        m = BULLET.match(line)
        if m:
            flush()
            buf = line[m.end():]
            continue
        stripped = line.strip()
        # A wrapped continuation (PDF line wrap): this line starts lower-case, with a digit
        # or bracket, or the previous chunk ends mid-phrase.
        if buf and (stripped[:1].islower() or stripped[:1].isdigit() or stripped[:1] in "(&"
                    or buf.rstrip()[-1:] in ",;&(/-"):
            buf += " " + stripped
        else:
            flush()
            buf = stripped
    flush()
    return facts


def _profile_lines() -> list[dict]:
    path = SKILL_DIR / "01-candidate-profile.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    section = "Profile"
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            section = s.lstrip("# ").strip()
            continue
        if not s.startswith(("-", "|")) or "[" in s and re.search(r"\[[A-Z_ ,./-]{4,}\]", s):
            continue  # skip placeholders such as [YOUR_NAME]
        if set(s) <= set("|-: "):
            continue
        s = s.lstrip("- ").strip()
        if len(s) >= 4 and not s.startswith("<!--"):
            out.append({"section": section, "text": s})
    return out


def profile_is_setup() -> bool:
    try:
        text = (SKILL_DIR / "01-candidate-profile.md").read_text(encoding="utf-8")
    except OSError:
        return False
    return "[YOUR_NAME]" not in text


REMOVED = "[removed] "


def _extra_facts(include_removed: bool = False) -> list[dict]:
    """Facts added in the app. A removed fact keeps its line (marked) so later X numbers never shift."""
    try:
        text = EXTRA_FACTS.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for l in text.splitlines():
        if l.startswith("- ") and l[2:].strip():
            body = l[2:].strip()
            gone = body.startswith(REMOVED)
            if gone and not include_removed:
                continue
            out.append({"section": "Added in UI", "text": body[len(REMOVED):] if gone else body, "removed": gone})
    return out


def remove_extra_fact(index: int) -> str:
    """Mark the index-th (1-based) added fact as removed; returns its text."""
    lines = EXTRA_FACTS.read_text(encoding="utf-8").splitlines()
    n = 0
    for i, l in enumerate(lines):
        if l.startswith("- ") and l[2:].strip():
            n += 1
            if n == index:
                body = l[2:].strip()
                if body.startswith(REMOVED):
                    raise ValueError("already removed")
                lines[i] = "- " + REMOVED + body
                EXTRA_FACTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return body
    raise ValueError("no such fact")


def add_extra_fact(text: str) -> None:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise ValueError("empty fact")
    EXTRA_FACTS.parent.mkdir(parents=True, exist_ok=True)
    existing = EXTRA_FACTS.read_text(encoding="utf-8") if EXTRA_FACTS.exists() else (
        "# Facts added in the CV tailor UI\n\nConfirmed by the candidate. Copy these into "
        "01-candidate-profile.md (or re-run /setup) so /apply grounds them too.\n\n")
    EXTRA_FACTS.write_text(existing + f"- {text}\n", encoding="utf-8")


def build(master_text: str) -> list[dict]:
    bank = []
    for prefix, items in (("F", split_facts(master_text)),
                          ("P", _profile_lines() if profile_is_setup() else []),
                          ("X", _extra_facts(include_removed=True))):
        for i, f in enumerate(items, 1):
            if f.pop("removed", False):
                continue  # its number stays taken
            bank.append({"id": f"{prefix}{i:03d}", **f})
    return bank


def as_prompt(bank: list[dict]) -> str:
    lines, section = [], None
    for f in bank:
        if f["section"] != section:
            section = f["section"]
            lines.append(f"\n## {section}")
        lines.append(f"{f['id']}: {f['text']}")
    return "\n".join(lines).strip()


# ----------------------------------------------------------------------------- skills bank
# Skills the candidate confirms they have, kept as facts ("- Skill: Snowflake") so tailoring
# can ground them too.

SKILL_PREFIX = "Skill: "


def skills() -> list[str]:
    return [f["text"][len(SKILL_PREFIX):].strip() for f in _extra_facts() if f["text"].startswith(SKILL_PREFIX)]


def add_skills(names: list[str]) -> list[str]:
    have = {s.lower() for s in skills()}
    for n in names:
        n = re.sub(r"\s+", " ", str(n)).strip().strip(",;")
        if n and len(n) <= 80 and n.lower() not in have:
            add_extra_fact(SKILL_PREFIX + n)
            have.add(n.lower())
    return skills()


def remove_skill(name: str) -> list[str]:
    """Mark the skill removed rather than deleting its line, so later X fact numbers don't shift."""
    if EXTRA_FACTS.exists():
        target = f"- {SKILL_PREFIX}{name}".strip().lower()
        lines = EXTRA_FACTS.read_text(encoding="utf-8").splitlines()
        lines = [("- " + REMOVED + l.strip()[2:]) if l.strip().lower() == target else l for l in lines]
        EXTRA_FACTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return skills()
