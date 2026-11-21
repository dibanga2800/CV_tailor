"""UK English spelling and grammar for CV lines.

Three kinds of finding, each with a one-click fix where there is one:
  - American spellings (analyze, modeled, optimization) -> their UK forms;
  - likely typos (managment), using a word list, never flagging a word that appears in your own facts
    or the job advert (your tools, employers and acronyms);
  - grammar slips recruiters notice: a repeated word, "a"/"an", spacing round punctuation, and present-tense
    verbs opening bullets in roles you have left ("Manage" in a 2018 job).
"""
from __future__ import annotations

import re

try:  # optional: typo detection needs the word list; UK spellings and grammar work without it
    from spellchecker import SpellChecker
    _SPELL = SpellChecker(distance=1)
except Exception:  # noqa: BLE001 - not installed yet (start_webui.bat installs it)
    _SPELL = None

# American stem -> British stem; every inflection is generated below (organize -> organizes, organized ...).
_STEMS = {
    "analyz": "analys", "paralyz": "paralys", "catalyz": "catalys",
    "organiz": "organis", "optimiz": "optimis", "prioritiz": "prioritis", "standardiz": "standardis",
    "summariz": "summaris", "utiliz": "utilis", "minimiz": "minimis", "maximiz": "maximis", "visualiz": "visualis",
    "customiz": "customis", "realiz": "realis", "recogniz": "recognis", "specializ": "specialis",
    "centraliz": "centralis", "modulariz": "modularis", "normaliz": "normalis", "authoriz": "authoris",
    "categoriz": "categoris", "characteriz": "characteris", "digitiz": "digitis", "finaliz": "finalis",
    "initializ": "initialis", "localiz": "localis", "mobiliz": "mobilis", "monetiz": "monetis",
    "operationaliz": "operationalis", "productioniz": "productionis", "rationaliz": "rationalis",
    "revolutioniz": "revolutionis", "stabiliz": "stabilis", "synchroniz": "synchronis", "emphasiz": "emphasis",
    "apologiz": "apologis", "criticiz": "criticis", "familiariz": "familiaris", "harmoniz": "harmonis",
    "itemiz": "itemis", "memoriz": "memoris", "modernis": "modernis", "moderniz": "modernis",
    "capitaliz": "capitalis", "commercializ": "commercialis", "containeriz": "containeris",
    "virtualiz": "virtualis", "parameteriz": "parameteris", "serializ": "serialis", "tokeniz": "tokenis",
    "anonymiz": "anonymis", "pseudonymiz": "pseudonymis", "incentiviz": "incentivis", "socializ": "socialis",
    "behavior": "behaviour", "color": "colour", "favor": "favour", "honor": "honour", "labor": "labour",
    "neighbor": "neighbour", "endeavor": "endeavour", "humor": "humour", "rumor": "rumour", "vapor": "vapour",
    "flavor": "flavour", "harbor": "harbour", "savior": "saviour",
    "center": "centre", "theater": "theatre", "meter": "metre", "fiber": "fibre", "liter": "litre",
    "defense": "defence", "offense": "offence", "pretense": "pretence",
    "catalog": "catalogue", "dialog": "dialogue", "analog": "analogue",
    "enroll": "enrol", "fulfill": "fulfil", "skillful": "skilful", "willful": "wilful",
    "judgment": "judgement", "acknowledgment": "acknowledgement", "aging": "ageing",
    "gray": "grey", "mold": "mould", "plow": "plough", "check": "check",
}
_WHOLE = {  # single words, US -> UK
    "modeled": "modelled", "modeling": "modelling", "labeled": "labelled", "labeling": "labelling",
    "traveled": "travelled", "traveling": "travelling", "canceled": "cancelled", "canceling": "cancelling",
    "fueled": "fuelled", "fueling": "fuelling", "leveled": "levelled", "leveling": "levelling",
    "signaled": "signalled", "signaling": "signalling", "channeled": "channelled", "channeling": "channelling",
    "counseled": "counselled", "counseling": "counselling", "totaled": "totalled", "totaling": "totalling",
    "enrolled": "enrolled", "enrollment": "enrolment", "fulfillment": "fulfilment", "installment": "instalment",
    "license": "licence",  # the noun; "licensed" is the same in both
    "practice": "practice", "program": "programme",  # programme for a scheme; software "program" is kept below
    "percent": "per cent", "math": "maths", "airplane": "aeroplane", "tire": "tyre", "cozy": "cosy",
    "jewelry": "jewellery", "mom": "mum", "pajamas": "pyjamas", "draft": "draft", "check": "check",
}
_KEEP = {"practice", "draft", "check", "program", "license", "enrolled"}  # same or ambiguous: never flagged
_SUFFIXES = ("e", "es", "ed", "ing", "ation", "ations", "er", "ers", "able", "ational", "")


