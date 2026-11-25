import asyncio
import base64
import binascii
import logging
import secrets
from pathlib import Path
from typing import List, Optional
from fastapi import Body, FastAPI, HTTPException, Response, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.dockerhub import dockerhub_client, StudentProfileSummary
from app.excel import (
    build_student_workbook,
    workbook_to_bytes,
    make_student_filename,
    generate_template_workbook,
    content_disposition,
)
from app.roster import parse_roster_excel, roster_store, RosterParseError
from app.jobs import job_manager
from app.evaluation import (
    AUTO_RULES,
    SelectionError,
    class_flags,
    evaluate_student,
    rescore_all,
    scoring_for,
    set_override,
    student_detail,
    student_summary,
    validate_selections,
)
from app.scoring import FLAGS, Rubric, catalogue

logger = logging.getLogger(__name__)

app = FastAPI(title="Docker Hub Submission Evaluator", version="2.0.0")

# Paths reachable without the shared login (host health checks).
AUTH_EXEMPT_PATHS = {"/api/health"}


class BasicAuthMiddleware:
    """Optional shared login for public hosting (HTTP Basic auth).

    Active only when BASIC_AUTH_USERNAME and BASIC_AUTH_PASSWORD are both set, so
    local runs and the test suite stay open. Browsers show their own login prompt
    and then resend the credentials on every same-origin request, including the
    page's fetch() calls and file downloads.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not settings.basic_auth_enabled
            or scope.get("path") in AUTH_EXEMPT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        if self._authorized(dict(scope.get("headers") or [])):
            await self.app(scope, receive, send)
            return

        response = Response(
            content="Authentication required.",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Faculty Toolkit", charset="UTF-8"'},
            media_type="text/plain",
        )
        await response(scope, receive, send)

    @staticmethod
    def _authorized(headers: dict) -> bool:
        raw = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, encoded = raw.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return False
        try:
            user, sep, password = base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        if not sep:
            return False
        # Compare both fields in constant time; evaluate both to avoid leaking which one failed.
        user_ok = secrets.compare_digest(user.encode("utf-8"), settings.BASIC_AUTH_USERNAME.encode("utf-8"))
        pass_ok = secrets.compare_digest(password.encode("utf-8"), settings.BASIC_AUTH_PASSWORD.encode("utf-8"))
        return user_ok and pass_ok


app.add_middleware(BasicAuthMiddleware)

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

# Branding assets (logos). Only the img/ folder is exposed, not the whole static dir.
app.mount("/static/img", StaticFiles(directory=STATIC_DIR / "img"), name="static-img")

@app.get("/api/health")
async def health_check():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    if not INDEX_HTML.is_file():
        raise HTTPException(status_code=404, detail="index.html not found")
    return HTMLResponse(content=INDEX_HTML.read_text(encoding="utf-8"))

@app.get("/api/template")
async def download_roster_template():
    wb = generate_template_workbook()
    content = workbook_to_bytes(wb)
    filename = "roster_template.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(filename)}
    )

@app.post("/api/roster")
async def upload_roster(file: UploadFile = File(...)):
    filename = file.filename or "roster.xlsx"
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(
            status_code=400,
            detail=f"Only Excel (.xlsx) files are supported. Received: {filename}"
        )

    content = await file.read()
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Uploaded file exceeds the maximum size limit of {settings.MAX_UPLOAD_MB} MB."
        )

    try:
        roster = parse_roster_excel(content, filename=filename)
    except RosterParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected error while parsing roster: %s", e)
        raise HTTPException(status_code=400, detail=f"Failed to read Excel workbook: {str(e)}")

    if len(roster.students) == 0:
        raise HTTPException(
            status_code=400,
            detail="No valid students found in the uploaded file."
        )

    if len(roster.students) > settings.MAX_STUDENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Roster contains {len(roster.students)} students, which exceeds the limit of {settings.MAX_STUDENTS}."
        )

    roster_store.add(roster)

    return {
        "roster_id": roster.roster_id,
        "filename": roster.filename,
        "rows_read": roster.rows_read,
        "students_loaded": len(roster.students),
        "skipped": roster.skipped,
        "warnings": roster.warnings,
    }

@app.get("/api/roster/{roster_id}")
async def get_roster_info(roster_id: str):
    roster = roster_store.get(roster_id)
    if not roster:
        raise HTTPException(
            status_code=404,
            detail="Roster expired or not found. Please upload your student list again."
        )
    return {
        "roster_id": roster.roster_id,
        "filename": roster.filename,
        "rows_read": roster.rows_read,
        "students_loaded": len(roster.students),
        "skipped": roster.skipped,
        "warnings": roster.warnings,
    }

@app.get("/api/roster/{roster_id}/students")
async def list_roster_students(roster_id: str):
    roster = roster_store.get(roster_id)
    if not roster:
        raise HTTPException(
            status_code=404,
            detail="Roster expired or not found. Please upload your student list again."
        )
    return [s.to_dict() for s in roster.students]

@app.get("/api/roster/{roster_id}/students/{docker_id}/preview")
async def preview_student(roster_id: str, docker_id: str):
    roster = roster_store.get(roster_id)
    if not roster:
        raise HTTPException(
            status_code=404,
            detail="Roster expired or not found. Please upload your student list again."
        )

    student = roster.find_student(docker_id)
    if not student:
        raise HTTPException(
            status_code=404,
            detail=f"Student with Docker ID '{docker_id}' not found in this roster."
        )

    profile = await dockerhub_client.get_student_profile(
        roll=student.roll,
        name=student.name,
        docker_id=student.docker_id,
        use_cache=True,
    )

    github_links = []
    for sub in profile.submissions:
        if sub.github_url and sub.github_url != "Not found":
            github_links.append(sub.github_url)

    repos_list = [
        {
            "name": sub.name,
            "url": sub.repo_url,
            "submission_date": sub.submission_date,
            "pulls": sub.pull_count,
            "notes": sub.notes,
            "tags": [
                {"name": t.name, "pushed": t.tag_last_pushed}
                for t in sorted(sub.tags, key=lambda t: t.tag_last_pushed or "", reverse=True)
            ],
        }
        for sub in profile.submissions
    ]

    return {
        "roll": profile.roll,
        "name": profile.name,
        "docker_id": profile.docker_id,
        "user_found": profile.user_found,
        "total_repos": profile.total_repos,
        "total_pulls": profile.total_pulls,
        "repos_with_github": profile.repos_with_github,
        "github_links": list(dict.fromkeys(github_links)),
        "error_note": profile.error_note,
        "repos": repos_list,
    }

@app.get("/api/roster/{roster_id}/students/{docker_id}/export")
async def export_student_excel(roster_id: str, docker_id: str):
    roster = roster_store.get(roster_id)
    if not roster:
        raise HTTPException(
            status_code=404,
            detail="Roster expired or not found. Please upload your student list again."
        )

    student = roster.find_student(docker_id)
    if not student:
        raise HTTPException(
            status_code=404,
            detail=f"Student with Docker ID '{docker_id}' not found in this roster."
        )

    profile = await dockerhub_client.get_student_profile(
        roll=student.roll,
        name=student.name,
        docker_id=student.docker_id,
        use_cache=True,
    )

    evaluation = None
    if roster.scoring is not None and student.docker_id_lower in roster.scoring.students:
        evaluation = student_detail(roster.scoring, student)
    wb = build_student_workbook(profile, evaluation=evaluation)
    content = workbook_to_bytes(wb)

    filename = make_student_filename(student.roll, student.docker_id)

    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(filename)}
    )

@app.post("/api/roster/{roster_id}/export-all/start")
async def start_export_all_job(roster_id: str):
    roster = roster_store.get(roster_id)
    if not roster:
        raise HTTPException(
            status_code=404,
            detail="Roster expired or not found. Please upload your student list again."
        )

    job_id = await job_manager.start_job(roster)
    return {"job_id": job_id}

@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return {
        "job_id": job.job_id,
        "kind": job.kind,
        "status": job.status,
        "done": job.done,
        "total": job.total,
        "current": job.current,
        "error": job.error,
        "note": job.status_note(),
    }

@app.get("/api/jobs/{job_id}/download")
async def download_job_excel(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")

    if job.kind != "export":
        raise HTTPException(status_code=400, detail="This job has no file to download.")

    if job.status != "done":
        raise HTTPException(
            status_code=409,
            detail=f"Export job is not complete yet (current status: '{job.status}')"
        )

    if not job.excel_bytes:
        raise HTTPException(status_code=500, detail="Export file content missing.")

    return Response(
        content=job.excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(job.download_filename)}
    )


# ---------------------------------------------------------------------------
# Repo selection and scoring (docs/plans/repo-scoring.md)
# ---------------------------------------------------------------------------

ROSTER_GONE = "Roster expired or not found. Please upload your student list again."


def _roster_or_404(roster_id: str):
    roster = roster_store.get(roster_id)
    if not roster:
        raise HTTPException(status_code=404, detail=ROSTER_GONE)
    return roster


def _student_or_404(roster, docker_id: str):
    student = roster.find_student(docker_id)
    if not student:
        raise HTTPException(status_code=404, detail=f"Student with Docker ID '{docker_id}' not found in this roster.")
    return student


def _rubric_response(roster) -> dict:
    return {"rubric": scoring_for(roster).rubric.to_dict(), "checks": catalogue(), "flags": FLAGS}


@app.get("/api/roster/{roster_id}/rubric")
async def get_rubric(roster_id: str):
    return _rubric_response(_roster_or_404(roster_id))


@app.put("/api/roster/{roster_id}/rubric")
async def update_rubric(roster_id: str, payload: dict = Body(...)):
    roster = _roster_or_404(roster_id)
    try:
        rubric = Rubric.from_dict(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scoring_for(roster).rubric = rubric
    rescore_all(roster)  # stored facts are rescored; nothing is fetched again
    return _rubric_response(roster)


@app.delete("/api/roster/{roster_id}/rubric")
async def reset_rubric(roster_id: str):
    roster = _roster_or_404(roster_id)
    scoring_for(roster).rubric = Rubric.default()
    rescore_all(roster)
    return _rubric_response(roster)


@app.get("/api/roster/{roster_id}/scores")
async def list_scores(roster_id: str):
    roster = _roster_or_404(roster_id)
    scoring = scoring_for(roster)
    extra = class_flags(scoring)
    rows = [student_summary(scoring, s, extra) for s in roster.students]
    return {"students": rows, "evaluated": sum(1 for r in rows if r["status"] != "not_evaluated")}


@app.get("/api/roster/{roster_id}/students/{docker_id}/evaluation")
async def get_evaluation(roster_id: str, docker_id: str):
    roster = _roster_or_404(roster_id)
    student = _student_or_404(roster, docker_id)
    return student_detail(scoring_for(roster), student)


@app.post("/api/roster/{roster_id}/students/{docker_id}/evaluate")
async def evaluate(roster_id: str, docker_id: str, payload: dict = Body(...)):
    roster = _roster_or_404(roster_id)
    student = _student_or_404(roster, docker_id)
    profile = await dockerhub_client.get_student_profile(
        roll=student.roll, name=student.name, docker_id=student.docker_id, use_cache=True)
    try:
        selections = validate_selections(profile, payload.get("repos", []))
    except SelectionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await evaluate_student(roster, student, selections, profile=profile)
    return student_detail(scoring_for(roster), student)


@app.put("/api/roster/{roster_id}/students/{docker_id}/override")
async def override_score(roster_id: str, docker_id: str, payload: dict = Body(...)):
    roster = _roster_or_404(roster_id)
    student = _student_or_404(roster, docker_id)
    try:
        set_override(roster, student, payload.get("repo"), payload.get("score"), payload.get("note", ""))
    except SelectionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return student_detail(scoring_for(roster), student)


@app.post("/api/roster/{roster_id}/evaluate-all/start")
async def start_evaluate_all(roster_id: str, payload: dict = Body(default={})):
    roster = _roster_or_404(roster_id)
    rule = str(payload.get("rule") or "keep")
    text = str(payload.get("text") or "").strip()
    if rule not in AUTO_RULES:
        raise HTTPException(status_code=400, detail=f"Unknown selection rule '{rule}'.")
    if rule == "name_contains" and not text:
        raise HTTPException(status_code=400, detail="Type the text the repository name should contain.")
    job_id = await job_manager.start_evaluation_job(roster, rule, text[:100], bool(payload.get("overwrite")))
    return {"job_id": job_id}
