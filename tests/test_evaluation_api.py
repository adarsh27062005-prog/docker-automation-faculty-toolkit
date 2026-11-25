"""Offline tests for repo selection, scoring routes, overrides, jobs and score exports."""
import io
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app.cache import cache
from app.dockerhub import (
    DOCKER_HUB_API_BASE as BASE,
    RATE_LIMIT,
    RepoSubmission,
    StudentProfileSummary,
    TagInfo,
    dockerhub_client,
    note_rate_limit,
)
from app.main import app
from app.scoring import RepoFacts
from tests.test_scoring import GOOD_OWN, PY_BASE, README, facts as make_facts

client = TestClient(app)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def sub(name, ns, tags, incomplete=False):
    pushed = [p for _, p in tags if p]
    return RepoSubmission(
        name=name, namespace=ns, repo_url=f"https://hub.docker.com/r/{ns}/{name}", description="Demo repo",
        pull_count=1, star_count=0, date_registered="2026-11-01T00:00:00Z",
        last_updated=max(pushed) if pushed else None, first_tag_pushed=min(pushed) if pushed else None,
        latest_tag_pushed=max(pushed) if pushed else None, submission_date=max(pushed) if pushed else None,
        tags_count=len(tags), github_url="Not found",
        tags=[TagInfo(n, p, p, 1000, "amd64", "linux") for n, p in tags], data_incomplete=incomplete,
    )


def fake_profile(roll, name, docker_id, **kw):
    did = docker_id.lower()
    return StudentProfileSummary(
        roll=roll, name=name, docker_id=docker_id, docker_id_lower=did, user_found=True, total_repos=2,
        total_pulls=2, repos_with_github=0,
        submissions=[
            sub("demo-app", did, [("latest", "2026-11-20T10:00:00Z"), ("1.0", "2026-11-18T09:30:00Z")]),
            sub("empty-repo", did, []),
        ],
    )


async def fake_facts(submission, tag, client=None):
    if not submission.tags:
        return RepoFacts(repo=submission.name, tag=tag)
    return make_facts(repo=submission.name, tag=tag, pushed_at=next(t.tag_last_pushed for t in submission.tags if t.name == tag))


@pytest.fixture
def hub():
    cache.clear()
    with patch.object(dockerhub_client, "get_student_profile", new=AsyncMock(side_effect=fake_profile)) as prof, \
         patch.object(dockerhub_client, "get_repo_facts", new=AsyncMock(side_effect=fake_facts)) as facts:
        yield prof, facts


