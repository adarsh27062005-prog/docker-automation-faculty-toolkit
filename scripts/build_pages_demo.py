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
* Excel downloads are real workbooks built by app.excel from that demo data.

No uploaded file is ever read by the demo.
"""
from __future__ import annotations

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
             "pulls": s.pull_count, "notes": s.notes}
            for s in p.submissions
        ],
    }


BANNER_CSS = """
    .demo-banner { background: var(--yellow); color: #1a1a1a; font-size: 0.86rem; font-weight: 600; }
    .demo-banner .wrap { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 16px; padding-top: 10px; padding-bottom: 10px; }
    .demo-banner a { color: var(--crimson-dark); white-space: nowrap; }
    .demo-banner a:hover { color: #000; }
"""

BANNER_HTML = f"""  <div class="demo-banner" role="note">
    <div class="wrap">
      <span>Demo mode: this page runs on GitHub Pages with made-up sample data. Uploading any .xlsx loads the built-in demo class; your file is never read or sent anywhere.</span>
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
    students, previews, filenames, profiles = [], {}, {}, []
    for i, s in enumerate(roster.students, start=1):
        p = demo_profile(i, s.roll, s.name, s.docker_id)
        profiles.append(p)
        key = s.docker_id.lower()
        students.append(s.to_dict())
        previews[key] = preview_json(p)
        fname = make_student_filename(s.roll, s.docker_id).replace(".xlsx", "_DEMO.xlsx")
        filenames[key] = fname
        (out / "demo-files" / "students" / f"{key}.xlsx").write_bytes(workbook_to_bytes(build_student_workbook(p)))

    (out / "demo-files" / "All_Submissions_DEMO.xlsx").write_bytes(workbook_to_bytes(build_combined_workbook(profiles)))
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
