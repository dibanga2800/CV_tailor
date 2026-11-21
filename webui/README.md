# CV Tailor (web UI)

A local editor for tailoring your CV to one job: upload a job description and a person
specification, get a tailored CV where every line cites the fact in your master CV it came
from, then edit it in a responsive preview and download Word, LaTeX or Markdown.

It sits on top of this repo instead of replacing it:

| Repo piece | How the UI uses it |
|---|---|
| `tools/posting_guard.py` | Runs on every JD and person spec before the model sees them (hidden text, injection-shaped lines). |
| `03-writing-style.md`, `05-cv-templates.md` | House style and ATS rules (ASCII date ranges, in-progress qualifications) are loaded into the prompt. |
| `01-candidate-profile.md` | Once `/setup` has filled it, its lines join the fact bank as `P###` facts. |
| `documents/cv/` | Your master CV and fact bank live here (gitignored). |
| `documents/applications/<company>_<role>/` | Posting, person spec, draft and files are archived here, the folder `/outcome` and `/interview` read. |
| `cv/main_<company>_<role>.tex` | The LaTeX CV uses the repo's moderncv banking template; "Compile PDF" runs lualatex and `tools/verify_pdf.py`. |
| `job_search_tracker.csv` | Gets the same `drafted` row `/apply` writes (toggle in Options). |

## Start

Windows: double-click `start_webui.bat` in the repo root. macOS/Linux: `./start_webui.sh`.
First run creates `webui/.venv` and installs `webui/requirements.txt`. Then open
<http://127.0.0.1:8765>. The server only listens on your own machine.

## AI engines (with automatic fallback)

Open **AI providers** to choose the preferred provider, reorder fallbacks, or run a connection test.
The app tries the next configured provider after a timeout, service error, or invalid response.
In **Automatic** mode, tailoring gives priority to providers that completed a draft in the last
hour of the current session. Set **Use first** to a provider to follow your chosen order.
Recently failed providers are paused for five minutes. A monthly spend limit pauses that provider
for one hour. You can immediately retest a paused provider to restore it after it responds.
The progress panel shows failed attempts and the current provider. Each draft records the provider that wrote it.

**Configured** means the CLI or API key was found. **Connected** means the provider answered a test
or completed a request during this app session. Subscription providers use their own CLI sign-in.

| Engine | Uses | Set up |
|---|---|---|
| Claude Code | your Claude subscription | install Claude Code, run `claude` once and log in |
| ChatGPT (Codex CLI) | your ChatGPT plan | `npm install -g @openai/codex`, then `codex login` > Sign in with ChatGPT |
| Grok Build CLI | your SuperGrok / X Premium+ subscription | `irm https://x.ai/cli/install.ps1 \| iex`, then `grok login` |
| Cursor CLI (optional, last in the order) | your Cursor subscription | `irm 'https://cursor.com/install?win32=true' \| iex`, then `cursor-agent login` |
| DeepSeek API | pay as you go | key from platform.deepseek.com (add credit), paste in the engine menu |
| OpenAI API | pay as you go | key from platform.openai.com (separate from ChatGPT Plus) |
| Anthropic API | pay as you go | key from console.anthropic.com |

Models can be pinned with `CODEX_MODEL`, `GROK_MODEL`, `CURSOR_MODEL`, `DEEPSEEK_MODEL` (default `deepseek-flash`),
`OPENAI_MODEL` (default `gpt-5-mini`) and `CV_TAILOR_MODEL` (Claude) in `webui/.env`.
Grok uses `grok-4.7-build-fast` with low reasoning effort for text-only drafting. Set
`GROK_MODEL` or `GROK_REASONING_EFFORT` to override these defaults.
Set `CV_TAILOR_ENGINE_TIMEOUT` to change the default 240-second provider timeout.
`CV_TAILOR_REQUEST_TIMEOUT` sets the 540-second default budget across providers. The budget
is at least the configured single-provider timeout.

Tailoring starts with a CV by default. Select **Write a cover letter** or **Draft a supporting
statement** when the application needs those documents.

## PostgreSQL (optional, recommended)

Click **Files only** (top right) to connect PostgreSQL. Once connected, Postgres is the system of record for
everything the app keeps about you: master CV and facts, skills bank, imported projects, tailored CVs and
their versions, job adverts, the job list and statuses, applications and outcomes, interview packs, ATS
work and settings, and `job_search_tracker.csv`.

