"""Build the static GitHub Pages demo of the Faculty Toolkit.

    python scripts/build_pages_demo.py [output_dir]   (default: _site)

GitHub Pages can only serve static files, and Docker Hub's API does not allow
browser requests from other sites, so the live app needs its Python server
(run it locally or on GitHub Codespaces). This script produces a demo that runs
the real interface against synthetic data:

* index.html is the real app page with paths made relative, a demo banner, and
  demo/demo-api.js loaded first. That script answers the page's /api/* calls in
  the browser.
* Every student, repository, date and pull count is generated from the bundled
  demo roster (sample_roster.xlsx, synthetic names only).
* Excel downloads are real workbooks built by app.excel from that demo data,
  scored with the default rubric (each student's latest pushed repo).
* Scoring: every demo repo gets a synthetic image (a few Dockerfile patterns)
  scored by the real app.scoring code at build time. In the browser,
  demo-api.js only redoes the rubric arithmetic (points, on/off, deadline,
  size limit, penalties), so rubric edits and overrides behave like the app.

No uploaded file is ever read by the demo.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.dockerhub import RepoSubmission, StudentProfileSummary, TagInfo  # noqa: E402
from app.evaluation import RepoEval, RosterScoring, Selection, StudentEval, student_detail  # noqa: E402
from app.layers import BaseSplit, Step  # noqa: E402
from app.scoring import FLAGS, RepoFacts, Rubric, catalogue, score_repo  # noqa: E402
from app.excel import (  # noqa: E402
    build_combined_workbook,
    build_student_workbook,
    generate_template_workbook,
    make_student_filename,
    workbook_to_bytes,
)
from app.roster import parse_roster_excel  # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "adarsh27062005-prog/docker-automation-faculty-toolkit")
CODESPACES_URL = f"https://codespaces.new/{REPO}?quickstart=1"
REPO_URL = f"https://github.com/{REPO}"

REPO_POOL = [
    "flask-todo-api", "nginx-reverse-proxy", "redis-cache-demo", "node-express-app",
    "postgres-backup", "spring-boot-hello", "react-frontend", "mysql-init",
    "python-ml-serve", "go-microservice", "rabbitmq-worker", "static-site-nginx",
    "django-blog", "fastapi-inventory", "mongo-seed", "kafka-consumer",
]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def demo_profile(index: int, roll: str, name: str, docker_id: str) -> StudentProfileSummary:
    """Deterministic synthetic Docker Hub data for one demo student."""
    rng = random.Random(1000 + index)
    did = docker_id.lower()
    base = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    names = rng.sample(REPO_POOL, rng.randint(2, 5))
    subs: list[RepoSubmission] = []
    for n in names:
        created = base + timedelta(days=rng.randint(0, 80), minutes=rng.randint(0, 600))
        empty = rng.random() < 0.15
        tags: list[TagInfo] = []
        if not empty:
            pushed = created + timedelta(hours=rng.randint(1, 48))
            for t in range(rng.randint(1, 3)):
                tags.append(TagInfo(
                    name="latest" if t == 0 else f"v1.{t}",
                    tag_last_pushed=iso(pushed),
                    last_updated=iso(pushed),
                    full_size=rng.randint(20, 400) * 1024 * 1024,
                    architecture="amd64",
                    os="linux",
                ))
                pushed += timedelta(days=rng.randint(1, 12), minutes=rng.randint(0, 300))
        pushes = [t.tag_last_pushed for t in tags]
        subs.append(RepoSubmission(
            name=n,
            namespace=did,
            repo_url=f"https://hub.docker.com/r/{did}/{n}",
            description="",
            pull_count=0 if empty else rng.randint(5, 180),
            star_count=rng.randint(0, 2),
            date_registered=iso(created),
            last_updated=max(pushes) if pushes else iso(created),
            first_tag_pushed=min(pushes) if pushes else None,
            latest_tag_pushed=max(pushes) if pushes else None,
            submission_date=max(pushes) if pushes else None,
            tags_count=len(tags),
            github_url="Not found",
            notes="No image pushed" if empty else "",
            tags=tags,
        ))
    subs.sort(key=lambda r: (r.submission_date is not None, r.submission_date or ""), reverse=True)
    return StudentProfileSummary(
        roll=roll, name=name, docker_id=docker_id, docker_id_lower=did, user_found=True,
        total_repos=len(subs), total_pulls=sum(s.pull_count for s in subs),
        repos_with_github=0, submissions=subs, error_note="",
    )


def preview_json(p: StudentProfileSummary) -> dict:
    """Same shape as GET /api/roster/{id}/students/{docker_id}/preview."""
    return {
        "roll": p.roll, "name": p.name, "docker_id": p.docker_id, "user_found": p.user_found,
        "total_repos": p.total_repos, "total_pulls": p.total_pulls,
        "repos_with_github": p.repos_with_github, "github_links": [], "error_note": p.error_note,
        "repos": [
            {"name": s.name, "url": s.repo_url, "submission_date": s.submission_date,
             "pulls": s.pull_count, "notes": s.notes,
             "tags": [{"name": t.name, "pushed": t.tag_last_pushed}
                      for t in sorted(s.tags, key=lambda t: t.tag_last_pushed or "", reverse=True)]}
            for s in p.submissions
        ],
    }


# --------------------------------------------------------------------------
# Synthetic images for scoring (Dockerfile patterns students commonly push)
# --------------------------------------------------------------------------
PY_SLIM = [
    "# debian.sh --arch 'amd64' out/ 'trixie' '@1795392000'",
    "ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
    "ENV LANG=C.UTF-8",
    "RUN /bin/sh -c set -eux; apt-get update; apt-get install -y --no-install-recommends ca-certificates tzdata; rm -rf /var/lib/apt/lists/* # buildkit",
    "ENV PYTHON_VERSION=3.11.18",
    "RUN /bin/sh -c set -eux; apt-get install -y --no-install-recommends gcc; make install; rm -rf /var/lib/apt/lists/* # buildkit",
    'CMD ["python3"]',
]
NGINX_ALPINE = [
    "ADD alpine-minirootfs-3.24.4-x86_64.tar.gz / # buildkit",
    'CMD ["/bin/sh"]',
    "ENV NGINX_VERSION=1.31.10",
    "RUN /bin/sh -c set -x && apk add --no-cache nginx # buildkit",
    'ENTRYPOINT ["/docker-entrypoint.sh"]',
    "EXPOSE map[80/tcp:{}]",
    'CMD ["nginx" "-g" "daemon off;"]',
]
NODE_ALPINE = [
    "ADD alpine-minirootfs-3.23.4-x86_64.tar.gz / # buildkit",
    'CMD ["/bin/sh"]',
    "ENV NODE_VERSION=20.20.2",
    "RUN /bin/sh -c addgroup -g 1000 node && adduser -u 1000 -G node -D node && apk add --no-cache libstdc++ # buildkit",
    'ENTRYPOINT ["docker-entrypoint.sh"]',
    'CMD ["node"]',
]
UBUNTU = [
    "ARG RELEASE", "ARG LAUNCHPAD_BUILD_ARCH", "LABEL org.opencontainers.image.version=24.04",
    "ADD file:54dafd97985c3ec227e2c96be9290657cf2ab456885b5fc8a24c9278bca65e60 in /", 'CMD ["/bin/bash"]',
]
TEMPLATES = [
    # name, base steps, base, confidence, own steps, size MB range
    ("python-good", PY_SLIM, "python:3.11.18-slim", "high", [
        "WORKDIR /app", "LABEL maintainer=demo org.opencontainers.image.version=1.0",
        "COPY requirements.txt . # buildkit", "RUN /bin/sh -c pip install --no-cache-dir -r requirements.txt # buildkit",
        "COPY . . # buildkit", "RUN /bin/sh -c useradd -m appuser # buildkit", "USER appuser",
        "EXPOSE map[8000/tcp:{}]", "HEALTHCHECK {Test:[CMD-SHELL curl -f http://localhost:8000/ || exit 1]}",
        'CMD ["python" "app.py"]'], (55, 140)),
    ("python-basic", PY_SLIM, "python:3.11.18-slim", "high", [
        "WORKDIR /app", "COPY . . # buildkit", "RUN /bin/sh -c pip install -r requirements.txt # buildkit",
        "EXPOSE map[5000/tcp:{}]", 'CMD ["python" "app.py"]'], (60, 180)),
    ("nginx-static", NGINX_ALPINE, "nginx:1.31.10-alpine", "high", [
        "COPY index.html /usr/share/nginx/html/ # buildkit", "COPY style.css /usr/share/nginx/html/ # buildkit"], (20, 30)),
    ("node-app", NODE_ALPINE, "node:20.20.2-alpine", "high", [
        "WORKDIR /usr/src/app", "COPY package*.json ./ # buildkit",
        "RUN /bin/sh -c npm ci --omit=dev && npm cache clean --force # buildkit", "COPY . . # buildkit",
        "USER node", "EXPOSE map[3000/tcp:{}]", 'CMD ["node" "server.js"]'], (45, 90)),
    ("ubuntu-sloppy", UBUNTU, "rootfs root", "medium", [
        "RUN /bin/sh -c apt-get update # buildkit", "RUN /bin/sh -c apt-get install -y python3 python3-pip # buildkit",
        "RUN /bin/sh -c pip3 install flask # buildkit", "ADD app.py /app/ # buildkit",
        "ENV SECRET_KEY=demo-secret-not-real-123", 'CMD ["python3" "/app/app.py"]'], (180, 420)),
    ("retag", NGINX_ALPINE, "nginx:1.31.10-alpine", "high", [], (20, 22)),
]
READMES = [
    "# {repo}\n\nCourse assignment image. It starts a small web service and keeps no state between requests, "
    "so it can be restarted at any time.\n\n## Run\n\n```\ndocker run -p 8000:8000 {did}/{repo}:latest\n```\n\n"
    "## Notes\n\nBuilt from the Dockerfile in the course repository. Logs go to standard output.\n",
    "# {repo}\n\nAssignment submission. Pull it with docker pull {did}/{repo} and run it.\n",
    "",
]


def _digest(*parts: str) -> str:
    return "sha256:" + hashlib.sha256("|".join(parts).encode()).hexdigest()


def demo_facts(index: int, sub: RepoSubmission, rng: random.Random, digest_key: str) -> RepoFacts:
    tags = [{"name": t.name, "pushed": t.tag_last_pushed} for t in sub.tags]
    latest = max((t for t in sub.tags if t.tag_last_pushed), key=lambda t: t.tag_last_pushed, default=None)
    facts = RepoFacts(repo=sub.name, tag=latest.name if latest else None, tags=tags,
                      description="" if rng.random() < 0.25 else f"Course assignment: {sub.name.replace('-', ' ')}")
    if latest is None:
        return facts
    pick = rng.choice([0, 0, 1, 1, 2, 3, 3, 4, 5])
    if digest_key == "shared-image":
        pick = 0
    name, base, base_name, conf, own, (lo, hi) = TEMPLATES[pick]
    steps = []
    for i, ins in enumerate(base + own):
        layer = ins.split(" ", 1)[0] in ("RUN", "COPY", "ADD", "#")
        key = base_name if i < len(base) else digest_key
        steps.append(Step(ins, size=rng.randint(1, 9) * 1_000_000 if layer else 0,
                          digest=_digest(key, str(i), ins) if layer else None))
    facts.readme = rng.choice(READMES).format(repo=sub.name, did=sub.namespace)
    facts.platforms = ["linux/amd64", "linux/arm64"] if rng.random() < 0.3 else ["linux/amd64"]
    facts.platform = "linux/amd64"
    facts.image_size = rng.randint(lo, hi) * 1024 * 1024
    facts.steps = steps
    facts.split = BaseSplit(len(base), base_name, conf)
    facts.pushed_at = latest.tag_last_pushed
    facts.has_image = True
    return facts


def compact(facts: RepoFacts) -> dict:
    """What demo-api.js needs to rescore a repo under any rubric."""
    r = score_repo(facts, Rubric.default())
    return {
        "has_image": facts.has_image, "base": r.base, "confidence": r.confidence, "own_steps": r.own_steps,
        "total_steps": r.total_steps, "platform": r.platform, "size_mb": round(facts.image_size / 1048576),
        "flags": [f for f in r.flags if f in ("F3", "F6")],
        "secrets": [p.split("(", 1)[1].rsplit(")", 1)[0] if "(" in p else p for p in r.penalties if p.startswith("Secret")],
        "fingerprint": ",".join(r.fingerprint) if r.fingerprint else None,
        "checks": [[c.id, c.fraction, c.status, c.evidence] for c in r.checks],
    }


BANNER_CSS = """
    .demo-banner { background: var(--yellow); color: #1a1a1a; font-size: 0.86rem; font-weight: 600; }
    .demo-banner .wrap { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 16px; padding-top: 10px; padding-bottom: 10px; }
    .demo-banner a { color: var(--crimson-dark); white-space: nowrap; }
    .demo-banner a:hover { color: #000; }
"""

BANNER_HTML = f"""  <div class="demo-banner" role="note">
    <div class="wrap">
      <span>Demo mode: this page runs on GitHub Pages with made-up sample data. Uploading any .xlsx loads the built-in demo class; your file is never read or sent anywhere. Scoring works on synthetic images; Excel downloads are pre-built samples.</span>
      <a href="{CODESPACES_URL}" target="_blank" rel="noopener">Run the full app on GitHub Codespaces &rarr;</a>
    </div>
  </div>
"""


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "demo").mkdir(parents=True)
    (out / "demo-files" / "students").mkdir(parents=True)

    # Branding assets
    shutil.copytree(ROOT / "app" / "static" / "img", out / "static" / "img")

    # Demo class from the bundled synthetic roster
    roster = parse_roster_excel((ROOT / "sample_roster.xlsx").read_bytes(), "sample_roster.xlsx")
    students, previews, filenames, profiles, scoring_data = [], {}, {}, [], {}
    scoring = RosterScoring()
    for i, s in enumerate(roster.students, start=1):
        p = demo_profile(i, s.roll, s.name, s.docker_id)
        profiles.append(p)
        key = s.docker_id.lower()
        students.append(s.to_dict())
        previews[key] = preview_json(p)
        rng = random.Random(5000 + i)
        repo_facts = {}
        for j, sub in enumerate(p.submissions):
            # Students 7 and 8 pushed the same image (shows the "possible copy" flag).
            dkey = "shared-image" if (i in (7, 8) and j == 0) else f"{key}/{sub.name}"
            repo_facts[sub.name] = demo_facts(i, sub, rng, dkey)
        scoring_data[key] = {name: compact(f) for name, f in repo_facts.items()}
        # Pre-built Excel samples: each student's latest pushed repo, default rubric.
        pushed = [x for x in p.submissions if x.submission_date]
        if pushed:
            latest = max(pushed, key=lambda x: x.submission_date)
            f = repo_facts[latest.name]
            sel = Selection(repo=latest.name, tag=f.tag)
            scoring.students[key] = StudentEval(selections=[sel], repos={latest.name: RepoEval(
                selection=sel, facts=f, result=score_repo(f, scoring.rubric))})
    for i, (s, p) in enumerate(zip(roster.students, profiles), start=1):
        key = s.docker_id.lower()
        fname = make_student_filename(s.roll, s.docker_id).replace(".xlsx", "_DEMO.xlsx")
        filenames[key] = fname
        evaluation = student_detail(scoring, s) if key in scoring.students else None
        (out / "demo-files" / "students" / f"{key}.xlsx").write_bytes(
            workbook_to_bytes(build_student_workbook(p, evaluation=evaluation)))

    (out / "demo-files" / "All_Submissions_DEMO.xlsx").write_bytes(workbook_to_bytes(
        build_combined_workbook(profiles, scoring=scoring, students=roster.students)))
    (out / "demo-files" / "roster_template.xlsx").write_bytes(workbook_to_bytes(generate_template_workbook()))

    data = {
        "roster": {
            "roster_id": "demo-roster",
            "filename": "sample_roster.xlsx (demo data)",
            "rows_read": roster.rows_read,
            "students_loaded": len(students),
            "skipped": [],
            "warnings": [],
        },
        "students": students,
        "previews": previews,
        "filenames": filenames,
        "combined_filename": "All_Submissions_DEMO.xlsx",
        "scoring": {"rubric": Rubric.default().to_dict(), "checks": catalogue(), "flags": FLAGS,
                    "repos": scoring_data},
    }
    (out / "demo" / "demo-data.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    shutil.copy(ROOT / "scripts" / "pages_demo" / "demo-api.js", out / "demo" / "demo-api.js")

    # The real page, adapted for a static sub-path
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    replacements = [
        ('src="/static/img/', 'src="static/img/'),
        ('href="/static/img/', 'href="static/img/'),
        ('href="/api/template"', 'href="demo-files/roster_template.xlsx"'),
        ('<a href="/" aria-label=', '<a href="./" aria-label='),
        ("<title>Docker Hub Submission Evaluator | Amrita Vishwa Vidyapeetham</title>",
         "<title>Demo | Docker Hub Submission Evaluator | Amrita Vishwa Vidyapeetham</title>"),
        ("  </style>", BANNER_CSS + "  </style>\n  <script src=\"demo/demo-api.js\"></script>"),
        ("<body>\n", "<body>\n" + BANNER_HTML),
    ]
    for old, new in replacements:
        if html.count(old) < 1:
            raise SystemExit(f"build_pages_demo: expected text not found in index.html: {old!r}")
        html = html.replace(old, new)
    if '"/static/' in html or "'/static/" in html:
        raise SystemExit("build_pages_demo: an absolute /static/ path is left in index.html")
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    print(f"Demo site built in {out} ({len(students)} demo students)")
    print(f"Codespaces link: {CODESPACES_URL}")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_site")
