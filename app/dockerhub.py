from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
import httpx

from app.cache import cache
from app.config import settings
from app.github_links import discover_github_links, load_overrides
from app.layers import pick_image, platforms_of, split_base, steps_from_layers

logger = logging.getLogger(__name__)

DOCKER_HUB_API_BASE = "https://hub.docker.com/v2"

# Concurrency semaphore to avoid overwhelming Docker Hub
SEMAPHORE = asyncio.Semaphore(3)

# Docker Hub's API allows ~180 requests per window and reports what is left in
# x-ratelimit-remaining / x-ratelimit-reset. When it runs low, every request
# waits until the window resets instead of failing (see note_rate_limit).
RATE_LIMIT: Dict[str, float] = {"wait_until": 0.0}
RATE_LIMIT_LOW_WATER = 3
RATE_LIMIT_MAX_WAIT = 60.0
OFFICIAL_CACHE_TTL = 24 * 3600   # official base images' step lists rarely change


def _header_int(value: Any) -> Optional[int]:
    if not isinstance(value, str):
        return None
    head = value.split(";")[0].strip()
    return int(head) if head.isdigit() else None


def note_rate_limit(resp: Any, now: Optional[float] = None) -> None:
    """Pause future requests when Docker Hub says the rate-limit window is nearly used up."""
    now = time.time() if now is None else now
    try:
        headers = resp.headers
        remaining = _header_int(headers.get("x-ratelimit-remaining"))
        reset = _header_int(headers.get("x-ratelimit-reset"))
    except Exception:
        return
    if getattr(resp, "status_code", None) == 429 or (remaining is not None and remaining <= RATE_LIMIT_LOW_WATER):
        wait = (reset - now) if reset and reset > now else 10.0
        RATE_LIMIT["wait_until"] = max(RATE_LIMIT["wait_until"], now + min(max(wait, 1.0), RATE_LIMIT_MAX_WAIT))


def rate_limit_wait_seconds() -> float:
    return max(0.0, RATE_LIMIT["wait_until"] - time.time())

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
    # True when Docker Hub failed (rate limit / network / 5xx) while listing tags,
    # so dates and "No image pushed" cannot be trusted for this repo.
    data_incomplete: bool = False

    @property
    def no_image_pushed(self) -> bool:
        return not self.data_incomplete and self.submission_date is None

