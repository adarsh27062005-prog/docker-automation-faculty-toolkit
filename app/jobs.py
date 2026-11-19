from __future__ import annotations
import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set
from zoneinfo import ZoneInfo
from app.config import settings
from app.dockerhub import dockerhub_client, StudentProfileSummary
from app.excel import build_combined_workbook, workbook_to_bytes, sanitize_filename
from app.roster import Roster, Student

logger = logging.getLogger(__name__)

@dataclass
class ExportJob:
    job_id: str
    roster_id: str
    uploaded_filename: str
    status: str  # "running", "done", "failed"
    done: int
    total: int
    current: Optional[str] = None
    error: Optional[str] = None
    excel_bytes: Optional[bytes] = None
    download_filename: str = ""
    created_at: float = field(default_factory=time.time)

class JobManager:
    def __init__(self, ttl_hours: int = settings.JOB_TTL_HOURS):
        self.ttl_seconds = ttl_hours * 3600
        self._jobs: Dict[str, ExportJob] = {}
        self._lock = asyncio.Lock()
        # Strong references to running tasks: asyncio only keeps weak references,
        # so an unreferenced task can be garbage-collected mid-export.
        self._tasks: Set[asyncio.Task] = set()

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [
            k for k, j in self._jobs.items()
            if now - j.created_at > self.ttl_seconds
        ]
        for k in expired:
            del self._jobs[k]

    def get_job(self, job_id: str) -> Optional[ExportJob]:
        self._purge_expired()
        return self._jobs.get(job_id)

    def find_running_job_for_roster(self, roster_id: str) -> Optional[ExportJob]:
        self._purge_expired()
        for j in self._jobs.values():
            if j.roster_id == roster_id and j.status == "running":
                return j
        return None

    async def start_job(self, roster: Roster) -> str:
        async with self._lock:
            existing = self.find_running_job_for_roster(roster.roster_id)
            if existing:
                return existing.job_id

            job_id = secrets.token_urlsafe(16)
            total = len(roster.students)
            job = ExportJob(
                job_id=job_id,
                roster_id=roster.roster_id,
                uploaded_filename=roster.filename,
                status="running",
                done=0,
                total=total,
                current=None,
                error=None,
            )
            self._jobs[job_id] = job

            # Launch asynchronous task
            task = asyncio.create_task(self._run_export_job(job, roster))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return job_id

    async def _run_export_job(self, job: ExportJob, roster: Roster) -> None:
        logger.info("Starting background export job %s for roster %s (%d students)", job.job_id, roster.roster_id, job.total)
        sem = asyncio.Semaphore(2)
        profiles: List[StudentProfileSummary] = []

        async def fetch_student(student: Student) -> StudentProfileSummary:
            async with sem:
                job.current = student.docker_id
                try:
                    profile = await dockerhub_client.get_student_profile(
                        roll=student.roll,
                        name=student.name,
                        docker_id=student.docker_id,
                        use_cache=True,
                    )
                except Exception as e:
                    logger.error("Error fetching %s in job %s: %s", student.docker_id, job.job_id, e)
                    profile = StudentProfileSummary(
                        roll=student.roll,
                        name=student.name,
                        docker_id=student.docker_id,
                        docker_id_lower=student.docker_id_lower,
                        user_found=False,
                        total_repos=0,
                        total_pulls=0,
                        repos_with_github=0,
                        submissions=[],
                        error_note=f"Fetch failed: {str(e)}",
                    )
                job.done += 1
                return profile

        try:
            # We fetch students while tracking progress
            tasks = [fetch_student(s) for s in roster.students]
            profiles = list(await asyncio.gather(*tasks))

            job.current = None

            # Build workbook
            # Format combined filename: All_Submissions_{uploaded_filename_without_ext}_{YYYY-MM-DD}.xlsx
            base_name = roster.filename
            if base_name.lower().endswith(".xlsx"):
                base_name = base_name[:-5]
            today_str = datetime.now(ZoneInfo(settings.DISPLAY_TZ_NAME)).strftime("%Y-%m-%d")
            clean_base = sanitize_filename(f"All_Submissions_{base_name}_{today_str}")
            job.download_filename = f"{clean_base}.xlsx"

            wb = build_combined_workbook(profiles)
            job.excel_bytes = workbook_to_bytes(wb)
            job.status = "done"
            logger.info("Export job %s completed successfully (%d profiles)", job.job_id, len(profiles))

        except Exception as exc:
            logger.exception("Export job %s failed: %s", job.job_id, exc)
            job.status = "failed"
            job.error = str(exc)
            job.current = None

    def clear(self) -> None:
        self._jobs.clear()

job_manager = JobManager()
