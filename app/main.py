import asyncio
import logging
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Response, UploadFile, File
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

logger = logging.getLogger(__name__)

app = FastAPI(title="Docker Hub Submission Evaluator", version="2.0.0")

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

    wb = build_student_workbook(profile)
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
        "status": job.status,
        "done": job.done,
        "total": job.total,
        "current": job.current,
        "error": job.error,
    }

@app.get("/api/jobs/{job_id}/download")
async def download_job_excel(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")

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