- The files under `documents/` stay as the working copy (the repo's Claude Code commands read them), kept
  in step both ways: changes are saved to Postgres within seconds (including changes made by `/apply`),
  deletes are mirrored, and on start-up anything missing or older on disk is restored. A wiped folder or a
  new PC gets everything back from the database.
- If the database is down, the app keeps working on files and catches up when it's back.
- Query-friendly tables for SQL or Power BI: `cvt_jobs`, `cvt_applications`, `cvt_application_events`,
  `cvt_facts` (stored files are in `cvt_files`). Read them freely; let the app do the writing.
- The sponsor register download is not stored (it's public and re-downloadable).
- Run one copy of the app per database: two PCs writing the same database at once would overwrite each
  other's newest changes.

Setup on Windows: install PostgreSQL (postgresql.org/download/windows or
`winget install PostgreSQL.PostgreSQL.17`), then in **SQL Shell (psql)** as `postgres`, one line at a time:

```sql
CREATE USER cvtailor WITH PASSWORD 'choose-a-password';
CREATE DATABASE cvtailor OWNER cvtailor;
```

Then paste `postgresql://cvtailor:choose-a-password@localhost:5432/cvtailor` into the Database dialog. It is
saved to `webui/.env` (gitignored) as `DATABASE_URL`.

## Finding the right jobs

- **Search criteria** lets you add or remove job titles and locations, with a radius in miles for
  each location. Use **Save search** to keep the criteria or **Save & scan jobs** to search with them.
- **Scan sources** includes [Himalayas](https://himalayas.app/api) and [We Work Remotely](https://weworkremotely.com/remote-job-rss-feed)
  when **Include UK remote roles** is enabled. For [JobServe](https://www.jobserve.com/gb/en/Job-Search/),
  enable the source and paste the RSS alert URL from a saved JobServe search.
- **Browse other boards** opens Indeed, CharityJob, jobs.ac.uk and Civil Service Jobs. Use
  **Add a job** and paste the advert with its link to save it with its source label.

- **Best fit** sort ranks jobs by skills match adjusted for your preferences (Find jobs > Your preferences:
  work mode, contract types, sponsorship need, agencies, notice period, minimum salary), ghost-job risk
  and earlier applications. Each job lists why it scored as it did; nothing is hidden.
- **Ghost-job risk** adds up visible signals: posted 30+ days ago, still up a month after your first scan,
  the same role reposted, closing date passed, "talent pool" wording, no salary, an agency advert that
  doesn't name the employer. Signals, not proof.
- **Applied before / rejected before** warnings come from `job_search_tracker.csv`, on the job card and
  again when you click Apply.
- **Skilled Worker pay check** guesses the likely occupation code from the title and compares the advertised
  salary with the higher of the general threshold and the going rate (figures in `sponsors.py`, checked
  November 2026 against GOV.UK). The employer chooses the real code: a guide, not immigration advice.

## Applications and outcomes

The **Applications** tab records what happens after you apply (interview, offer, rejected, no response,
withdrawn...) with a date and note, including applications made outside the app. Status stays in the
tracker CSV; each tailored application also keeps `outcome.json` with a snapshot of what was sent (CV
version, match and ATS scores, source, work mode, sponsor status, cover letter, projects section). The tab
shows interview and offer rates, days to a reply, who hasn't replied after 3 weeks, and interview rates
broken down by role type, seniority, source, work mode, sponsorship, match score and CV choices. Groups
under 5 applications are marked: small numbers can be chance.

## Interview preparation

In Tailor & apply, the **Interview** tab writes a prep pack for that job: likely questions (one per
essential criterion first) with STAR answer plans built only from your facts, follow-ups, questions to ask,
and gaps to prepare for. Technical exercises for the role include **SQL that runs** against sample tables in
an in-memory SQLite database (your result is compared with the model answer; SELECT only, 3-second limit),
plus data modelling, pipeline design, Python and BI tasks. Practice answers are marked against a rubric, and
any claim not in your facts is pointed out.

## Evidence, not invention

- **Results you could add** (Checks tab) lists lines with no number in them or their facts. Type the real
  result: it's saved as a fact and the assistant rewrites the line from it.
- The ATS check marks skills **shown** in a role or project (✓) versus only **listed** (◐), and checks
  seniority and named certifications the advert asks for.

## CV designs

Pick **Classic**, **Modern** or **Compact** next to the accent colours. All three are ATS-safe (one column,
no tables, text boxes or images, contact details in the body, body text 10pt or larger); the Word file and
the on-screen preview follow the choice.

## Import projects that aren't on your CV

Every line the app or assistant writes must come from your fact bank (master CV + facts you add), so
nothing appears that you couldn't defend in an interview. To use projects that aren't on your CV yet,
open **Facts > Import projects**:

- **GitHub:** type your username, tick repos, click **Draft facts**. The app reads each repo's README,
  file list and a sample of its code (notebooks, SQL, Python, dbt) through GitHub's public API, and
  takes the dates from the repo history. Optional `GITHUB_TOKEN` in the API keys raises GitHub's
  limit of 60 calls an hour.
- **LinkedIn:** LinkedIn has no API for reading profiles and its terms forbid scraping, so paste your
  Projects section (or the text of LinkedIn's Save to PDF) instead.

You edit and tick each drafted line; only ticked lines are saved, as `Project: name (dates; link; tools): fact`.
Then ask the assistant "Add my Databricks project", and it proposes a new project entry citing those
facts, which you add with one click.

## Dedicated ATS check

The **ATS check** tab reads the actual .docx or .pdf you would send, in code (no AI, instant, free):
contact details, standard section headings, date ranges, plain-hyphen dates, length, and layout
risks that break parsers (tables, text boxes, columns, images, contact details in the header or
footer, unusual fonts, PDFs with no text layer or garbled characters). Add a job description to get
keyword match (essential terms weighted 3x) and job-fit signals (title, years of experience, degree,
clearance, driving licence), with every formula shown. It also runs automatically on every
tailored CV's Word file (Match tab).


### Improve and recheck

Under the scores, **Improve this CV** turns the report into edits. Every change goes to a new copy of your
Word file (v1, v2 ... under `documents/cv/ats_work/`, gitignored) and is checked again. Your original is
never changed, and you can go back to any version, download it, or make it your master CV.

- **Format fixes:** plain hyphens in date ranges, your phone number on the contact line, standard fonts.
- **Headline and profile:** edit them directly. The ad's soft-skill words are listed so you can use the true ones.
- **Skills:** for the job you picked, the panel shows what you have that isn't on this CV (master CV and skills
  bank), what the ad asks for, and items on your lines the ad never mentions (general office tools like Word or
  Trello are marked as easy cuts). Click to add or remove, or edit a line directly.
- **Skills bank:** skills you have that don't fit on every CV. Saved as facts (`Skill: ...`) in
  `documents/cv/extra_facts.md`, so tailoring can use them too. A skill the ad asks for is only added
  after you confirm you have it.

## Which Claude does the writing (legacy notes)

- **Claude Code (default if installed):** uses your existing Claude login through `claude -p`.
  No API key needed.
- **Anthropic API:** copy `webui/.env.example` to `webui/.env` and set `ANTHROPIC_API_KEY`.
  Optional `CV_TAILOR_MODEL` (default `claude-sonnet-5-5`).
- `CV_TAILOR_ENGINE=mock` serves a canned draft for trying the UI.

## How tailoring works

1. **Fact bank.** Your master CV is split into numbered facts (`F001`...). Add true facts that
   are not on it in "View fact bank" (`X###`, saved to `documents/cv/extra_facts.md`).
2. **Requirement map** (the hiring-interviews *map-role-requirements* method). Every criterion
   gets an ID, its essential / desirable / unrated status as the documents state it, the
   verbatim passage, and how it is assessed. Nothing is added that the documents do not say.
3. **Tailored CV** (the *jd-resume-tailor* method). Every line carries fact IDs; skills come
   only from the bank; numbers are never changed; gaps stay visible.
4. **Checks in code, not by the model.** After every save: untagged lines, numbers not in the
   cited facts, employers/titles/dates not in your master CV, ATS date format, contact details,
   bullet length and repeated verbs, clichés and dashes, two-page budget, and whether every
   evidenced essential criterion actually appears on the CV.
5. **Match score** = Σ(weight × coverage) / Σ(weight), essential ×2, others ×1; evidenced 1,
   partial 0.5, listed only 0.25, gap 0. The formula and inputs are shown under the score.

## Find jobs (scan) and apply

The **Find jobs** tab scans, de-duplicates (with the repo's `tools/job_key.py`) and ranks vacancies:

| Source | What you need | Notes |
|---|---|---|
| NHS Jobs | Nothing | Official search XML feed; Trac-run trusts publish here too |
| GOV.UK Work Hub | Nothing | DWP's replacement for Find a Job; its robots.txt allows `/jobs` |
| Arbeitnow UK | Nothing (free, no key) | UK feed of jobs from employers' own ATSs; flags visa sponsorship |
| Reed | Free key from reed.co.uk/developers | Official API; full advert fetched at tailor time |
| Adzuna | Free app_id + app_key from developer.adzuna.com | Official API, ~1,000 calls/month free; full ad fetched with `tools/fetch_page.py` (robots.txt enforced) |
| Company boards | Nothing | Public Greenhouse / Lever / Ashby / Workable feeds, e.g. `greenhouse:companyname` |
| LinkedIn | `bun` + the repo's linkedin-search CLI | Against LinkedIn's terms for automated use; off by default, capped low |

**Not scannable, by design:** Indeed has no public API (retired) and blocks automated access;
Civil Service Jobs puts an "I'm not a robot" check in front of search. For those, and for Trac or
recruiter emails, use **+ Add a job**: paste the advert and it joins the list like any other.

Keys are saved to `webui/.env`. The job list and search settings live in `documents/postings/` (gitignored).
Each job gets a **quick match** (share of the advert's skill terms found in your master CV) so you
can triage; the full requirement mapping runs when you press **Tailor my CV**.

**Apply ▸** (in the editor, once a CV came from a job) opens the employer's application page,
downloads your tailored CV (and statement), copies your profile summary to the clipboard, and,
after you confirm you pressed Submit, moves the tracker row from `drafted` to `applied`. It never
submits anything for you: you check the form and press Submit yourself.

## Visa sponsorship

Each job shows two separate signals:
- **Licensed sponsor:** the employer matched against the Home Office *Register of licensed sponsors:
  workers* (Skilled Worker route, A or B rating). Click **Update from GOV.UK** in Find jobs (it
  downloads the official CSV through the GOV.UK Content API), or download the CSV yourself and use
  **Upload CSV**. Cached in `documents/postings/` and flagged as stale after 7 days.
- **What the advert says:** "sponsorship available" vs "unable to sponsor / must have the right to work".

Use **Sponsors only** to filter. A licence means the employer *can* sponsor; the role still has to
meet the Skilled Worker occupation and salary rules. Agency postings are marked, because the
register check then applies to the agency, not the hiring employer. Names are matched on the
legal name, so a brand name can miss.

## Cover letters

Tick **Write a cover letter** (on by default). The letter follows the repo's
`06-cover-letter-templates.md` structure, one page, every claim tagged to your facts; numbers
quoted from the advert itself (job reference, "30,000 homes") are allowed. Edit it in the
**Letters** tab. You get a Word file, plus `cover_letters/cover_<company>_<role>.tex` for the repo's
`cover.cls` (compile with xelatex). The Apply pack downloads it with the CV.

## Enhancv (optional)

If you have an Enhancv **Business** plan API key, add it under Find jobs > API keys. Then
**Download > Send to Enhancv** creates the tailored resume in your Enhancv account and saves their
designed PDF next to your other files (their API: 60 requests/minute, 1,000/hour; no cover letters).

## Editor features

- **Assistant tab.** Opens on "Here's the line I'd review first" (picked in code: failures first,
  then warnings, then the weakest bullet). Click a line on the CV to focus it, then ask for a
  change. Suggested edits show before and after, the facts they cite and any warning (a number
  not in those facts, no source). Nothing changes until you press Apply.
- **Red and amber highlights.** Red = failed check (likely untrue or unsupported), amber =
  warning (style, recruiter concern).
- **Recruiter checks.** Employment gaps over 3 months, age-bias signals (qualification or role
  dates over 20 years old, personal details), quantified impact (bullets whose own cited fact has
  a number you left out), scope & ownership ("supported", "involved in"), unbacked self-praise,
  and essential criteria with no evidence.
- **ATS keywords.** Short terms the posting uses, with High / Medium / Low priority and a status:
  ✓ on the CV, ↺ in your master CV but not on this CV, ✕ a genuine gap. Dismiss terms that don't
  apply. Coverage is weighted (high 3, medium 2, low 1) against a "ready to send at 85%" marker.
- **Autosave.** Edits save 2.5 seconds after you stop typing; checks and scores refresh without
  moving your cursor. Bullets have ↑ ↓ to reorder within a role.
- **History.** Every save keeps the previous version (last 40; autosaves at most one per 2 minutes). Preview on the page, restore in
  one click; the version you replace is kept too.

For a cover letter, the reviewer agent and the full PDF layout loop, use
**Download ▾ → Copy /apply prompt** and paste it into Claude Code in this repo.
