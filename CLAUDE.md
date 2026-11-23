# CLAUDE.md — Docker Hub Submission Evaluator

Working guide for Claude Code in this repo. **Keep the "Work log" and "Open items" sections current**: add an entry whenever a change lands, so any new session knows what has been done and what is next.

## What this is
A FastAPI web app for faculty to grade students' Docker Hub submissions. Faculty upload a roster (`.xlsx`). The app fetches each student's public repos and tags live from Docker Hub, shows a preview, and exports Excel reports (single student, or all students via a background job). No database, no auth, everything in process memory, and dates are shown in IST.

Built originally with Antigravity. Its history and design notes are in `antigravity.md`. **The code is the source of truth**: Antigravity docs drifted in places, so check claims against the code before relying on them.

## Commands
```powershell
python -m pytest -q                                              # offline test suite (67 tests, ~8 s)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload   # dev server -> http://127.0.0.1:8000
```
- **Restart the server after Python changes.** `index.html` is read on every request, so UI edits show up even on a stale server, but routes and fixes don't. A server without `--reload` silently runs old code (on 2026-11-19 an 18:31 server on port 8000 returned 404 for the new logos). Use `--reload` in development.
- Dependencies are installed in the **global Python 3.11** (no `.venv` yet). They are pinned in `requirements.txt`.
- Browser checks use the user-level Playwright skill (`~/.claude/skills/playwright`). Proven pattern:
  `python <skill>/scripts/with_server.py --server "python -m uvicorn app.main:app --host 127.0.0.1 --port 8765" --port 8765 -- python flow.py`
  Put throwaway flow scripts and screenshots in the scratchpad, not in the repo.
