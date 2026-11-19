from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import httpx

from app.cache import cache
from app.config import settings
from app.github_links import discover_github_links

logger = logging.getLogger(__name__)

DOCKER_HUB_API_BASE = "https://hub.docker.com/v2"

# Concurrency semaphore to avoid overwhelming Docker Hub
SEMAPHORE = asyncio.Semaphore(3)

@dataclass
class TagInfo:
    name: str
    tag_last_pushed: Optional[str]
    last_updated: Optional[str]
    full_size: int
    architecture: str
    os: str

@dataclass
class RepoSubmission:
    name: str
    namespace: str
    repo_url: str
    description: str
    pull_count: int
    star_count: int
    date_registered: Optional[str]    # Repo Created
    last_updated: Optional[str]       # Last Repo Update
    first_tag_pushed: Optional[str]   # First Push
    latest_tag_pushed: Optional[str]  # Last Push (most recent tag push)
    submission_date: Optional[str]    # Based on SUBMISSION_DATE_BASIS
    tags_count: int
    github_url: str
    other_github_urls: List[str] = field(default_factory=list)
    status: Optional[str] = None      # "On time", "Late", or None
    notes: str = ""
    tags: List[TagInfo] = field(default_factory=list)

@dataclass
class StudentProfileSummary:
    roll: str
    name: str
    docker_id: str
    docker_id_lower: str
    user_found: bool
    total_repos: int
    total_pulls: int
    repos_with_github: int
    submissions: List[RepoSubmission] = field(default_factory=list)
    error_note: str = ""

def calculate_submission_status(submission_date_iso: Optional[str]) -> Optional[str]:
    """Calculates On time / Late against SUBMISSION_DEADLINE if set."""
    deadline = settings.submission_deadline
    if deadline is None or not submission_date_iso:
        return None

    try:
        iso_str = submission_date_iso
        if iso_str.endswith("Z"):
            iso_str = iso_str[:-1] + "+00:00"
        tag_dt = datetime.fromisoformat(iso_str)
        if tag_dt.tzinfo is None:
            tag_dt = tag_dt.replace(tzinfo=deadline.tzinfo)

        return "On time" if tag_dt <= deadline else "Late"
    except Exception as e:
        logger.debug("Failed to calculate submission status for date %s: %s", submission_date_iso, e)
        return None

def compute_submission_date(
    basis: str,
    latest_tag_pushed: Optional[str],
    first_tag_pushed: Optional[str],
    date_registered: Optional[str],
    has_pushed_tags: bool,
) -> Optional[str]:
    """
    Computes submission date according to configured SUBMISSION_DATE_BASIS.
    If no tags were pushed, submission date is always None (blank).
    """
    if not has_pushed_tags:
        return None

    basis_clean = basis.strip().lower()
    if basis_clean == "first_push":
        return first_tag_pushed
    elif basis_clean == "created":
        return date_registered
    else:
        # Default: last_push
        return latest_tag_pushed

