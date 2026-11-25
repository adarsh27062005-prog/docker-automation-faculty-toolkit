# Plan: repo selection and scoring

Status: **draft**, agreed with the user on 2026-11-26. **Phase 1 (spike) done**; see "Spike results". Phases 2 to 6 are not built yet.

Work happens on branch `feature/repo-scoring`. Tag **`v1.0`** marks the web app before this upgrade. If the upgrade isn't approved, `main` and the live demo stay at `v1.0`.

## Decisions already made

| Question | Decision |
|---|---|
| How many repos per student? | Faculty choose: any number of repos from the student's profile. |
| Score scale | Always out of **100**. |
| Final say | The app **suggests** a score with evidence. Faculty can **override** it and add a note. |
| Rubric | A **default general rubric**, editable per session (checks on/off, points). |
| Domain | **Domain-agnostic.** Judge how an image was built, documented and versioned, never what the app inside does. |

## What Docker Hub lets us see

Checked on 2026-11-26 against public official images. The Hub API v2 is the same one the app already calls, and it does **not** use the image-pull quota.

- `GET /v2/repositories/{ns}/{repo}/`: `description`, `full_description` (the README/overview), `last_updated`, `date_registered`.
- `GET /v2/repositories/{ns}/{repo}/tags`: tags, push times, sizes, platforms (already used).
- `GET /v2/repositories/{ns}/{repo}/tags/{tag}/images`: per platform, the **layer list with the Dockerfile instruction and size of each step** (`RUN …`, `USER …`, `HEALTHCHECK …`, `EXPOSE …`, `CMD …`), plus the image digest.

We do **not** pull or run images. That would need Docker on the server and would execute student code.

## How faculty use it

1. Upload the roster (as today).
2. *(Optional)* Open the **Rubric** panel: class deadline, checks on/off, and points.
3. Choose the repos to evaluate:
   - **Per student:** in the preview, tick any number of repos. For each one, pick a tag (default: the most recent push, or the last one before the deadline), a weight (default 1) and, optionally, its own deadline. Then click **Evaluate**.
   - **In bulk:** **Auto-select** for the whole class, with one of these rules: repo name contains *text*, the most recently pushed repo, or every repo with a pushed image. Faculty can then adjust per student.
4. **Evaluate all** runs as a background job, like Export all, with progress.
5. The students table shows: repos selected, **Score /100**, flags, and a status (*Not selected*, *Scored*, *Overridden*, *Needs re-check*). It can be filtered by status.
6. Faculty open a student to see the per-repo breakdown and evidence, and override where needed.
7. Export: the Excel workbooks gain the marks and the breakdown (see below).

## Scoring one repo (0 to 100)

Every check reports **pass / partial / fail, the points, and evidence** (for example "`USER appuser` at step 9", or "README has no run command").

### Default rubric

