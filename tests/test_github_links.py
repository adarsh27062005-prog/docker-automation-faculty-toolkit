import pytest
from app.github_links import (
    clean_github_url,
    discover_github_links,
    extract_github_urls_from_text,
    enrich_github_repo,
)

def test_clean_github_url_standard():
    assert clean_github_url("https://github.com/torvalds/linux") == "https://github.com/torvalds/linux"
    assert clean_github_url("http://github.com/torvalds/linux") == "https://github.com/torvalds/linux"

def test_clean_github_url_strips_git():
    assert clean_github_url("https://github.com/owner/repo.git") == "https://github.com/owner/repo"

def test_clean_github_url_strips_trailing_punctuation():
    assert clean_github_url("https://github.com/owner/repo.") == "https://github.com/owner/repo"
    assert clean_github_url("https://github.com/owner/repo,") == "https://github.com/owner/repo"
    assert clean_github_url("(https://github.com/owner/repo)") == "https://github.com/owner/repo"
    assert clean_github_url("<https://github.com/owner/repo>") == "https://github.com/owner/repo"

def test_clean_github_url_strips_path_suffixes():
    assert clean_github_url("https://github.com/owner/repo/tree/main") == "https://github.com/owner/repo"
    assert clean_github_url("https://github.com/owner/repo/blob/master/README.md") == "https://github.com/owner/repo"

def test_extract_github_urls_multiple_and_deduplicate():
    text = (
        "Check out https://github.com/user/project1 and also "
        "https://github.com/user/project2.git or https://github.com/user/project1/tree/dev"
    )
    urls = extract_github_urls_from_text(text)
    assert len(urls) == 2
    assert urls[0] == "https://github.com/user/project1"
    assert urls[1] == "https://github.com/user/project2"

def test_discover_github_links_not_found():
    primary, other = discover_github_links("user123", full_description=None, description="No repo here")
    assert primary == "Not found"
    assert other == []

def test_discover_github_links_from_readme():
    readme = "Source code: https://github.com/student/app and docs at https://github.com/student/docs"
    primary, other = discover_github_links("student", full_description=readme, description="short desc")
    assert primary == "https://github.com/student/app"
    assert other == ["https://github.com/student/docs"]

def test_discover_github_links_overrides():
    overrides = {"student": "https://github.com/manual/override"}
    readme = "Source code: https://github.com/student/app"
    primary, other = discover_github_links("student", full_description=readme, overrides=overrides)
    assert primary == "https://github.com/manual/override"
    assert other == []

@pytest.mark.asyncio
async def test_enrich_github_repo_without_token():
    # If token is None, enrichment must return None and make zero network calls
    res = await enrich_github_repo("https://github.com/user/repo", client=None, token=None)
    assert res is None

@pytest.mark.asyncio
async def test_enrich_github_repo_mocked_success(monkeypatch):
    import httpx

    class MockResponse:
        status_code = 200
        def json(self):
            return {
                "created_at": "2026-01-01T00:00:00Z",
                "pushed_at": "2026-01-02T00:00:00Z",
                "stargazers_count": 42,
                "forks_count": 7,
                "fork": False,
            }

    class MockClient:
        async def get(self, url, headers=None, timeout=None):
            return MockResponse()

    res = await enrich_github_repo("https://github.com/user/repo", client=MockClient(), token="fake_token")
    assert res is not None
    assert res["stars"] == 42
    assert res["forks"] == 7
    assert res["is_fork"] is False
