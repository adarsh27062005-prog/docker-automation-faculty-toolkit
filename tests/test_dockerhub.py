import pytest
from app.dockerhub import (
    DockerHubClient,
    calculate_submission_status,
    compute_submission_date,
    RepoSubmission,
)
from app.cache import cache
from app.config import settings

def test_compute_submission_date_bases():
    # 1. last_push (default)
    dt = compute_submission_date(
        basis="last_push",
        latest_tag_pushed="2026-06-29T18:23:15Z",
        first_tag_pushed="2026-06-25T10:00:00Z",
        date_registered="2026-06-20T12:00:00Z",
        has_pushed_tags=True,
    )
    assert dt == "2026-06-29T18:23:15Z"

    # 2. first_push
    dt_first = compute_submission_date(
        basis="first_push",
        latest_tag_pushed="2026-06-29T18:23:15Z",
        first_tag_pushed="2026-06-25T10:00:00Z",
        date_registered="2026-06-20T12:00:00Z",
        has_pushed_tags=True,
    )
    assert dt_first == "2026-06-25T10:00:00Z"

    # 3. created
    dt_created = compute_submission_date(
        basis="created",
        latest_tag_pushed="2026-06-29T18:23:15Z",
        first_tag_pushed="2026-06-25T10:00:00Z",
        date_registered="2026-06-20T12:00:00Z",
        has_pushed_tags=True,
    )
    assert dt_created == "2026-06-20T12:00:00Z"

    # 4. No pushed tags -> always None
    dt_none = compute_submission_date(
        basis="last_push",
        latest_tag_pushed=None,
        first_tag_pushed=None,
        date_registered="2026-06-20T12:00:00Z",
        has_pushed_tags=False,
    )
    assert dt_none is None

@pytest.mark.asyncio
async def test_calculate_submission_status():
    settings.SUBMISSION_DEADLINE_STR = None
    assert calculate_submission_status("2026-06-25T10:00:00Z") is None

    settings.SUBMISSION_DEADLINE_STR = "2026-06-30T00:00:00Z"
    assert calculate_submission_status("2026-06-25T10:00:00Z") == "On time"
    assert calculate_submission_status("2026-07-01T10:00:00Z") == "Late"
    settings.SUBMISSION_DEADLINE_STR = None

@pytest.mark.asyncio
async def test_dockerhub_client_pagination_and_sorting(monkeypatch):
    cache.clear()
    client = DockerHubClient()
    settings.SUBMISSION_DATE_BASIS = "last_push"

    class FakeResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
        def json(self):
            return self._json

    async def fake_fetch(self_client, client_obj, url):
        # 1. User check
        if "/users/mockuser/" in url:
            return FakeResponse(200, {"username": "mockuser"})
        # 2. Repo list page 1 (returns page 1 with next link)
        elif url == "https://hub.docker.com/v2/repositories/mockuser/?page_size=100":
            return FakeResponse(200, {
                "count": 3,
                "next": "https://hub.docker.com/v2/repositories/mockuser/?page=2",
                "results": [
                    {
                        "name": "older-repo",
                        "pull_count": 10,
                        "star_count": 1,
                        "date_registered": "2026-05-01T10:00:00Z",
                        "last_updated": "2026-05-05T10:00:00Z",
                        "description": "older",
                    },
                    {
                        "name": "empty-repo",
                        "pull_count": 0,
                        "star_count": 0,
                        "date_registered": "2026-06-01T10:00:00Z",
                        "last_updated": "2026-06-01T10:00:00Z",
                        "description": "",
                    }
                ]
            })
        # 3. Repo list page 2
        elif url == "https://hub.docker.com/v2/repositories/mockuser/?page=2":
            return FakeResponse(200, {
                "count": 3,
                "next": None,
                "results": [
                    {
                        "name": "newest-repo",
                        "pull_count": 50,
                        "star_count": 2,
                        "date_registered": "2026-06-10T10:00:00Z",
                        "last_updated": "2026-06-25T10:00:00Z",
                        "description": "newest",
                    }
                ]
            })
        # 4. Tags for newest-repo: pagination across 2 pages
        elif url == "https://hub.docker.com/v2/repositories/mockuser/newest-repo/tags?page_size=100":
            return FakeResponse(200, {
                "count": 2,
                "next": "https://hub.docker.com/v2/repositories/mockuser/newest-repo/tags?page=2",
                "results": [
                    {"name": "v1", "tag_last_pushed": "2026-06-15T10:00:00Z", "full_size": 1000, "images": []}
                ]
            })
        elif url == "https://hub.docker.com/v2/repositories/mockuser/newest-repo/tags?page=2":
            return FakeResponse(200, {
                "count": 2,
                "next": None,
                "results": [
                    {"name": "v2", "tag_last_pushed": "2026-06-25T10:00:00Z", "full_size": 2000, "images": []}
                ]
            })
        # 5. Tags for older-repo
        elif "/repositories/mockuser/older-repo/tags" in url:
            return FakeResponse(200, {
                "count": 1,
                "next": None,
                "results": [
                    {"name": "latest", "tag_last_pushed": "2026-05-05T10:00:00Z", "full_size": 1000, "images": []}
                ]
            })
        # 6. Tags for empty-repo: 0 tags
        elif "/repositories/mockuser/empty-repo/tags" in url:
            return FakeResponse(200, {"count": 0, "next": None, "results": []})
        return FakeResponse(404, {})

    monkeypatch.setattr(DockerHubClient, "_fetch", fake_fetch)

    profile = await client.get_student_profile(
        roll="DEMO.U4CYS23001",
        name="Mock Student",
        docker_id="mockuser",
        use_cache=False,
    )

    # 1. Total repos equals live count (3) across pagination
    assert profile.total_repos == 3
    assert len(profile.submissions) == 3

    # 2. Check sort order: newest first, blank dates last
    subs = profile.submissions
    assert subs[0].name == "newest-repo"
    assert subs[0].submission_date == "2026-06-25T10:00:00Z"
    assert subs[0].tags_count == 2  # Followed tag pagination

    assert subs[1].name == "older-repo"
    assert subs[1].submission_date == "2026-05-05T10:00:00Z"

    # 3. Check empty-repo: submission_date is None, note is "No image pushed"
    assert subs[2].name == "empty-repo"
    assert subs[2].submission_date is None
    assert subs[2].notes == "No image pushed"

