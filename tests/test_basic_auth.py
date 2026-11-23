import base64

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)


def basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "BASIC_AUTH_USERNAME", "faculty")
    monkeypatch.setattr(settings, "BASIC_AUTH_PASSWORD", "s3cret pass:with colon")


def test_open_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "BASIC_AUTH_USERNAME", "")
    monkeypatch.setattr(settings, "BASIC_AUTH_PASSWORD", "")
    assert client.get("/").status_code == 200
    assert client.get("/api/template").status_code == 200


def test_requires_login_when_configured(auth_on):
    res = client.get("/")
    assert res.status_code == 401
    assert res.headers["www-authenticate"].startswith("Basic")
    assert client.get("/api/template").status_code == 401
    assert client.get("/static/img/amrita-logo.svg").status_code == 401


def test_correct_credentials_pass(auth_on):
    h = basic("faculty", "s3cret pass:with colon")
    assert client.get("/", headers=h).status_code == 200
    assert client.get("/static/img/iic-logo.png", headers=h).status_code == 200
    assert client.get("/api/template", headers=h).status_code == 200


@pytest.mark.parametrize("headers", [
    {"Authorization": "Basic " + base64.b64encode(b"faculty:wrong").decode()},
    {"Authorization": "Basic " + base64.b64encode(b"someone:s3cret pass:with colon").decode()},
    {"Authorization": "Basic not-base64!!"},
    {"Authorization": "Bearer abc"},
    {"Authorization": "Basic " + base64.b64encode(b"nocolon").decode()},
])
def test_bad_credentials_rejected(auth_on, headers):
    assert client.get("/", headers=headers).status_code == 401


def test_health_check_stays_open(auth_on):
    assert client.get("/api/health").status_code == 200
