# CLAUDE.md — Docker Hub Submission Evaluator

Working guide for Claude Code in this repo. **Keep the "Work log" and "Open items" sections current**: add an entry whenever a change lands, so any new session knows what has been done and what is next.

## What this is
A FastAPI web app for faculty to grade students' Docker Hub submissions. Faculty upload a roster (`.xlsx`). The app fetches each student's public repos and tags live from Docker Hub, shows a preview, and exports Excel reports (single student, or all students via a background job). No database, no auth, everything in process memory, and dates are shown in IST.

Built originally with Antigravity. Its history and design notes are in `antigravity.md`. **The code is the source of truth**: Antigravity docs drifted in places, so check claims against the code before relying on them.

## Commands
```powershell
python -m pytest -q                                              # offline test suite (55 tests, ~8 s)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload   # dev server -> http://127.0.0.1:8000
```
- Dependencies are installed in the **global Python 3.11** (no `.venv` yet). They are pinned in `requirements.txt`.
- Browser checks use the user-level Playwright skill (`~/.claude/skills/playwright`). Proven pattern:
  `python <skill>/scripts/with_server.py --server "python -m uvicorn app.main:app --host 127.0.0.1 --port 8765" --port 8765 -- python flow.py`
  Put throwaway flow scripts and screenshots in the scratchpad, not in the repo.

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
| `app/static/index.html` | Single-page vanilla JS UI (no build step, no CDN) |
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
- Git: branch `main`. The user asks before commits. Commit identity comes from the global git config (user chose to keep it).

## Docs in the repo
- `README.md`: user-facing setup and configuration. Update it when behaviour or config changes.
- `antigravity.md`: architecture record and history. Section 10 lists post-handoff changes. Append to it for significant changes.
- `FRAMEWORKS.md`: tech-stack write-up (user-added, partly stale: wrong FastAPI/Uvicorn versions, mentions pydantic-settings, says 46 tests).
- `IMPLEMENTATION_PLAN.md`: original spec (historical).

## Open items
- Next web-app updates: *to be defined with the user.*
- `GITHUB_TOKEN` has no effect: GitHub enrichment is not wired in.
- `FRAMEWORKS.md` has stale details (user chose to leave it for now).
- No `.venv`; dependencies live in the global Python.

## Work log
| Date | Commit | Change |
|---|---|---|
| 2026-11-19 | `84a4220` | Baseline: app as handed over from Antigravity (46 tests). |
| 2026-11-19 | `462c255` | Fixes: failed/partial Docker Hub fetches flagged, not shown as "No image pushed", and not cached; background tasks kept alive; Unicode-safe download filenames; overrides read once per student; IST date in filenames; docs synced. 55 tests. |
| 2026-11-19 | — | Added user-level Playwright skill; verified upload → skipped rows → single Excel download → mobile layout in a real browser. |
| 2026-11-19 | — | Added this CLAUDE.md. |
