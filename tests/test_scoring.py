"""Offline tests for the base/own split (app.layers) and repo scoring (app.scoring)."""
import asyncio

import pytest

from app.layers import BaseSplit, Step, normalise, pick_image, platforms_of, prefix_len, split_base
from app.scoring import CHECKS, Rubric, RepoFacts, find_secrets, parse_deadline, score_repo

PY_BASE = [
    "# debian.sh --arch 'amd64' out/ 'trixie' '@1795392000'",
    "ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "ENV LANG=C.UTF-8",
    "RUN /bin/sh -c set -eux; apt-get update; apt-get install -y --no-install-recommends ca-certificates tzdata ; rm -rf /var/lib/apt/lists/* # buildkit",
    "ENV GPG_KEY=A035C8C19219BA821ECEA86B64E628F8D684696D",
    "ENV PYTHON_VERSION=3.11.18",
    "ENV PYTHON_SHA256=9120e0a653868f6e3698d36cdd99e5a58903ec9469e448f21d98f7b16d8fa290",
    "RUN /bin/sh -c set -eux; apt-get install -y --no-install-recommends gcc; make install; rm -rf /var/lib/apt/lists/* # buildkit",
    "RUN /bin/sh -c set -eux; for src in idle3 pip3 pydoc3 python3; do ln -svT \"$src\" \"/usr/local/bin/$dst\"; done # buildkit",
    'CMD ["python3"]',
]
# The same base, built earlier: older patch version and hashes. Must still match.
PY_BASE_OLDER = [s.replace("3.11.18", "3.11.17").replace("9120e0a653868f6e", "77aa00bb11cc22dd").replace("1795392000", "1790000000") for s in PY_BASE]
PY_FULL = ["# debian.sh --arch 'amd64' out/ 'trixie' '@1795392000'", "RUN /bin/sh -c apt-get install -y buildpack-deps # buildkit", "ENV PYTHON_VERSION=3.11.18", 'CMD ["python3"]']

GOOD_OWN = [
    "WORKDIR /app",
    "LABEL maintainer=demo-student-01 org.opencontainers.image.version=1.0",
    "COPY requirements.txt . # buildkit",
    "RUN /bin/sh -c pip install --no-cache-dir -r requirements.txt # buildkit",
    "COPY . . # buildkit",
    "RUN /bin/sh -c useradd -m appuser # buildkit",
    "USER appuser",
    "EXPOSE map[8000/tcp:{}]",
    "HEALTHCHECK {Test:[CMD-SHELL curl -f http://localhost:8000/ || exit 1] Interval:30s}",
    'CMD ["python" "app.py"]',
]
README = (
    "# Demo app\n\nA small demo service used to test the Faculty Toolkit scoring rules. "
    "It answers on port 8000 and keeps no state between requests.\n\n## Run\n\n"
    "```\ndocker run -p 8000:8000 demo-student-01/demo-app:1.0\n```\n\n"
    "## Configuration\n\nNo environment variables are needed for the demo. Logs go to standard output.\n"
)


def mk_steps(instructions):
    out = []
    for i, ins in enumerate(instructions):
        layer = ins.split(" ", 1)[0] in ("RUN", "COPY", "ADD", "#") or ins.startswith("/bin/sh")
        out.append(Step(ins, size=1000 if layer else 0, digest=f"sha256:{i:04d}{hash(ins) & 0xffff:x}" if layer else None))
    return out


def run(coro):
    return asyncio.run(coro)


async def list_tags(fam, name_filter):
    return {"python": ["3.11.18-slim", "3.11.18", "3.11.18-alpine", "3.11.18-windowsservercore"]}.get(fam, [])


async def fetch_steps(fam, tag):
    return {"3.11.18-slim": PY_BASE, "3.11.18": PY_FULL}.get(tag)


# ---------------------------------------------------------------- layers

def test_normalise_ignores_versions_hashes_and_buildkit():
    assert normalise(PY_BASE[5]) == normalise(PY_BASE_OLDER[5])
    assert normalise("RUN /bin/sh -c echo hi # buildkit") == normalise("/bin/sh -c echo hi")
    assert normalise("RUN |2 A=1 B=2 /bin/sh -c make") == normalise("RUN /bin/sh -c make")
    assert prefix_len(PY_BASE, PY_BASE_OLDER + GOOD_OWN) == len(PY_BASE)
    assert prefix_len(PY_FULL, PY_BASE + GOOD_OWN) == 0


def test_split_finds_official_base_built_earlier():
    split = run(split_base(PY_BASE_OLDER + GOOD_OWN, list_tags, fetch_steps))
    assert split == BaseSplit(len(PY_BASE), "python:3.11.18-slim", "high")


def test_split_pure_retag_has_no_own_steps():
    split = run(split_base(PY_BASE, list_tags, fetch_steps))
    assert split.own_start == len(PY_BASE)