class DockerHubClient:
    def __init__(self, timeout: float = 15.0, max_retries: int = 5):
        self.timeout = timeout
        self.max_retries = max_retries

    async def _fetch(self, client: httpx.AsyncClient, url: str) -> Optional[httpx.Response]:
        delay = 1.0
        for attempt in range(self.max_retries):
            async with SEMAPHORE:
                await asyncio.sleep(0.02)
                try:
                    resp = await client.get(
                        url,
                        timeout=self.timeout,
                        headers={"User-Agent": "DockerSubmissionApp/1.0"}
                    )
                    if resp.status_code == 429:
                        logger.warning("Docker Hub 429 on %s; retry in %.1fs", url, delay)
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 8.0)
                        continue
                    if resp.status_code >= 500:
                        logger.warning("Docker Hub %d on %s; retry in %.1fs", resp.status_code, url, delay)
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 8.0)
                        continue
                    return resp
                except (httpx.RequestError, httpx.TimeoutException) as exc:
                    if attempt == self.max_retries - 1:
                        logger.debug("Request failed after %d retries for %s: %s", self.max_retries, url, exc)
                        return None
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 8.0)
        return None

    async def _process_repo(
        self,
        client: httpx.AsyncClient,
        did_lower: str,
        repo_item: dict
    ) -> RepoSubmission:
        repo_name = repo_item.get("name", "")
        pull_count = repo_item.get("pull_count", 0)
        star_count = repo_item.get("star_count", 0)
        date_registered = repo_item.get("date_registered")
        last_updated = repo_item.get("last_updated")
        desc = repo_item.get("description") or ""

        # Step 1: Check description and overrides for GitHub link
        primary_gh, other_gh = discover_github_links(
            docker_id=did_lower,
            full_description=None,
            description=desc,
        )

        # Step 2: Fetch tags live (following pagination)
        tags_url = f"{DOCKER_HUB_API_BASE}/repositories/{did_lower}/{repo_name}/tags?page_size=100"
        raw_tags: List[dict] = []
        tags_resp = await self._fetch(client, tags_url)

        if tags_resp is not None and getattr(tags_resp, "status_code", None) == 200:
            t_data = tags_resp.json()
            raw_tags.extend(t_data.get("results", []))
            next_tag_url = t_data.get("next")
            while next_tag_url:
                next_t_resp = await self._fetch(client, next_tag_url)
                if next_t_resp is not None and getattr(next_t_resp, "status_code", None) == 200:
                    nt_data = next_t_resp.json()
                    raw_tags.extend(nt_data.get("results", []))
                    next_tag_url = nt_data.get("next")
                else:
                    break

        # Step 3: Check README in single repo details only if GITHUB_README_LOOKUP is enabled
        if primary_gh == "Not found" and settings.GITHUB_README_LOOKUP:
            single_url = f"{DOCKER_HUB_API_BASE}/repositories/{did_lower}/{repo_name}/"
            single_resp = await self._fetch(client, single_url)
            if single_resp is not None and getattr(single_resp, "status_code", None) == 200:
                s_data = single_resp.json()
                full_desc = s_data.get("full_description")
                if full_desc:
                    primary_gh, other_gh = discover_github_links(
                        docker_id=did_lower,
                        full_description=full_desc,
                        description=desc,
                    )

        # Parse tag information
        tag_objects: List[TagInfo] = []
        pushed_dates: List[str] = []

        for t in raw_tags:
            t_name = t.get("name", "")
            t_pushed = t.get("tag_last_pushed")
            t_updated = t.get("last_updated")
            t_size = t.get("full_size", 0)

            arch = ""
            os_n = ""
            imgs = t.get("images", [])
            if imgs and isinstance(imgs, list) and len(imgs) > 0:
                arch = imgs[0].get("architecture", "")
                os_n = imgs[0].get("os", "")

            tag_objects.append(TagInfo(
                name=t_name,
                tag_last_pushed=t_pushed,
                last_updated=t_updated,
                full_size=t_size,
                architecture=arch,
                os=os_n,
            ))
            if t_pushed:
                pushed_dates.append(t_pushed)

        has_pushed_tags = len(pushed_dates) > 0
        first_tag_pushed = min(pushed_dates) if has_pushed_tags else None
        latest_tag_pushed = max(pushed_dates) if has_pushed_tags else None

        submission_date = compute_submission_date(
            basis=settings.SUBMISSION_DATE_BASIS,
            latest_tag_pushed=latest_tag_pushed,
            first_tag_pushed=first_tag_pushed,
            date_registered=date_registered,
            has_pushed_tags=has_pushed_tags,
        )

        status = calculate_submission_status(submission_date)
        repo_page_url = f"https://hub.docker.com/r/{did_lower}/{repo_name}"

        # Notes: if no tags pushed, explicit requirement: "No image pushed"
        notes = "No image pushed" if not has_pushed_tags else ""

        return RepoSubmission(
            name=repo_name,
            namespace=did_lower,
            repo_url=repo_page_url,
            description=desc,
            pull_count=pull_count,
            star_count=star_count,
            date_registered=date_registered,
            last_updated=last_updated,
            first_tag_pushed=first_tag_pushed,
            latest_tag_pushed=latest_tag_pushed,
            submission_date=submission_date,
            tags_count=len(tag_objects),
            github_url=primary_gh,
            other_github_urls=other_gh,
            status=status,
            notes=notes,
            tags=tag_objects,
        )

    async def get_student_profile(
        self,
        roll: str,
        name: str,
        docker_id: str,
        client: Optional[httpx.AsyncClient] = None,
        use_cache: bool = True,
    ) -> StudentProfileSummary:
        did_lower = docker_id.lower().strip()
        cache_key = f"student_profile:{did_lower}:{settings.SUBMISSION_DATE_BASIS}"

        if use_cache:
            cached_val = cache.get(cache_key)
            if cached_val is not None:
                return cached_val

        owns_client = False
        if client is None:
            client = httpx.AsyncClient()
            owns_client = True

        try:
            # 1. Check user existence and get repos list live
            user_url = f"{DOCKER_HUB_API_BASE}/users/{did_lower}/"
            repo_list_url = f"{DOCKER_HUB_API_BASE}/repositories/{did_lower}/?page_size=100"

            user_resp, repo_list_resp = await asyncio.gather(
                self._fetch(client, user_url),
                self._fetch(client, repo_list_url)
            )

            user_found = (user_resp is not None and user_resp.status_code == 200)
            user_is_404 = (user_resp is not None and user_resp.status_code == 404)

            raw_repos: List[dict] = []
            if repo_list_resp is not None and repo_list_resp.status_code == 200:
                page_data = repo_list_resp.json()
                raw_repos.extend(page_data.get("results", []))
                next_url = page_data.get("next")
                while next_url:
                    next_resp = await self._fetch(client, next_url)
                    if next_resp is not None and next_resp.status_code == 200:
                        next_data = next_resp.json()
                        raw_repos.extend(next_data.get("results", []))
                        next_url = next_data.get("next")
                    else:
                        break

            if user_is_404 and not raw_repos:
                summary = StudentProfileSummary(
                    roll=roll,
                    name=name,
                    docker_id=docker_id,
                    docker_id_lower=did_lower,
                    user_found=False,
                    total_repos=0,
                    total_pulls=0,
                    repos_with_github=0,
                    submissions=[],
                    error_note="Docker Hub user not found",
                )
                if use_cache:
                    cache.set(cache_key, summary)
                return summary

            if repo_list_resp is None:
                logger.error("Could not fetch repositories for %s (%s) due to network error", roll, docker_id)
                return StudentProfileSummary(
                    roll=roll,
                    name=name,
                    docker_id=docker_id,
                    docker_id_lower=did_lower,
                    user_found=user_found,
                    total_repos=0,
                    total_pulls=0,
                    repos_with_github=0,
                    submissions=[],
                    error_note="Failed to fetch repositories (network/rate-limit error)",
                )

            if not raw_repos:
                summary = StudentProfileSummary(
                    roll=roll,
                    name=name,
                    docker_id=docker_id,
                    docker_id_lower=did_lower,
                    user_found=True,
                    total_repos=0,
                    total_pulls=0,
                    repos_with_github=0,
                    submissions=[],
                    error_note="No public repositories",
                )
                if use_cache:
                    cache.set(cache_key, summary)
                return summary

            # Process all repositories concurrently
            repo_tasks = [
                self._process_repo(client, did_lower, r_item)
                for r_item in raw_repos
            ]
            submissions = list(await asyncio.gather(*repo_tasks))

            # Decision 3: Sort repos by Submission Date, newest first; blank dates last.
            submissions.sort(
                key=lambda r: (1 if r.submission_date is not None else 0, r.submission_date or ""),
                reverse=True
            )

            total_pulls = sum(s.pull_count for s in submissions)
            repos_with_gh = sum(1 for s in submissions if s.github_url != "Not found")

            summary = StudentProfileSummary(
                roll=roll,
                name=name,
                docker_id=docker_id,
                docker_id_lower=did_lower,
                user_found=True,
                total_repos=len(submissions),
                total_pulls=total_pulls,
                repos_with_github=repos_with_gh,
                submissions=submissions,
                error_note="",
            )

            if use_cache and not summary.error_note:
                cache.set(cache_key, summary)
            return summary

        finally:
            if owns_client:
                await client.aclose()

dockerhub_client = DockerHubClient()