def _forms() -> dict[str, str]:
    out: dict[str, str] = {}
    for us, uk in _STEMS.items():
        if us == uk:
            continue
        if us.endswith("z"):  # -ize/-yze families
            for suf in _SUFFIXES:
                if suf:
                    out[us + suf] = uk + suf
        else:  # whole-word stems (color, center, catalog ...)
            for suf in ("", "s", "ed", "ing", "ful", "less", "al", "ally"):
                out[us + suf] = uk + suf
    for us, uk in _WHOLE.items():
        if us not in _KEEP and us != uk:
            out[us] = uk
    # Plurals and verb forms of a few irregular ones
    out.update({"centers": "centres", "centered": "centred", "centering": "centring", "meters": "metres",
                "enrolls": "enrols", "fulfills": "fulfils", "catalogs": "catalogues", "dialogs": "dialogues",
                "defenses": "defences", "licenses": "licences"})
    return out


US_TO_UK = _forms()
_UK_WORDS = set(US_TO_UK.values())

# Opening verbs that should be past tense in a role you have left.
PRESENT_TO_PAST = {
    "manage": "Managed", "lead": "Led", "build": "Built", "design": "Designed", "develop": "Developed",
    "deliver": "Delivered", "create": "Created", "run": "Ran", "own": "Owned", "drive": "Drove",
    "maintain": "Maintained", "support": "Supported", "oversee": "Oversaw", "coordinate": "Coordinated",
    "implement": "Implemented", "analyse": "Analysed", "analyze": "Analysed", "report": "Reported",
    "write": "Wrote", "train": "Trained", "mentor": "Mentored", "present": "Presented", "automate": "Automated",
    "migrate": "Migrated", "monitor": "Monitored", "produce": "Produced", "provide": "Provided",
    "work": "Worked", "handle": "Handled", "ensure": "Ensured", "establish": "Established",
    "line-manage": "Line-managed", "prioritise": "Prioritised", "review": "Reviewed", "set": "Set",
    "plan": "Planned", "launch": "Launched", "reduce": "Reduced", "improve": "Improved", "increase": "Increased",
    "collaborate": "Collaborated", "partner": "Partnered", "liaise": "Liaised", "negotiate": "Negotiated",
}
_CURRENT = re.compile(r"present|current|now|ongoing|date\b", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*[A-Za-z]|[A-Za-z]")
_VOWEL_SOUND_LETTERS = set("aefhilmnorx")  # acronyms read as letters: "an MSc", "an HR" (SQL can be "sequel")


def _fold(s: str) -> str:
    return s.lower().replace("’", "'")


def _vocabulary(*texts: str) -> set[str]:
    return {_fold(w) for t in texts for w in _WORD.findall(t or "")}


def _accept(word: str, vocab: set[str]) -> bool:
    """Words we never call typos: yours, the advert's, names, acronyms, codes, and UK spellings."""
    w = _fold(word)
    if len(w) < 4 or w in vocab or w in _UK_WORDS or not word.isalpha() or word[0].isupper():
        return True
    if _SPELL is None or w in _SPELL:
        return True
    if w.endswith("s") and (w[:-1] in _SPELL or w[:-1] in vocab):  # plural of a known word
        return True
    if any(w[:i] in _SPELL and w[i:] in _SPELL and (len(w) - i >= 3 or w[i:] in ("up", "in", "on"))
           for i in range(3, len(w) - 1)):  # workflow, lookup, codebase
        return True
    # A UK spelling whose American twin is in the word list (organise -> organize, colour -> color).
    for uk, us in (("ise", "ize"), ("isa", "iza"), ("yse", "yze"), ("our", "or"), ("tre", "ter"),
                   ("ence", "ense"), ("ogue", "og"), ("lled", "led"), ("lling", "ling"), ("mme", "m")):
        if uk in w and w.replace(uk, us) in _SPELL:
            return True
    return False


def _sub(text: str, wrong: str, right: str) -> str:
    """Replace one word, keeping its capital letter."""
    if wrong[:1].isupper():
        right = right[:1].upper() + right[1:]
    return re.sub(rf"(?<![A-Za-z]){re.escape(wrong)}(?![A-Za-z])", right, text, count=1)


def check_line(path: str, text: str, vocab: set[str], past_role: bool = False) -> tuple[list[dict], list[dict]]:
    """(spelling items, grammar items) for one line. Each item can carry fix = {"from", "to"} for the line."""
    spell, grammar = [], []
    seen = set()
    for m in _WORD.finditer(text):
        word = m.group(0)
        w = _fold(word)
        if w in seen:
            continue
        uk = US_TO_UK.get(w)
        if uk and w not in vocab:  # an American spelling (unless the advert or your facts spell it that way)
            seen.add(w)
            spell.append({"path": path, "text": text, "note": f"'{word}' is US spelling: use '{uk}'",
                          "fix": {"from": word, "to": _sub(word, word, uk)}})
        elif not uk and not _accept(word, vocab):
            seen.add(w)
            guess = _SPELL.correction(w) if _SPELL else None
            spell.append({"path": path, "text": text, "severity": "fail",
                          "note": f"'{word}' may be a typo" + (f": did you mean '{guess}'?" if guess and guess != w else ""),
                          **({"fix": {"from": word, "to": _sub(word, word, guess)}} if guess and guess != w else {})})
    # Grammar
    rep = re.search(r"\b([A-Za-z]+)\s+\1\b", text, re.I)
    if rep and rep.group(1).lower() not in ("that", "had"):
        grammar.append({"path": path, "text": text, "note": f"'{rep.group(1)}' is repeated",
                        "fix": {"from": rep.group(0), "to": rep.group(1)}})
    for m in re.finditer(r"\b([Aa]n?)\s+([A-Za-z][\w-]*)", text):
        art, nxt = m.group(1), m.group(2)
        if nxt.isupper() and len(nxt) > 1:  # an acronym: sounded by its first letter
            want = "an" if nxt[0].lower() in _VOWEL_SOUND_LETTERS else "a"
        elif nxt[0].isupper() and nxt[1:].islower() and nxt.isalpha():  # a name: Azure, Excel, Oracle
            if nxt[0] in "U":
                continue
            want = "an" if nxt[0] in "AEIO" else "a"
        elif nxt[0].islower() and nxt.isalpha():
            if nxt.lower().startswith(("uni", "use", "usu", "eu", "one", "once")):
                want = "a"
            elif nxt.lower().startswith(("hour", "honest", "honour", "heir")):
                want = "an"
            else:
                want = "an" if nxt[0] in "aeiou" else "a"
        else:
            continue
        if art.lower() != want:
            right = want.capitalize() if art[0].isupper() else want
            grammar.append({"path": path, "text": text, "note": f"'{art} {nxt}' should be '{right} {nxt}'",
                            "fix": {"from": f"{art} {nxt}", "to": f"{right} {nxt}"}})
            break
    space = re.search(r"\s+([,.;:])(?!\d)|([,;:])(?=[A-Za-z])|\s{2,}", text)
    if space:
        bad = space.group(0)
        good = space.group(1) if space.group(1) else (space.group(2) + " " if space.group(2) else " ")
        grammar.append({"path": path, "text": text, "note": "Spacing around punctuation", "fix": {"from": bad, "to": good}})
    if past_role:
        first = (text.strip().split() or [""])[0]
        past = PRESENT_TO_PAST.get(_fold(first).strip(",.;:"))
        if past and first[:1].isupper():
            grammar.append({"path": path, "text": text,
                            "note": f"'{first}' is present tense in a role you've left: use '{past}'",
                            "fix": {"from": first, "to": past}})
    return spell, grammar


def check_cv(cv: dict, lines, vocab_texts: list[str]) -> tuple[list[dict], list[dict]]:
    """Run over every line of the CV (lines = (path, label, text, facts) tuples), plus the headline."""
    vocab = _vocabulary(*vocab_texts)
    spell, grammar = [], []
    exp = cv.get("experience") or []
    rows = [("subtitle", cv.get("subtitle", ""))] + [(p, t) for p, _l, t, _f in lines]
    for path, text in rows:
        if not text:
            continue
        m = re.match(r"experience\.(\d+)\.bullets\.", path)
        past = bool(m) and int(m.group(1)) < len(exp) and not _CURRENT.search(exp[int(m.group(1))].get("dates", ""))
        s, g = check_line(path, text, vocab, past)
        spell += s
        grammar += g
    return spell[:30], grammar[:30]