def test_split_os_roots_and_scratch():
    async def none(*a):
        return None
    ubuntu = ["ARG RELEASE", "ARG LAUNCHPAD_BUILD_ARCH", "LABEL org.opencontainers.image.version=24.04",
              "ADD file:54dafd97985c3ec227e2c96be9290657cf2ab456885b5fc8a24c9278bca65e60 in /", 'CMD ["/bin/bash"]',
              "RUN /bin/sh -c apt-get update # buildkit"]
    assert run(split_base(ubuntu, none, none)) == BaseSplit(5, "rootfs root", "medium")
    alpine = ["ADD alpine-minirootfs-3.20.10-x86_64.tar.gz / # buildkit", 'CMD ["/bin/sh"]', "COPY app /app # buildkit"]
    assert run(split_base(alpine, none, none)) == BaseSplit(2, "alpine root", "medium")
    scratch = ["COPY whoami / # buildkit", 'ENTRYPOINT ["/whoami"]']
    assert run(split_base(scratch, none, none)) == BaseSplit(0, "scratch", "high")


def test_split_signature_without_match_is_low_confidence():
    async def nothing(*a):
        return []
    split = run(split_base(PY_BASE + GOOD_OWN, nothing, nothing))
    assert split.confidence == "low"


def test_pick_image_skips_attestations_and_prefers_amd64():
    imgs = [{"os": "unknown", "architecture": "unknown"}, {"os": "linux", "architecture": "arm64"},
            {"os": "linux", "architecture": "amd64"}]
    assert pick_image(imgs)["architecture"] == "amd64"
    assert pick_image(imgs[:2])["architecture"] == "arm64"      # Apple Silicon student: arm64 only
    assert platforms_of(imgs) == ["linux/arm64", "linux/amd64"]
    assert pick_image([{"os": "unknown", "architecture": "unknown"}]) is None


# ---------------------------------------------------------------- scoring helpers

def facts(own=GOOD_OWN, base=PY_BASE, **kw):
    steps = mk_steps(base + own)
    defaults = dict(
        repo="demo-app", tag="1.0", description="Demo service for scoring tests", readme=README,
        tags=[{"name": "latest", "pushed": "2026-11-20T10:00:00Z"}, {"name": "1.0", "pushed": "2026-11-18T09:30:00Z"}],
        platforms=["linux/amd64", "linux/arm64"], platform="linux/amd64", image_size=60 * 1024 * 1024,
        steps=steps, split=BaseSplit(len(base), "python:3.11.18-slim", "high"),
        pushed_at="2026-11-18T09:30:00Z", has_image=True,
    )
    defaults.update(kw)
    return RepoFacts(**defaults)


def by_id(result):
    return {c.id: c for c in result.checks}


def test_default_rubric_totals_100():
    assert sum(c.points for c in CHECKS) == 100
    assert len(Rubric.default().to_dict()["checks"]) == len(CHECKS)


def test_good_repo_scores_100_and_skipped_deadline_is_redistributed():
    r = score_repo(facts(), Rubric.default())
    c = by_id(r)
    assert c["S2"].status == "skip"                 # no deadline set
    assert all(x.status == "pass" for x in r.checks if x.id != "S2"), [(x.id, x.evidence) for x in r.checks if x.status != "pass"]
    assert r.score == 100.0
    assert r.flags == []
    assert r.own_steps == len(GOOD_OWN)


def test_retag_gets_zero_build_points_and_flag():
    r = score_repo(facts(own=[]), Rubric.default())
    c = by_id(r)
    assert all(c[b].status == "fail" for b in ("B1", "B2", "B3", "B5", "B9"))
    assert "re-tag" in c["B1"].evidence
    assert "F3" in r.flags
    assert r.score < 70


def test_no_image_scores_zero():
    r = score_repo(facts(has_image=False, steps=[], split=None, tag="latest"), Rubric.default())
    assert r.status == "no_image" and r.score == 0.0
    assert by_id(r)["S1"].status == "fail"


def test_docker_hub_error_is_not_scored():
    r = score_repo(facts(error="Tag data unavailable (Docker Hub error)"), Rubric.default())
    assert r.status == "needs_recheck" and r.score is None and r.flags == ["F5"]


def test_deadline_late_penalty_and_flag():
    rub = Rubric.from_dict({"deadline": "2026-11-15T23:59", "late_penalty_percent": 20})
    r = score_repo(facts(), rub)
    c = by_id(r)
    assert c["S2"].status == "fail" and "after the deadline" in c["S2"].evidence
    assert "F4" in r.flags
    assert r.score == pytest.approx((90 / 100) * 100 * 0.8, abs=0.1)
    on_time = score_repo(facts(), rub, deadline_override="2026-11-30T23:59")
    assert by_id(on_time)["S2"].status == "pass" and on_time.score == 100.0


