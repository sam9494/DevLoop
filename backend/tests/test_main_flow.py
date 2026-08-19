"""主流程的端到端：擋門 → 同步 → 產生 → 讀報告 → 答題 → 凍結 → 卡進「進行中」。"""

import json
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from devloop.api import app as app_module
from devloop.api.app import app
from devloop.api.deps import get_jira_override, get_session, get_verifier
from devloop.db.models import Card
from devloop.jira.client import Card as RemoteCard
from devloop.jira.client import FakeJira
from devloop.jira.connections import save_connection
from devloop.runner.claude import FakeRunner
from devloop.spec import service
from tests.conftest import needs_db
from tests.test_spec_service import REPORT


def redirect_message(response) -> str:  # type: ignore[no-untyped-def]
    """導向訊息是 URL 編碼的，斷言前先解回中文。"""
    return unquote(response.headers["location"])


REMOTE = RemoteCard(
    key="KAN-15",
    title="104 adapter",
    status="待辦事項",
    url="https://x/browse/KAN-15",
    labels=["phase-0"],
)


@pytest.fixture
def jira() -> FakeJira:
    return FakeJira([REMOTE])


@pytest.fixture
def unconfigured(session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


@pytest.fixture
def client(session: Session, jira: FakeJira) -> Iterator[TestClient]:
    save_connection(
        session,
        "sam",
        site="team.atlassian.net",
        email="s@x.com",
        project="KAN",
        token="tok",
        verifier=FakeJira(),
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_verifier] = lambda: FakeJira()
    app.dependency_overrides[get_jira_override] = lambda: jira
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def _generate_for(session: Session, key: str = "KAN-15") -> None:
    card = session.query(Card).filter_by(key=key).one()
    job = service.enqueue_generation(
        session, card, workspace=Path("/tmp"), permission_mode="acceptEdits"
    )
    service.run_job(session, job, FakeRunner(result=json.dumps(REPORT)), timeout_s=60)


# ---------- 擋門 ----------


@needs_db
def test_without_a_verified_connection_the_flow_redirects_to_settings(
    unconfigured: TestClient,
) -> None:
    r = unconfigured.get("/")
    assert r.status_code == 307
    assert r.headers["location"] == "/settings"


@needs_db
def test_settings_itself_is_never_gated(unconfigured: TestClient) -> None:
    assert unconfigured.get("/settings").status_code == 200


# ---------- 同步與產生 ----------


@needs_db
def test_sync_pulls_cards_and_the_list_shows_them(client: TestClient) -> None:
    assert client.post("/cards/sync").status_code == 303

    body = client.get("/").text
    assert "KAN-15" in body
    assert "104 adapter" in body
    assert "尚未產生規格" in body


@needs_db
def test_generate_only_queues_a_job_so_the_request_returns_at_once(
    client: TestClient, session: Session
) -> None:
    client.post("/cards/sync")
    r = client.post("/cards/KAN-15/generate")

    assert r.status_code == 303
    card = session.query(Card).filter_by(key="KAN-15").one()
    assert service.latest_report(session, card) is None  # worker 還沒跑
    assert "產生中" in client.get("/").text


# ---------- 規格頁 ----------


@needs_db
def test_the_report_page_shows_sections_and_inline_questions(
    client: TestClient, session: Session
) -> None:
    client.post("/cards/sync")
    _generate_for(session)

    body = client.get("/cards/KAN-15").text
    assert "關鍵決策點" in body
    assert "第一個來源接誰" in body
    assert "source-choice" in body
    assert "以上皆非" in body
    assert "已答 <b>0</b> / 2 題必答" in body


@needs_db
def test_answers_are_saved_and_progress_moves(client: TestClient, session: Session) -> None:
    client.post("/cards/sync")
    _generate_for(session)

    client.post(
        "/cards/KAN-15/answers",
        data={"choice__source-choice": "a3", "note__source-choice": "104 之後再說"},
    )

    body = client.get("/cards/KAN-15").text
    assert "已答 <b>1</b> / 2 題必答" in body
    assert "104 之後再說" in body


@needs_db
def test_none_of_above_without_a_note_is_rejected_with_a_message(
    client: TestClient, session: Session
) -> None:
    client.post("/cards/sync")
    _generate_for(session)

    r = client.post(
        "/cards/KAN-15/answers",
        data={"choice__source-choice": "none", "note__source-choice": "   "},
    )
    assert "以上皆非" in redirect_message(r)


# ---------- 凍結 ----------


@needs_db
def test_freeze_is_blocked_while_questions_remain(client: TestClient, session: Session) -> None:
    client.post("/cards/sync")
    _generate_for(session)

    r = client.post("/cards/KAN-15/freeze", data={"choice__source-choice": "a3"})
    assert "沒答" in redirect_message(r)

    card = session.query(Card).filter_by(key="KAN-15").one()
    report = service.latest_report(session, card)
    assert report is not None and report.state == "draft"


@needs_db
def test_freezing_locks_the_report_and_moves_the_jira_card(
    client: TestClient, session: Session, jira: FakeJira
) -> None:
    client.post("/cards/sync")
    _generate_for(session)

    r = client.post(
        "/cards/KAN-15/freeze",
        data={"choice__source-choice": "a3", "value__tenacity-dep": "yes"},
    )

    assert "已凍結" in redirect_message(r)
    assert jira.transitions == [("KAN-15", "進行中")]

    card = session.query(Card).filter_by(key="KAN-15").one()
    report = service.latest_report(session, card)
    assert report is not None
    assert report.state == "frozen" and report.version == "v1.0"


@needs_db
def test_a_frozen_report_no_longer_offers_the_gate(client: TestClient, session: Session) -> None:
    client.post("/cards/sync")
    _generate_for(session)
    client.post(
        "/cards/KAN-15/freeze",
        data={"choice__source-choice": "a3", "value__tenacity-dep": "no"},
    )

    body = client.get("/cards/KAN-15").text
    assert "已凍結 v1.0" in body
    assert "凍結並開工" not in body
    assert "下載決策紀錄" in body


@needs_db
def test_jira_failure_after_freezing_does_not_undo_the_freeze(
    session: Session, jira: FakeJira
) -> None:
    from devloop.jira.client import JiraError

    class Broken(FakeJira):
        def transition(self, key: str, to_status: str) -> None:
            raise JiraError("沒有通往「進行中」的轉換")

    save_connection(
        session,
        "sam",
        site="t.atlassian.net",
        email="s@x.com",
        project="KAN",
        token="tok",
        verifier=FakeJira(),
    )
    broken = Broken([REMOTE])
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_jira_override] = lambda: broken
    try:
        c = TestClient(app, follow_redirects=False)
        c.post("/cards/sync")
        _generate_for(session)
        r = c.post(
            "/cards/KAN-15/freeze",
            data={"choice__source-choice": "a3", "value__tenacity-dep": "yes"},
        )
        assert "Jira 沒動到" in redirect_message(r)

        card = session.query(Card).filter_by(key="KAN-15").one()
        report = service.latest_report(session, card)
        assert report is not None and report.state == "frozen"  # 決策先落地，外部系統後動
    finally:
        app.dependency_overrides.clear()


@needs_db
def test_review_json_is_downloadable_with_the_card_in_the_filename(
    client: TestClient, session: Session
) -> None:
    client.post("/cards/sync")
    _generate_for(session)
    client.post(
        "/cards/KAN-15/freeze",
        data={"choice__source-choice": "a3", "value__tenacity-dep": "yes"},
    )

    r = client.get("/cards/KAN-15/review.json")
    assert "KAN-15-review.json" in r.headers["content-disposition"]
    assert json.loads(r.text)["report_version"] == "v1.0"


@needs_db
def test_the_owner_key_is_still_hardcoded(session: Session) -> None:
    # 這條是提醒：加登入時要改的就是這裡（DEBT.md 有登記）
    assert app_module.OWNER_KEY == "sam"
