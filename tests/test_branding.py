from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_index_references_college_logos():
    html = client.get("/").text
    assert "/static/img/amrita-logo.svg" in html
    assert "/static/img/iic-logo.png" in html


def test_logo_assets_are_served():
    svg = client.get("/static/img/amrita-logo.svg")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")

    png = client.get("/static/img/iic-logo.png")
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"

    assert client.get("/static/img/favicon.svg").status_code == 200


def test_only_img_folder_is_exposed():
    # index.html is served at "/", not as a raw static file, and nothing outside img/ is reachable
    assert client.get("/static/index.html").status_code == 404
    assert client.get("/static/img/../index.html").status_code == 404