def test_inherited_cmd_and_expose_get_half_points():
    own = ["COPY index.html /usr/share/nginx/html/ # buildkit"]
    base = ["ADD alpine-minirootfs-3.24.4-x86_64.tar.gz / # buildkit", 'CMD ["/bin/sh"]', "ENV NGINX_VERSION=1.31.9",
            'ENTRYPOINT ["/docker-entrypoint.sh"]', "EXPOSE map[80/tcp:{}]", 'CMD ["nginx" "-g" "daemon off;"]']
    r = score_repo(facts(own=own, base=base, split=BaseSplit(len(base), "nginx:1.31.9-alpine", "high")), Rubric.default())
    c = by_id(r)
    assert c["B1"].fraction == 0.5 and "inherited" in c["B1"].evidence
    assert c["B4"].fraction == 0.5
    assert c["B5"].status == "skip" and c["B6"].status == "skip"
    assert c["L1"].status == "pass"


def test_package_cleanup_partial_and_consecutive_runs():
    own = ["RUN /bin/sh -c apt-get update && apt-get install -y curl # buildkit",
           "RUN /bin/sh -c pip install flask # buildkit",
           "RUN /bin/sh -c apk add --no-cache git # buildkit",
           "RUN /bin/sh -c echo done # buildkit",
           "ADD app.py /app/ # buildkit"]
    c = by_id(score_repo(facts(own=own), Rubric.default()))
    assert c["B5"].fraction == pytest.approx(1 / 3)
    assert "apt" in c["B5"].evidence and "pip" in c["B5"].evidence
    assert c["B6"].status == "fail"            # 4 consecutive RUN steps
    assert c["B9"].status == "fail"            # ADD for a local file
    assert c["B2"].status == "fail"            # no USER


def test_secret_detection_ignores_placeholders():
    steps = list(enumerate(mk_steps([
        "ENV API_KEY=**None** PORT=8080",
        "ENV DB_PASSWORD=$DB_PASSWORD",
        "ENV SECRET_KEY=s3cr3t-value-123",
        "RUN |1 NPM_TOKEN=abcd1234efgh /bin/sh -c npm ci # buildkit",
        "ARG GITHUB_TOKEN",
    ])))
    found = find_secrets(steps)
    assert any(f.startswith("SECRET_KEY") for f in found)
    assert any(f.startswith("NPM_TOKEN") for f in found)
    assert not any(f.startswith(("API_KEY", "DB_PASSWORD", "GITHUB_TOKEN")) for f in found)
    assert not any("s3cr3t" in f or "abcd1234" in f for f in found)   # names only, never values


def test_secret_penalty_applied():
    r = score_repo(facts(own=GOOD_OWN + ["ENV ADMIN_PASSWORD=hunter2hunter2"]), Rubric.default())
    assert "F6" in r.flags and r.score == 90.0


def test_disabled_check_is_off_and_excluded():
    rub = Rubric.from_dict({"checks": {"B3": {"enabled": False, "points": 5}}})
    r = score_repo(facts(own=[s for s in GOOD_OWN if not s.startswith("HEALTHCHECK")]), rub)
    assert by_id(r)["B3"].status == "off"
    assert r.score == 100.0


def test_size_limit_and_tags():
    rub = Rubric.from_dict({"size_limit_mb": 50})
    c = by_id(score_repo(facts(), rub))              # 60 MB vs 50 MB limit
    assert c["L2"].fraction == 0.5
    c = by_id(score_repo(facts(tags=[{"name": "latest", "pushed": "2026-11-20T10:00:00Z"}], platforms=["linux/amd64"]), Rubric.default()))
    assert c["V1"].status == "fail" and c["V2"].status == "fail" and c["V3"].status == "fail"
    c = by_id(score_repo(facts(tags=[{"name": "final", "pushed": "2026-11-20T10:00:00Z"}]), Rubric.default()))
    assert c["V1"].fraction == 0.5


def test_low_confidence_split_is_marked_estimated():
    r = score_repo(facts(split=BaseSplit(0, "rootfs root", "low")), Rubric.default())
    assert "estimated" in by_id(r)["B1"].evidence
    assert "F3" not in r.flags


def test_documentation_checks():
    c = by_id(score_repo(facts(description="", readme="short"), Rubric.default()))
    assert c["D1"].status == "fail" and c["D2"].status == "fail" and c["D3"].status == "fail" and c["D4"].status == "fail"
    c = by_id(score_repo(facts(readme="# Title\n\n" + "x" * 100), Rubric.default()))
    assert c["D2"].fraction == 0.5 and c["D4"].fraction == 0.5


def test_rubric_validation():
    with pytest.raises(ValueError):
        Rubric.from_dict({"checks": {"ZZ": {"points": 1}}})
    with pytest.raises(ValueError):
        Rubric.from_dict({"checks": {"S1": {"points": 500}}})
    with pytest.raises(ValueError):
        Rubric.from_dict({"checks": {c.id: {"enabled": False} for c in CHECKS}})
    with pytest.raises(ValueError):
        Rubric.from_dict({"deadline": "tomorrow"})
    rub = Rubric.from_dict({"deadline": "2026-11-30T23:59"})
    assert rub.deadline.endswith("+05:30")             # naive input = IST
    assert parse_deadline("2026-11-30T18:29:00Z") == rub.deadline_dt