@pytest.fixture
def live_client():
    """One event loop for the whole test, so background jobs keep running between requests
    (a bare TestClient starts a new loop per request, like no real server does)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def roster_id():
    wb = Workbook()
    ws = wb.active
    ws.append(["Roll No", "Name", "Docker ID"])
    ws.append(["DEMO.U4CYS23001", "Demo Student 01", "demo-student-01"])
    ws.append(["DEMO.U4CYS23002", "Demo Student 02", "demo-student-02"])
    buf = io.BytesIO()
    wb.save(buf)
    res = client.post("/api/roster", files={"file": ("class.xlsx", buf.getvalue(), XLSX)})
    assert res.status_code == 200
    return res.json()["roster_id"]


def evaluate(rid, did, repos):
    return client.post(f"/api/roster/{rid}/students/{did}/evaluate", json={"repos": repos})


def wait_job(job_id, c=None):
    c = c or client
    for _ in range(100):
        data = c.get(f"/api/jobs/{job_id}").json()
        if data["status"] != "running":
            return data
        time.sleep(0.1)
    return data


def test_rubric_defaults_and_validation(roster_id):
    res = client.get(f"/api/roster/{roster_id}/rubric")
    assert res.status_code == 200
    body = res.json()
    assert sum(c["default_points"] for c in body["checks"]) == 100
    assert body["rubric"]["deadline"] is None and "F1" in body["flags"]
    bad = client.put(f"/api/roster/{roster_id}/rubric", json={"checks": {"S1": {"points": -1}}})
    assert bad.status_code == 400 and "between" in bad.json()["detail"]
    assert client.get("/api/roster/nope/rubric").status_code == 404


def test_selection_is_checked_against_docker_hub(hub, roster_id):
    assert evaluate(roster_id, "demo-student-01", [{"repo": "someone-elses-repo"}]).status_code == 400
    res = evaluate(roster_id, "demo-student-01", [{"repo": "demo-app", "tag": "../../etc"}])
    assert res.status_code == 400 and "does not exist" in res.json()["detail"]
    assert evaluate(roster_id, "demo-student-01", [{"repo": "demo-app", "weight": 0}]).status_code == 400
    assert evaluate(roster_id, "demo-student-01", [{"repo": "demo-app"}, {"repo": "DEMO-APP"}]).status_code == 400
    assert evaluate(roster_id, "not-in-roster", [{"repo": "demo-app"}]).status_code == 404
    hub[1].assert_not_called()          # nothing was fetched for invalid input


def test_evaluate_weights_overrides_and_rescore(hub, roster_id):
    res = evaluate(roster_id, "demo-student-01", [{"repo": "demo-app", "weight": 3}, {"repo": "empty-repo"}])
    assert res.status_code == 200, res.text
    body = res.json()
    repos = {r["repo"]: r for r in body["repos"]}
    assert repos["demo-app"]["tag"] == "latest"          # most recent push chosen automatically
    assert repos["demo-app"]["score"] == 100.0
    assert repos["empty-repo"]["status"] == "no_image" and repos["empty-repo"]["score"] == 0.0
    assert body["summary"]["suggested"] == 75.0           # (3*100 + 1*0) / 4

    url = f"/api/roster/{roster_id}/students/demo-student-01/override"
    assert client.put(url, json={"repo": "demo-app", "score": 150}).status_code == 400
    s = client.put(url, json={"repo": "demo-app", "score": 50, "note": "Copied README"}).json()["summary"]
    assert s["final"] == 37.5 and s["suggested"] == 75.0 and s["status"] == "overridden"
    s = client.put(url, json={"score": 88, "note": "Viva"}).json()["summary"]
    assert s["final"] == 88

    calls = hub[1].call_count
    res = client.put(f"/api/roster/{roster_id}/rubric", json={"deadline": "2026-11-19T00:00"})
    assert res.status_code == 200
    assert hub[1].call_count == calls                    # rescored from stored facts, no refetch
    detail = client.get(f"/api/roster/{roster_id}/students/demo-student-01/evaluation").json()
    app_repo = next(r for r in detail["repos"] if r["repo"] == "demo-app")
    assert "F4" in app_repo["flags"] and app_repo["score"] == 90.0
    assert app_repo["override_score"] == 50            # overrides survive a rubric change
    assert client.delete(f"/api/roster/{roster_id}/rubric").json()["rubric"]["deadline"] is None


def test_scores_list_flags_copies_across_students(hub, roster_id):
    for did in ("demo-student-01", "demo-student-02"):
        assert evaluate(roster_id, did, [{"repo": "demo-app", "tag": "1.0"}]).status_code == 200
    rows = {r["docker_id"]: r for r in client.get(f"/api/roster/{roster_id}/scores").json()["students"]}
    assert rows["demo-student-01"]["status"] == "scored"
    assert "F1" in rows["demo-student-01"]["flags"] and "F1" in rows["demo-student-02"]["flags"]
    assert evaluate(roster_id, "demo-student-02", []).status_code == 200      # clearing the selection
    rows = {r["docker_id"]: r for r in client.get(f"/api/roster/{roster_id}/scores").json()["students"]}
    assert rows["demo-student-02"]["status"] == "not_evaluated"
    assert "F1" not in rows["demo-student-01"]["flags"]


def test_needs_recheck_is_provisional_not_zero(hub, roster_id):
    async def broken(submission, tag, client=None):
        return RepoFacts(repo=submission.name, tag=tag, error="Repository data unavailable (Docker Hub error)")
    hub[1].side_effect = broken
    body = evaluate(roster_id, "demo-student-01", [{"repo": "demo-app"}]).json()
    assert body["repos"][0]["status"] == "needs_recheck" and body["repos"][0]["score"] is None
    assert body["summary"]["status"] == "needs_recheck" and body["summary"]["final"] is None


def test_exports_include_scores(hub, roster_id, live_client):
    evaluate(roster_id, "demo-student-01", [{"repo": "demo-app"}, {"repo": "empty-repo"}])
    res = client.get(f"/api/roster/{roster_id}/students/demo-student-01/export")
    wb = load_workbook(io.BytesIO(res.content))
    assert wb.sheetnames[:2] == ["Repositories", "Score"] and wb.active.title == "Repositories"
    text = " ".join(str(c.value) for row in wb["Score"].iter_rows() for c in row if c.value)
    assert "B2 Runs as non-root" in text and "Final score: 50.0 / 100" in text

    with patch("app.jobs.dockerhub_client.get_student_profile", new=AsyncMock(side_effect=fake_profile)):
        job = live_client.post(f"/api/roster/{roster_id}/export-all/start").json()["job_id"]
        assert wait_job(job, live_client)["status"] == "done"
    wb = load_workbook(io.BytesIO(live_client.get(f"/api/jobs/{job}/download").content))
    assert wb.sheetnames[:2] == ["Marks", "Repo scores"] and "All Submissions" in wb.sheetnames
    marks = [[c.value for c in r] for r in wb["Marks"].iter_rows(values_only=False)]
    assert marks[1][2] == "demo-student-01" and marks[1][5] == 50.0
    assert marks[2][6] == "Not evaluated"


def test_evaluate_all_job(hub, roster_id, live_client):
    assert live_client.post(f"/api/roster/{roster_id}/evaluate-all/start", json={"rule": "nope"}).status_code == 400
    assert live_client.post(f"/api/roster/{roster_id}/evaluate-all/start", json={"rule": "name_contains"}).status_code == 400
    job = live_client.post(f"/api/roster/{roster_id}/evaluate-all/start", json={"rule": "latest"}).json()["job_id"]
    data = wait_job(job, live_client)
    assert data["status"] == "done" and data["kind"] == "evaluate" and "Scored 2" in data["note"]
    assert live_client.get(f"/api/jobs/{job}/download").status_code == 400
    rows = live_client.get(f"/api/roster/{roster_id}/scores").json()
    assert rows["evaluated"] == 2

    evaluate(roster_id, "demo-student-01", [{"repo": "empty-repo"}])          # a manual pick is kept
    job = live_client.post(f"/api/roster/{roster_id}/evaluate-all/start", json={"rule": "name_contains", "text": "demo"}).json()["job_id"]
    wait_job(job, live_client)
    detail = live_client.get(f"/api/roster/{roster_id}/students/demo-student-01/evaluation").json()
    assert [r["repo"] for r in detail["repos"]] == ["empty-repo"]


def test_preview_lists_tags(hub, roster_id):
    with patch("app.main.dockerhub_client.get_student_profile", new=AsyncMock(side_effect=fake_profile)):
        repos = client.get(f"/api/roster/{roster_id}/students/demo-student-01/preview").json()["repos"]
    assert repos[0]["tags"][0] == {"name": "latest", "pushed": "2026-11-20T10:00:00Z"}


class Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}

    def json(self):
        return self._data


def layers(instructions):
    return [{"instruction": i, "size": 1000 if i.split(" ")[0] in ("RUN", "COPY") else 0,
             "digest": f"sha256:{n:064x}"} for n, i in enumerate(instructions)]


@pytest.mark.asyncio
async def test_get_repo_facts_fetches_readme_steps_and_base():
    cache.clear()
    student_steps = PY_BASE + GOOD_OWN
    routes = {
        f"{BASE}/repositories/stu/demo-app/": Resp(200, {"description": "Demo", "full_description": README}),
        f"{BASE}/repositories/stu/demo-app/tags/1.0/images": Resp(200, [
            {"os": "unknown", "architecture": "unknown"},
            {"os": "linux", "architecture": "amd64", "size": 70_000_000, "digest": "sha256:img",
             "last_pushed": "2026-11-18T09:30:00Z", "layers": layers(student_steps)},
            {"os": "linux", "architecture": "arm64", "size": 69_000_000, "digest": "sha256:img2", "layers": []},
        ]),
        f"{BASE}/repositories/library/python/tags?page_size=100&name=3.11.18": Resp(200, {"results": [{"name": "3.11.18-slim"}]}),
        f"{BASE}/repositories/library/python/tags/3.11.18-slim/images": Resp(200, [
            {"os": "linux", "architecture": "amd64", "layers": layers(PY_BASE)}]),
    }
    fetch = AsyncMock(side_effect=lambda c, url: routes.get(url, Resp(404)))
    s = sub("demo-app", "stu", [("1.0", "2026-11-18T09:30:00Z")])
    with patch.object(dockerhub_client, "_fetch", new=fetch):
        f = await dockerhub_client.get_repo_facts(s, "1.0")
    assert f.error == "" and f.has_image and f.readme == README
    assert f.platform == "linux/amd64" and f.platforms == ["linux/amd64", "linux/arm64"]
    assert f.split.base == "python:3.11.18-slim" and f.split.own_start == len(PY_BASE)

    routes.pop(f"{BASE}/repositories/stu/demo-app/")
    cache.clear()
    fetch.side_effect = lambda c, url: routes.get(url)        # None: Docker Hub unreachable
    with patch.object(dockerhub_client, "_fetch", new=fetch):
        f = await dockerhub_client.get_repo_facts(s, "1.0")
    assert "Docker Hub error" in f.error

    # Tag list partly loaded (huge repo): still scored, with a note. Nothing loaded: re-check.
    routes[f"{BASE}/repositories/stu/demo-app/"] = Resp(200, {"description": "Demo", "full_description": README})
    cache.clear()
    fetch.side_effect = lambda c, url: routes.get(url, Resp(404))
    partial = sub("demo-app", "stu", [("1.0", "2026-11-18T09:30:00Z")], incomplete=True)
    with patch.object(dockerhub_client, "_fetch", new=fetch):
        f = await dockerhub_client.get_repo_facts(partial, "1.0")
    assert f.error == "" and f.has_image and "incomplete" in f.warning
    nothing = sub("demo-app", "stu", [], incomplete=True)
    f = await dockerhub_client.get_repo_facts(nothing, None)
    assert "Docker Hub error" in f.error


def test_rate_limit_headers_pause_requests():
    RATE_LIMIT["wait_until"] = 0.0
    note_rate_limit(Resp(200, headers={"x-ratelimit-remaining": "150", "x-ratelimit-reset": "2000000000"}), now=1_000)
    assert RATE_LIMIT["wait_until"] == 0.0
    note_rate_limit(Resp(200, headers={"x-ratelimit-remaining": "2", "x-ratelimit-reset": "1030"}), now=1_000)
    assert RATE_LIMIT["wait_until"] == 1_030
    note_rate_limit(Resp(429, headers={}), now=2_000)
    assert RATE_LIMIT["wait_until"] == 2_010
    RATE_LIMIT["wait_until"] = 0.0