- UI/design work: the user-level **UI/UX Pro Max** skill (`~/.claude/skills/ui-ux-pro-max`, upstream nextlevelbuilder/ui-ux-pro-max-skill v2.15.0, MIT) is installed. It provides design-system search via `scripts/search.py`. Keep its suggestions inside the Amrita theme below (crimson/yellow, Montserrat); don't let it replace the college branding.
- The user-level **Frontend Design** skill (`~/.claude/skills/frontend-design`, Anthropic's official anthropics/skills, Apache 2.0) is also installed. Its "distinctive, non-templated" guidance applies inside the fixed Amrita brief: the college theme is the brief, and the skill says the brief's own words always win.
- **Impeccable** (`~/.claude/skills/impeccable`, pbakaus/impeccable v4.10.0, Apache 2.0) is installed. This app is its **Operate** mode (tools/dashboards: precision and restraint over expression). Its engine binary is downloaded from the project's GitHub releases on first use and has **not** been fetched yet, so ask the user before the first run. Don't enable `/impeccable hooks` without asking.
- **TasteSkill** (`~/.claude/skills/design-taste-frontend`, Leonxlnx/taste-skill 2.5.1, MIT) is installed. It targets marketing/company websites (one section per screen, real photos, a figma.com/x.ai bar), which is a poor fit for this faculty tool, so don't apply its house rules here unless the user asks. While it's active, a Stop hook runs `gate.mjs`, which only blocks during a TasteSkill build (`.taste-skill/build.json`).

## Code map
| File | Role |
|---|---|
| `app/main.py` | FastAPI routes (roster upload, preview, single export, export-all job start/poll/download, template, health) |
| `app/roster.py` | In-memory `.xlsx` roster parsing (header aliases, URL→ID normalisation, skip reasons) + `RosterStore` (LRU 50, TTL 12 h) |
| `app/dockerhub.py` | Async Docker Hub client: pagination, retries/backoff, submission-date basis, `data_incomplete` flagging, caching rules |
| `app/excel.py` | Workbook builders (single, combined, template), `set_safe_cell` formula-injection guard, `content_disposition` |
| `app/jobs.py` | Background export-all jobs (2 students at a time, results kept 2 h, strong task refs) |
| `app/cache.py` | Process-local TTL cache (15 min) for student profiles |
| `app/github_links.py` | GitHub URL discovery (overrides.csv → README → description). `enrich_github_repo` exists but is **not called** |
| `app/config.py` | Env-var settings (see `.env.example`) |
| `app/parser.py` | Legacy `docker_id.md` parser. Unused by the app, kept with its tests |
| `app/static/index.html` | Single-page vanilla JS UI (no build step, no CDN), Amrita-themed |
| `app/static/img/` | Branding: `amrita-logo.svg`, `iic-logo.png` (IIC, resized to 600px), `favicon.svg`. Only this folder is mounted (`/static/img`) |
| `tests/` | pytest, all network mocked; `test_reliability_fixes.py` covers post-handoff fixes |
| `scripts/`, `scratch/*.py` | One-off survey/verification scripts from the Antigravity phases (hit live Docker Hub) |

## Rules to keep
- **Never write uploads or reports to disk.** Parse and build in memory (`io.BytesIO`).
- **Every string written to Excel goes through `set_safe_cell`** (formula-injection guard).
- **Downloads use `content_disposition()`** so non-ASCII filenames work.
- **Never forward arbitrary input to Docker Hub.** Look the student up in the roster first (404 otherwise).
- **Don't report a Docker Hub failure as a real result.** A failed fetch sets `data_incomplete` and an `error_note`, and anything with an `error_note` is not cached.
- Docker Hub politeness: keep the semaphore (3 requests) and backoff; don't raise concurrency without testing against rate limits.
- Run a **single uvicorn worker**: rosters, jobs and the cache live in process memory.
- No hardcoded course/batch data (`23CYS`, `20CYS402`, `59`) in `app/`. `docker_id.md` and `IMPLEMENTATION_PLAN.md` are read-only history.
- Tests must stay offline. Mock `DockerHubClient._fetch`.
- **The repo is public** (github.com/adarsh27062005-prog/docker-automation-faculty-toolkit). **Never commit real student data** (names, roll numbers, Docker IDs, exports). Samples, tests and docs use synthetic `Demo Student NN` / `DEMO.U4CYS23NNN` / `demo-student-NN`. Real exports stay in git-ignored `scratch/exports/`. README screenshots in `docs/screenshots/` must also use demo data.
- Git: branch `main`. The user asks before commits. Commit identity comes from the global git config (user chose to keep it).

## Hosting (GitHub only, user's choice: no Render)
- **Full app:** GitHub Codespaces via `.devcontainer/devcontainer.json` (Python 3.11 image, `pip install --user -r requirements.txt`, auto-starts uvicorn on 8000 and opens the browser). Link: https://codespaces.new/adarsh27062005-prog/docker-automation-faculty-toolkit?quickstart=1
- **Live demo:** GitHub Pages at https://adarsh27062005-prog.github.io/docker-automation-faculty-toolkit/, built by `.github/workflows/pages.yml` (tests → `scripts/build_pages_demo.py _site` → deploy) on every push to `main`. Pages can't run Python, and Docker Hub sends no CORS headers (browser fetch is blocked), so the demo answers `/api/*` in the browser via `scripts/pages_demo/demo-api.js` with synthetic data and pre-built demo workbooks. It never reads uploaded files.
- `index.html` must keep working in both: use relative-safe fetch calls; the build rewrites `/static/img/` and `/api/template` and fails loudly if an expected string is missing. Downloads go through `saveResponseAsFile()` (fetch + blob), never `window.location`, so the demo can intercept them.
- Optional shared login (`BASIC_AUTH_USERNAME`/`BASIC_AUTH_PASSWORD`, middleware in `main.py`) stays available for any public server; it's off by default.

## UI theme (Amrita Vishwa Vidyapeetham)
Taken from amrita.edu campus/school pages (visited with Playwright, 2026-11-19):
- Crimson `#af0c3e` (dark `#8e0a33`, bright `#c8104c`; logo `#b41e49`), accent yellow `#ffd92a` (pill buttons with dark text), soft pink page background `#fdf8f9`, tint `#fbeff3`, headings `#262626`, body grey `#656565`.
- Montserrat font (500/600/700). It's listed first in the font stack, but **not bundled yet**, so machines without it fall back to Segoe UI or the system font.
- Patterns: crimson top strip ("Amrita Vishwa Vidyapeetham" left, "IIC-Initiative" right) → white logo bar (Amrita left, "Faculty - Toolkit" label centre with yellow underline, IIC right) → crimson hero with yellow/white triangle graphics; section titles in crimson with a short yellow bar above; pill buttons; crimson footer rule.
- Colours are CSS variables in `:root` of `index.html`. Change them there, not inline.
- Phone width (≤720px): the students table turns into stacked cards (`data-label` on each `td`).

## Docs in the repo
- `README.md`: user-facing setup and configuration. Update it when behaviour or config changes.
- `antigravity.md`: architecture record and history. Section 10 lists post-handoff changes. Append to it for significant changes.
- `FRAMEWORKS.md`: tech-stack write-up (user-added, partly stale: wrong FastAPI/Uvicorn versions, mentions pydantic-settings, says 46 tests).
- `IMPLEMENTATION_PLAN.md`: original spec (historical).

## Open items
- Next web-app updates: *to be defined with the user.*
- Montserrat is not bundled locally (offline campus machines fall back to system fonts).
- Excel reports still use the old blue header fill (`1F4E78`), not the crimson theme.
- `GITHUB_TOKEN` has no effect: GitHub enrichment is not wired in.
- `FRAMEWORKS.md` has stale details (user chose to leave it for now).
- No `.venv`; dependencies live in the global Python.

## Work log
| Date | Commit | Change |
|---|---|---|
| 2026-11-19 | `b46b8fb` | Baseline: app as handed over from Antigravity (46 tests). |
| 2026-11-19 | `1b91ab8` | Fixes: failed/partial Docker Hub fetches flagged, not shown as "No image pushed", and not cached; background tasks kept alive; Unicode-safe download filenames; overrides read once per student; IST date in filenames; docs synced. 55 tests. |
| 2026-11-19 | — | Added user-level Playwright skill; verified upload → skipped rows → single Excel download → mobile layout in a real browser. |
| 2026-11-19 | `392aa1b` | Added this CLAUDE.md. |
| 2026-11-19 | `92f8395` | Amrita/IIC branding: logos served from `/static/img`, UI restyled to the amrita.edu theme, mobile card layout for the students table, favicon; `tests/test_branding.py` (58 tests). Verified desktop + 375px in Playwright. |
| 2026-11-20 | — | Installed UI/UX Pro Max skill (user level + claude.ai package). Not part of this repo. |
| 2026-11-20 | — | Installed Frontend Design skill (user level + claude.ai package). Not part of this repo. |
| 2026-11-20 | — | Installed Impeccable and TasteSkill (user level + claude.ai packages). Not part of this repo. |
| 2026-11-20 | (all) | History rewritten with `git filter-branch` to replace the 59 real students with synthetic data and drop `scripts/survey_output/`, so commit IDs changed (baseline is now `b46b8fb`). The pre-rewrite history exists only as a local bundle backup. |
| 2026-11-20 | — | New README (features, screenshots with demo data, quick start, config, architecture, API, security) and push to GitHub. |
| 2026-11-23 | — | GitHub-only hosting: Codespaces devcontainer + GitHub Pages demo (Actions workflow, in-browser demo API, demo workbooks). Render config removed. Optional Basic-auth middleware (`tests/test_basic_auth.py`). Export-all download now uses fetch+blob. Fixed `Demo_Student_09` → `Demo Student 09` in the samples. README links now point to the live demo and Codespaces. 67 tests. |
