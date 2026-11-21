# CV Tailor

A local web app that tailors your CV to a job advert, without inventing anything. Every line on a tailored CV
cites a fact from your own master CV, and changed or made-up numbers are flagged. It runs on your own PC, on top
of the [ai-job-search](https://github.com/MadsLorentzen/ai-job-search) framework (included here; see
`README-ai-job-search.md` and `LICENSE-ai-job-search`).

## What it does

- **Tailor and review**: match score against the advert's criteria, keyword coverage, recruiter checks
  (first 10 seconds, gaps, interview risks), UK English spelling and grammar, ATS checks, with problems shown on
  the CV itself and one-click fixes.
- **Design**: five ATS-safe templates, font, size, spacing and margin controls, a page map, and a Key Achievements
  section. The Word file follows the same settings.
- **Find jobs**: NHS Jobs, Work Hub, Reed, Adzuna, Arbeitnow, company boards, KTP Associate roles (with a Global
  Talent visa check) and more, scored out of 100 with reasons, ghost-job checks and visa sponsor checks. Describe the
  job you want in one sentence and it sets the filters.
- **Interview prep**: company research with a source on every point, likely questions with suggested answers from
  your facts, practice exercises.
- **LinkedIn checklist** against the skills your target jobs ask for.
- **Edit from Claude**: `webui/mcp_server.py` is an MCP server, so Claude Code or the Claude desktop app can read and
  edit your CVs through the app.

## Setup

1. Clone this repo.
2. Optional: copy `webui/.env.example` to `webui/.env` and add any keys you want.
3. Run `start_webui.bat` (Windows) or `./start_webui.sh`, then open http://127.0.0.1:8765 and upload your master CV.

To use it from Claude Code:

```
claude mcp add cv-tailor -- python "<path to>/webui/mcp_server.py"
```

Your CVs, facts, applications and keys stay on your machine; `.gitignore` keeps them out of git.
