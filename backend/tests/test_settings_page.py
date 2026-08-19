"""設定頁 —— 重點是「明文 token 永遠不會出現在回應裡」。"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from devloop.api.app import app
from devloop.api.deps import get_session, get_verifier
from devloop.jira.client import FakeJira
from tests.conftest import needs_db

SECRET = "ATATT-do-not-leak-me"
FORM = {
    "site": "team.atlassian.net",
    "email": "sam@example.com",
    "project": "kan",
    "token": SECRET,
}


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    fake = FakeJira()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_verifier] = lambda: fake
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def failing_client(session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_verifier] = lambda: FakeJira(fail_with="Jira 拒絕了這組憑證")
    yield TestClient(app)
    app.dependency_overrides.clear()


@needs_db
def test_empty_state_tells_you_what_to_do(client: TestClient) -> None:
    r = client.get("/settings")
    assert r.status_code == 200
    assert "還沒設定" in r.text
    assert "api-tokens" in r.text  # 有指到 Atlassian 產 token 的地方


@needs_db
def test_saving_shows_connected_and_never_echoes_the_token(client: TestClient) -> None:
    r = client.post("/settings", data=FORM)

    assert r.status_code == 200
    assert "已連上 Jira" in r.text
    assert SECRET not in r.text  # 這一條是這個頁面最重要的斷言
    assert "•" in r.text


@needs_db
def test_project_key_is_normalised_to_uppercase(client: TestClient) -> None:
    r = client.post("/settings", data=FORM)
    assert "KAN" in r.text


@needs_db
def test_a_bad_token_surfaces_the_reason(failing_client: TestClient) -> None:
    r = failing_client.post("/settings", data=FORM)

    assert r.status_code == 200
    assert "連不上" in r.text
    assert "Jira 拒絕了這組憑證" in r.text
    assert SECRET not in r.text


@needs_db
def test_revisiting_the_page_still_hides_the_token(client: TestClient) -> None:
    client.post("/settings", data=FORM)
    r = client.get("/settings")

    assert SECRET not in r.text
    assert "留空 = 不更換" in r.text
