"""Web 工作台静态资源与入口测试。"""

from fastapi.testclient import TestClient

from app.main import create_app


def test_frontend_entry_is_served():
    response = TestClient(create_app()).get("/")
    assert response.status_code == 200
    assert "AudioMuse · 声音工作台" in response.text
    assert 'id="recordButton"' in response.text


def test_frontend_assets_are_served():
    client = TestClient(create_app())
    css = client.get("/assets/styles.css")
    js = client.get("/assets/app.js")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