@pytest.mark.asyncio
async def test_dockerhub_client_user_not_found(monkeypatch):
    cache.clear()
    client = DockerHubClient()

    class FakeResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
        def json(self):
            return self._json

    async def fake_fetch(self_client, client_obj, url):
        return FakeResponse(404, {"message": "User not found"})

    monkeypatch.setattr(DockerHubClient, "_fetch", fake_fetch)

    profile = await client.get_student_profile(
        roll="DEMO.U4CYS23999",
        name="Unknown Student",
        docker_id="unknown_user",
        use_cache=False,
    )

    assert profile.user_found is False
    assert profile.total_repos == 0
    assert profile.error_note == "Docker Hub user not found"

@pytest.mark.asyncio
async def test_github_readme_lookup_flag(monkeypatch):
    cache.clear()
    client = DockerHubClient()

    class FakeResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
        def json(self):
            return self._json

    called_detail_urls = []

    async def fake_fetch(self_client, client_obj, url):
        if "users/mockuser" in url:
            return FakeResponse(200, {"username": "mockuser"})
        elif "repositories/mockuser/?page_size" in url:
            return FakeResponse(200, {
                "count": 1,
                "next": None,
                "results": [
                    {
                        "name": "repo1",
                        "namespace": "mockuser",
                        "description": "no github here",
                        "pull_count": 5,
                        "star_count": 0,
                        "date_registered": "2026-06-01T00:00:00Z",
                        "last_updated": "2026-06-01T00:00:00Z",
                    }
                ]
            })
        elif "tags" in url:
            return FakeResponse(200, {"count": 0, "next": None, "results": []})
        elif "repositories/mockuser/repo1" in url:
            called_detail_urls.append(url)
            return FakeResponse(200, {"full_description": "https://github.com/org/repo1"})
        return FakeResponse(404, {})

    monkeypatch.setattr(DockerHubClient, "_fetch", fake_fetch)

    # 1. Default (GITHUB_README_LOOKUP = False) -> Detail endpoint should NOT be called
    settings.GITHUB_README_LOOKUP = False
    profile = await client.get_student_profile(
        roll="1", name="Test", docker_id="mockuser", use_cache=False
    )
    assert len(called_detail_urls) == 0
    assert profile.submissions[0].github_url == "Not found"

    # 2. GITHUB_README_LOOKUP = True -> Detail endpoint should be called
    settings.GITHUB_README_LOOKUP = True
    profile_with_lookup = await client.get_student_profile(
        roll="1", name="Test", docker_id="mockuser", use_cache=False
    )
    assert len(called_detail_urls) == 1
    assert profile_with_lookup.submissions[0].github_url == "https://github.com/org/repo1"
    settings.GITHUB_README_LOOKUP = False