| Area | ID | Check | Points |
|---|---|---|---|
| **Submission (20)** | S1 | The selected tag has a pushed image (**gate**: if it doesn't, the repo scores 0, "No image pushed") | 10 |
| | S2 | Pushed on or before the deadline (repo deadline, else class deadline; skipped if neither is set) | 10 |
| **Documentation (20)** | D1 | Short description filled in | 4 |
| | D2 | README present: full points at ≥ 300 characters of text, half at 50 to 299 | 6 |
| | D3 | README shows how to run it (`docker run`, `docker pull` or `docker compose` command) | 6 |
| | D4 | README is structured: at least 2 headings, or a code block | 4 |
| **Build practices (40)**: checked on the student's **own steps** (base image stripped, see Spike results); B2/B3 check the final image | B1 | `CMD` or `ENTRYPOINT` in own steps (half points if only inherited from the base) | 5 |
| | B2 | Final image runs as non-root: the last `USER` isn't `root` or `0` (evidence says if inherited) | 7 |
| | B3 | Final image has a `HEALTHCHECK` | 5 |
| | B4 | `EXPOSE` in own steps (half points if only inherited) | 3 |
| | B5 | Package-manager cleanup in steps that install packages (apt lists removed, `--no-install-recommends`, `apk --no-cache`, `pip --no-cache-dir`, …) | 6 |
| | B6 | `RUN` steps combined: no run of 3 or more consecutive `RUN` layers that could be one | 4 |
| | B7 | `WORKDIR` set | 3 |
| | B8 | `LABEL` metadata (maintainer, version, `org.opencontainers.*`) | 3 |
| | B9 | `COPY` used for local files; `ADD` only for URLs or archives | 4 |
| **Versioning (10)** | V1 | A version tag besides `latest` (for example `v1`, `1.0.2`) | 5 |
| | V2 | Pushed more than once (2 or more distinct push times) | 3 |
| | V3 | Multi-architecture image | 2 |
| **Lean image (10)** | L1 | Minimal base family: alpine, slim, distroless or scratch | 5 |
| | L2 | Compressed size ≤ 500 MB, half points ≤ 1 GB (faculty can change the threshold or switch the check off for heavy domains such as ML) | 5 |

**Penalty:** P1 deducts 10 when one of the student's own steps contains a secret-like value in `ENV`/`ARG`/`RUN` (`PASSWORD=`, `TOKEN=`, `SECRET=`, `API_KEY=`, AWS key pattern, private-key header). Placeholder values (empty, `none`, `**None**`, `changeme`, `${VAR}`) don't count; swagger-ui's `ENV API_KEY=**None**` would otherwise be a false positive. It is always flagged. Evidence shows the variable name only, never the value.

**No own steps (F3):** if nothing is left after stripping the base, the image is a re-tag. All Build practices points are 0, because the base's work can't be credited, and F3 is raised.

**Rules**
- Points from checks that are switched off or skipped (for example S2 when there's no deadline) are shared out proportionally among the remaining checks, so a repo is always scored out of 100.
- A Docker Hub error gives **no score**, the status *Needs re-check*, and no caching. This is the same rule as `data_incomplete` today.
- Pull count and stars are **not** scored, because students can inflate them easily.

### Flags (not scored; faculty investigate)

| ID | Flag |
|---|---|
| F1 | Same image (identical digest or layer set) as another student's selected repo: possible copy |
| F2 | The same image appears in two of this student's selected repos |
| F3 | No build steps of the student's own were detected (a re-tag of a public image) |
| F4 | Pushed after the deadline (also costs S2) |
| F5 | Needs re-check (Docker Hub error) |
| F6 | Secret-like value found (see P1) |

## Student score from several repos

- Student score = **weighted average** of their selected repos' scores (weights default to 1).
- If any selected repo is *Needs re-check*, the score is shown as **provisional** until it's evaluated again.
- Faculty can override a **repo** score or the **student** total, with a note. The marks sheet keeps both *suggested* and *final*.

## Main technical risk: base image steps vs the student's own steps (Phase 1 spike)

The layer list includes the **base image's** steps. For example, `python:3.11-slim` already has a `CMD` and cleans apt caches, so without a split, students would get B1/B5 for free.

Options to test in the spike:
1. **Registry image config** (`history[].created`): the base image's steps are older, and the student's own steps cluster at their build time. This is accurate, but a manifest `GET` counts as a pull against the anonymous per-IP limit. Check current limits, and consider an optional `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` setting to raise them.
2. **Hub-only heuristics:** no pull quota, but less certain.

**Acceptance:** on at least 5 public images built `FROM` common bases (python, node, nginx, alpine, openjdk), the split must be correct. Where we can't separate reliably, the check falls back to "final image has X", says so in the evidence, and starts with lower default points. Spike notes go in this file. The spike uses public images only, never real student data.

## Spike results (2026-11-26)

The script is `scripts/spike_layer_split.py` (live Docker Hub, public images only).

**Limits measured**

| API | Limit | Consequence |
|---|---|---|
| Registry (`registry-1.docker.io`) | **100 anonymous pulls per hour per IP** (`ratelimit-limit: 100;w=3600`) | **Option 1 rejected.** A campus shares one IP, so one class run could use it up. Scoring uses the Hub API only. |
| Hub API (`hub.docker.com/v2`) | **180 requests per window** (`x-ratelimit-limit/remaining/reset` headers) | Workable. Pace with the headers, keep the semaphore, cache. |

**Data confirmed:** the tag images endpoint gives each step's instruction and size per platform. Repo detail gives `description` and `full_description` for user repos too (swagger-ui: a 7,121-character README). Platform lists include `unknown/unknown` attestation entries, which must be skipped.

**Base/own split approaches compared (15 public images)**

| Approach | Correct | Notes |
|---|---|---|
| `# buildkit` marker | No | Official images are built with BuildKit too. |
| Naive: own steps start after the last `CMD`/`ENTRYPOINT` followed by more steps | **2 of 11** checkable | Fails on `nginx:alpine` (official steps after its CMD), re-tags, `FROM scratch`, and multi-level bases. |
| **Official-base prefix match** (chosen) | **15 of 15** | See the method below. |

**Chosen method**
1. Find the last official-image signature ENV (`PYTHON_VERSION`, `NGINX_VERSION`, `NODE_VERSION`, `JAVA_VERSION`, `PHP_VERSION`, `GOLANG_VERSION`, … 14 families). This names the most-derived official base.
2. List that official repo's tags with `?name=<version>` (falling back to `major.minor`, then `major`), filtered by variant (alpine vs debian codename). Fetch up to 8 candidates' step lists.
3. Pick the **longest candidate whose normalised steps are a prefix** of the image's steps. Normalising replaces digits and hashes, drops `# buildkit` and BuildKit's `RUN |N ARG=…` prefix, adds the `RUN` keyword missing from classic-builder steps, and treats any OS root layer as equal. This lets a student's image built on an *older* base build still match.
4. No signature: strip only the OS root (alpine/debian/ubuntu/busybox root and its `CMD`; Ubuntu has `ARG`/`LABEL` steps before its root). No root at all means `FROM scratch`, and every step is the student's own.

**Confidence** is shown in the evidence:
- *high*: an official base was matched, or `FROM scratch`.
- *medium*: OS root only. Steps from a non-official intermediate base would count as the student's own; this is rare for students.
- *low*: a signature was found but no candidate matched. Build checks then run on the whole image and are marked "estimated".

**Cost:** 65 Hub API calls for 15 images, about 4 to 5 extra per **distinct** base. Base step lists are cached for the process (official version tags rarely change), so a class sharing a few bases costs little. Rough budget per selected repo: 1 repo detail + 1 tag images, plus the shared base look-ups. 60 students × 1 repo ≈ 140 calls (one window); × 2 repos ≈ 260 calls (paced over two windows).

**Changes to the plan from the spike**
- Build checks run on the student's own steps. B1/B4 give half points when only inherited; B2/B3 judge the final image. A re-tag gets 0 for Build practices plus F3.
- L1 (minimal base) uses the matched base name (`-slim`, `-alpine`, alpine root, scratch), which is now reliable.
- P1 ignores placeholder values.
- **Architecture:** students on Apple Silicon often push **arm64 only**. Use the image's own platform (prefer `linux/amd64`, else whatever exists) for both the image and its base candidates.
- The client must read `x-ratelimit-remaining`/`reset` and wait instead of failing, and an evaluate-all job must show "waiting for Docker Hub rate limit".

## API (draft)

Every route looks up the student in the roster first, and repo names must come from that student's fetched repo list. Arbitrary input is never forwarded to Docker Hub.

| Method | Path | Purpose |
|---|---|---|
| GET / PUT | `/api/roster/{id}/rubric` | Read or edit the rubric (deadline, checks, points, size threshold) |
| PUT | `/api/roster/{id}/students/{docker_id}/selection` | `[{repo, tag, weight, deadline?}]` |
| POST | `/api/roster/{id}/auto-select` | Apply a class-wide selection rule |
| POST | `/api/roster/{id}/students/{docker_id}/evaluate` | Score the selected repos and return the breakdown |
| POST | `/api/roster/{id}/evaluate-all/start` + job polling | Background scoring for the class |
| PUT | `/api/roster/{id}/students/{docker_id}/override` | `{repo?, score, note}` |

## State, rules and limits

- The rubric, selections, scores and overrides live **in memory on the roster entry**, with the roster's 12 h TTL. A server restart loses them, so the **exported marks sheet is the record**. Re-importing a marks sheet to restore a session is a later item.
- Nothing is written to disk. Every string into Excel goes through `set_safe_cell`. README text is used only for checks and short evidence snippets.
- Docker Hub politeness: keep the 3-request semaphore and backoff. Tag-image results are cached for 15 min, and errors are never cached.
- Single uvicorn worker (unchanged).
- The GitHub Pages demo (`scripts/pages_demo/demo-api.js` and `build_pages_demo.py`) needs synthetic rubric, selection and evaluation responses so the demo keeps working.

## Excel output

- **Single student** workbook: a new **Score** sheet with one block per evaluated repo (check, result, points, evidence) and the weighted total, suggested and final.
- **Combined** workbook:
  - A **Marks** sheet: Roll, Name, Docker ID, repos evaluated, suggested /100, final /100, override note, flags.
  - A **Repo scores** sheet: one row per student × repo, with a column per check.

## UI

- A **Rubric** panel (a collapsible card above the students table).
- Preview modal: repo checkboxes with tag, weight and deadline controls; **Evaluate**; a per-repo breakdown with evidence; override inputs.
- Table: a score badge, flag icons, and a status filter. The mobile card layout keeps its `data-label`s.
- **Auto-select** and **Evaluate all** buttons, with job progress.
- Stays inside the Amrita theme (CSS variables in `:root`).

## Phases

| # | Phase | Done when |
|---|---|---|
| 1 | **Spike**: base-vs-own step split, pull-quota cost, README and layer fetch | ✅ Done 2026-11-26: official-base prefix match, 15/15; Hub API only |
| 2 | **Scoring engine** `app/scoring.py`: pure functions over fetched data, rubric model, redistribution, penalties, flags | Offline tests with fixture layer lists for every check |
| 3 | **Fetching and API**: repo detail and tag images in `dockerhub.py`, selection, auto-select, evaluate, evaluate-all job, override | Offline API tests (mocked `_fetch`), roster look-up enforced |
| 4 | **UI**: rubric panel, repo picker, breakdown, override, table columns and filter | Browser check at desktop and 375 px, no console errors |
| 5 | **Excel**: Score sheet, Marks and Repo scores sheets | Workbooks open; formula guard covered by tests |
| 6 | **Demo and docs**: Pages demo data, README, CLAUDE.md, `antigravity.md` §10 | Pages build passes; live demo works end to end |

## Out of scope for v1

- Pulling or running images, or testing what the app does (domain-specific).
- Persisting sessions across server restarts.
- Analysing the GitHub source (`GITHUB_TOKEN` enrichment stays unwired).

## Open questions (decide during the build)

- Late policy: is losing S2's 10 points enough, or should a late repo also lose a percentage of its total?
- Is 500 MB / 1 GB a fair default for L2?
- Should a flag such as F1 (possible copy) block the suggested score until faculty review it, or only show a warning?
