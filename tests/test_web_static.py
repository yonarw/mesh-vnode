import pytest
from fastapi.testclient import TestClient

from mesh_vnode import web
from mesh_vnode.config import Settings


@pytest.fixture
def http(tmp_path, monkeypatch):
    dist = tmp_path / "webui" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("index")
    (dist / "sw.js").write_text("worker")
    (tmp_path / "secret.txt").write_text("secret")
    monkeypatch.setattr(web, "WEBUI_DIST", dist)
    return TestClient(web.create_app(Settings(db_path=tmp_path / "db.sqlite3")))


def test_files_in_the_ui_build_are_served(http):
    assert http.get("/sw.js").text == "worker"
    assert http.get("/some/client/route").text == "index"


@pytest.mark.parametrize("path", ["/..%2f..%2fsecret.txt", "/%2e%2e/%2e%2e/secret.txt"])
def test_nothing_outside_the_ui_build_is_served(http, path):
    assert http.get(path).text == "index"
