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
from devloop.api.deps import get_graph, get_jira_override, get_session, get_verifier
from devloop.db.models import Card
from devloop.graph.client import FakeGraph
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
        workspace="/tmp",
        token="tok",
        verifier=FakeJira(),
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_verifier] = lambda: FakeJira()
    app.dependency_overrides[get_jira_override] = lambda: jira
    app.dependency_overrides[get_graph] = FakeGraph
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
    assert '已答 <b id="progress-count">0</b> / 2 題必答' in body


@needs_db
def test_answers_are_saved_and_progress_moves(client: TestClient, session: Session) -> None:
    client.post("/cards/sync")
    _generate_for(session)

    client.post(
        "/cards/KAN-15/answers",
        data={"choice__source-choice": "a3", "note__source-choice": "104 之後再說"},
    )

    body = client.get("/cards/KAN-15").text
    assert '已答 <b id="progress-count">1</b> / 2 題必答' in body
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
        workspace="/tmp",
        token="tok",
        verifier=FakeJira(),
    )
    broken = Broken([REMOTE])
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_jira_override] = lambda: broken
    app.dependency_overrides[get_graph] = FakeGraph
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


# ---------- 要求修改（閘門的第二顆鈕） ----------


@needs_db
def test_the_report_page_offers_the_send_back_button(client: TestClient, session: Session) -> None:
    client.post("/cards/sync")
    _generate_for(session)

    body = client.get("/cards/KAN-15").text
    assert "要求修改" in body
    assert 'formaction="/cards/KAN-15/revise"' in body
    assert "哪一節" in body


@needs_db
def test_sending_back_queues_a_revision(client: TestClient, session: Session) -> None:
    client.post("/cards/sync")
    _generate_for(session)

    r = client.post(
        "/cards/KAN-15/revise",
        data={"section_n": "3", "reason": "沒寫出不做的代價"},
    )

    assert r.status_code == 303
    assert "已要求修改" in redirect_message(r)

    card = session.query(Card).filter_by(key="KAN-15").one()
    report = service.latest_report(session, card)
    assert report is not None and report.state == "changes_requested"


@needs_db
def test_sending_back_without_a_reason_is_refused(client: TestClient, session: Session) -> None:
    client.post("/cards/sync")
    _generate_for(session)

    r = client.post("/cards/KAN-15/revise", data={"section_n": "3", "reason": ""})
    assert "哪裡要改" in redirect_message(r)


@needs_db
def test_a_frozen_report_has_no_send_back_button(client: TestClient, session: Session) -> None:
    client.post("/cards/sync")
    _generate_for(session)
    client.post(
        "/cards/KAN-15/freeze",
        data={"choice__source-choice": "a3", "value__tenacity-dep": "yes"},
    )

    body = client.get("/cards/KAN-15").text
    assert 'formaction="/cards/KAN-15/revise"' not in body


# ---------- 草稿保存的接線 ----------


@needs_db
def test_the_page_carries_what_the_draft_script_needs(client: TestClient, session: Session) -> None:
    client.post("/cards/sync")
    _generate_for(session)

    body = client.get("/cards/KAN-15").text
    assert '<script src="/static/gate.js">' in body
    # 草稿 key 綁版本 —— 改版後不會把舊答案錯置到新題目上
    assert 'data-card="KAN-15"' in body
    assert 'data-version="v0.1"' in body
    assert 'data-slug="source-choice"' in body
    assert 'data-required="1"' in body
    assert 'data-required="0"' in body  # extra-notes 是選填
    assert 'id="progress-count"' in body
    assert 'id="freeze-btn"' in body


@needs_db
def test_a_frozen_page_tells_the_script_to_stay_out(client: TestClient, session: Session) -> None:
    client.post("/cards/sync")
    _generate_for(session)
    client.post(
        "/cards/KAN-15/freeze",
        data={"choice__source-choice": "a3", "value__tenacity-dep": "yes"},
    )

    body = client.get("/cards/KAN-15").text
    assert 'data-frozen="1"' in body


# ---------- 成本煞車 ----------


@needs_db
def test_the_card_list_shows_what_today_has_cost(client: TestClient) -> None:
    client.post("/cards/sync")
    body = client.get("/").text
    assert "今日已用 US$0.00 / 上限 US$10.00" in body


@needs_db
def test_generating_is_blocked_once_the_daily_limit_is_reached(
    client: TestClient, session: Session
) -> None:
    from devloop.db.models import Job

    client.post("/cards/sync")
    card = session.query(Card).filter_by(key="KAN-15").one()
    session.add(
        Job(
            card_id=card.id,
            kind="generate_spec",
            status="succeeded",
            prompt="p",
            cwd="/tmp",
            permission_mode="plan",
            cost_usd=10.0,
        )
    )
    session.flush()

    r = client.post("/cards/KAN-15/generate")
    assert "今日成本已達上限" in redirect_message(r)

    # 沒有排出新的工作
    assert session.query(Job).filter_by(status="queued").count() == 0
    # 而且按鈕是停用的
    assert "今日成本已達上限" in client.get("/").text


@needs_db
def test_a_running_job_can_be_cancelled_from_the_list(client: TestClient, session: Session) -> None:
    from devloop.db.models import Job

    client.post("/cards/sync")
    client.post("/cards/KAN-15/generate")
    job = session.query(Job).filter_by(status="queued").one()

    assert f"/jobs/{job.id}/cancel" in client.get("/").text

    r = client.post(f"/jobs/{job.id}/cancel")
    assert "已中止" in redirect_message(r)

    # 路由改的是同一個 session 上的物件；正式環境由 get_session 在請求結束時 commit
    session.flush()
    assert job.status == "cancelled"
    assert job.finished_at is not None


@needs_db
def test_cancelling_a_finished_job_says_so(client: TestClient, session: Session) -> None:
    from devloop.db.models import Job

    client.post("/cards/sync")
    card = session.query(Card).filter_by(key="KAN-15").one()
    done = Job(
        card_id=card.id,
        kind="generate_spec",
        status="succeeded",
        prompt="p",
        cwd="/tmp",
        permission_mode="plan",
    )
    session.add(done)
    session.flush()

    r = client.post(f"/jobs/{done.id}/cancel")
    assert "已經結束" in redirect_message(r)


# ---------- 知識庫命中 ----------


@needs_db
def test_the_list_shows_what_the_knowledge_base_will_feed_claude(
    client: TestClient, session: Session
) -> None:
    from devloop.db.models import Risk

    client.post("/cards/sync")
    other = Card(key="KAN-14", project="KAN", title="前一張卡")
    session.add(other)
    session.flush()
    session.add(
        Risk(
            card_id=other.id,
            slug="chinese-jd-quality",
            text="中文 JD 品質未驗",
            owner_card_key="KAN-15",
        )
    )
    session.flush()

    body = client.get("/").text
    assert "🔗 知識庫命中 1 條" in body
    assert "產生規格時會一起餵給 Claude" in body
    assert "中文 JD 品質未驗" in body
    assert "指派給這張卡" in body


@needs_db
def test_no_hits_means_no_panel(client: TestClient) -> None:
    client.post("/cards/sync")
    assert "知識庫命中" not in client.get("/").text