TAGS_UNAVAILABLE_NOTE = "Tag data unavailable (Docker Hub error) - re-check later"
TAGS_PARTIAL_NOTE = "Tag list incomplete (Docker Hub error) - dates may be off"
REPOS_PARTIAL_NOTE = "Repository list incomplete (Docker Hub error) - re-check later"

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
            pause = rate_limit_wait_seconds()
            if pause > 0:
                logger.info("Docker Hub rate limit nearly used; waiting %.0fs", pause)
                await asyncio.sleep(pause)
            async with SEMAPHORE:
                await asyncio.sleep(0.02)
                try:
                    resp = await client.get(
                        url,
                        timeout=self.timeout,
                        headers={"User-Agent": "DockerSubmissionApp/1.0"}
                    )
                    note_rate_limit(resp)
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
        repo_item: dict,
        overrides: Optional[Dict[str, str]] = None,
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
            overrides=overrides,
        )

        # Step 2: Fetch tags live (following pagination).
        # tags_status: "complete" | "partial" (a later page failed) | "unavailable" (first page failed)
        tags_url = f"{DOCKER_HUB_API_BASE}/repositories/{did_lower}/{repo_name}/tags?page_size=100"
        raw_tags: List[dict] = []
        tags_status = "complete"
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
                    tags_status = "partial"
                    logger.warning("Tag pagination failed for %s/%s; tag list incomplete", did_lower, repo_name)
                    break
        else:
            tags_status = "unavailable"
            logger.warning(
                "Could not fetch tags for %s/%s (status=%s)",
                did_lower, repo_name, getattr(tags_resp, "status_code", "no response"),
            )

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
                        overrides=overrides,
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

        # Notes: "No image pushed" only when Docker Hub actually returned a complete,
        # empty tag list. A failed tag fetch must not look like a missing submission.
        if tags_status == "unavailable":
            notes = TAGS_UNAVAILABLE_NOTE
        elif tags_status == "partial":
            notes = TAGS_PARTIAL_NOTE
        elif not has_pushed_tags:
            notes = "No image pushed"
        else:
            notes = ""

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
            data_incomplete=(tags_status != "complete"),
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
            repos_partial = False
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
                        repos_partial = True
                        logger.warning("Repository pagination failed for %s; list incomplete", did_lower)
                        break

            repo_list_is_404 = (repo_list_resp is not None and repo_list_resp.status_code == 404)
            if (user_is_404 or (repo_list_is_404 and not user_found)) and not raw_repos:
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

            if repo_list_resp is None or repo_list_resp.status_code != 200:
                logger.error(
                    "Could not fetch repositories for %s (%s): status=%s",
                    roll, docker_id, getattr(repo_list_resp, "status_code", "no response"),
                )
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

            # Process all repositories concurrently (overrides.csv read once per student)
            overrides = load_overrides()
            repo_tasks = [
                self._process_repo(client, did_lower, r_item, overrides)
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

            incomplete_repos = sum(1 for s in submissions if s.data_incomplete)
            notes: List[str] = []
            if repos_partial:
                notes.append(REPOS_PARTIAL_NOTE)
            if incomplete_repos:
                notes.append(f"Tag data incomplete for {incomplete_repos} repo(s) (Docker Hub error) - re-check later")

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
                error_note="; ".join(notes),
            )

            # Only cache complete results, so a transient Docker Hub error is retried
            # on the next request instead of being served for CACHE_TTL_SECONDS.
            if use_cache and not summary.error_note:
                cache.set(cache_key, summary)
            return summary

        finally:
            if owns_client:
                await client.aclose()

    # ------------------------------------------------------------------
    # Scoring support: repo details, a tag's step list, official bases
    # ------------------------------------------------------------------

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> Tuple[Optional[int], Any]:
        """(status, json). status is None when Docker Hub could not be reached."""
        resp = await self._fetch(client, url)
        if resp is None:
            return None, None
        status = getattr(resp, "status_code", None)
        if status != 200:
            return status, None
        try:
            return 200, resp.json()
        except ValueError:
            return None, None

    async def get_repo_detail(self, client: httpx.AsyncClient, namespace: str, repo: str) -> Optional[dict]:
        """Description and README (full_description). None on a Docker Hub error."""
        key = f"repo_detail:{namespace}/{repo}"
        hit = cache.get(key)
        if hit is not None:
            return hit
        status, data = await self._get_json(client, f"{DOCKER_HUB_API_BASE}/repositories/{quote(namespace)}/{quote(repo)}/")
        if status != 200 or not isinstance(data, dict):
            return None
        detail = {"description": data.get("description") or "", "full_description": data.get("full_description") or ""}
        cache.set(key, detail)
        return detail

    async def get_tag_images(self, client: httpx.AsyncClient, namespace: str, repo: str, tag: str) -> Tuple[bool, List[dict]]:
        """(ok, images) for one tag. A 404 is a real answer (no image); other failures are not."""
        key = f"tag_images:{namespace}/{repo}:{tag}"
        hit = cache.get(key)
        if hit is not None:
            return True, hit
        url = f"{DOCKER_HUB_API_BASE}/repositories/{quote(namespace)}/{quote(repo)}/tags/{quote(tag, safe='')}/images"
        status, data = await self._get_json(client, url)
        if status == 404:
            return True, []
        if status != 200 or not isinstance(data, list):
            return False, []
        cache.set(key, data)
        return True, data

    async def official_tag_names(self, client: httpx.AsyncClient, family: str, name_filter: str) -> Optional[List[str]]:
        key = f"official_tags:{family}:{name_filter}"
        hit = cache.get(key)
        if hit is not None:
            return hit
        url = f"{DOCKER_HUB_API_BASE}/repositories/library/{quote(family)}/tags?page_size=100&name={quote(name_filter)}"
        status, data = await self._get_json(client, url)
        if status != 200 or not isinstance(data, dict):
            return None
        names = [t.get("name", "") for t in data.get("results") or [] if t.get("name")]
        cache.set(key, names, ttl=OFFICIAL_CACHE_TTL)
        return names

    async def official_steps(self, client: httpx.AsyncClient, family: str, tag: str, arch: str) -> Optional[List[str]]:
        key = f"official_steps:{family}:{tag}:{arch}"
        hit = cache.get(key)
        if hit is not None:
            return hit
        ok, images = await self.get_tag_images(client, "library", family, tag)
        if not ok:
            return None
        match = [i for i in images if i.get("os") == "linux" and i.get("architecture") == arch]
        image = match[0] if match else None
        steps = [s.instruction for s in steps_from_layers(image.get("layers"))] if image else []
        cache.set(key, steps, ttl=OFFICIAL_CACHE_TTL)
        return steps

    async def get_repo_facts(self, submission: RepoSubmission, tag: Optional[str],
                             client: Optional[httpx.AsyncClient] = None):
        """Everything the scorer needs for one repo + tag (see app.scoring.RepoFacts).

        The repo and tag must already be validated against the student's own
        Docker Hub listing; nothing user-typed is forwarded here.
        """
        from app.scoring import RepoFacts  # local import: scoring imports layers only

        tags = [{"name": t.name, "pushed": t.tag_last_pushed} for t in submission.tags]
        facts = RepoFacts(repo=submission.name, tag=tag, tags=tags, description=submission.description or "")
        if submission.data_incomplete:
            if not submission.tags:
                facts.error = submission.notes or TAGS_UNAVAILABLE_NOTE
                return facts
            # Some tag pages failed (very large repos): the image itself can still be
            # checked, but say that versioning checks only saw the tags that loaded.
            facts.warning = "Tag list incomplete (Docker Hub error): versioning checks used the tags that loaded."
        if not tag:
            return facts  # no tag at all: no image pushed

        owns_client = client is None
        client = client or httpx.AsyncClient()
        try:
            ns = submission.namespace
            detail, (ok, images) = await asyncio.gather(
                self.get_repo_detail(client, ns, submission.name),
                self.get_tag_images(client, ns, submission.name, tag),
            )
            if detail is None or not ok:
                facts.error = "Repository data unavailable (Docker Hub error) - re-check later"
                return facts
            facts.description = detail["description"] or facts.description
            facts.readme = detail["full_description"]
            facts.platforms = platforms_of(images)
            image = pick_image(images)
            pushed = next((t["pushed"] for t in tags if t["name"] == tag), None)
            facts.pushed_at = pushed or (image.get("last_pushed") if image else None)
            if image is None:
                return facts
            arch = image.get("architecture") or "amd64"
            facts.has_image = True
            facts.platform = f"{image.get('os')}/{arch}"
            facts.image_size = int(image.get("size") or 0)
            facts.image_digest = image.get("digest")
            facts.steps = steps_from_layers(image.get("layers"))
            facts.split = await split_base(
                [s.instruction for s in facts.steps],
                lambda fam, f: self.official_tag_names(client, fam, f),
                lambda fam, t: self.official_steps(client, fam, t, arch),
            )
            return facts
        finally:
            if owns_client:
                await client.aclose()

dockerhub_client = DockerHubClient()
