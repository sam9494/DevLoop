from devloop.core.crypto import decrypt
from devloop.jira.client import FakeJira
from devloop.jira.connections import get_connection, save_connection
from tests.conftest import needs_db

ARGS = {"site": "team.atlassian.net", "email": "sam@example.com", "project": "KAN"}


@needs_db
def test_first_save_encrypts_the_token_and_marks_verified(session) -> None:  # type: ignore[no-untyped-def]
    view = save_connection(session, "sam", token="tok-1", verifier=FakeJira(), **ARGS)

    assert view.is_verified
    assert view.token_masked.endswith("ok-1")
    stored = get_connection(session, "sam")
    assert stored is not None
    assert "tok-1" not in stored.jira_token_encrypted
    assert decrypt(stored.jira_token_encrypted) == "tok-1"


@needs_db
def test_failed_verification_keeps_settings_but_records_the_error(session) -> None:  # type: ignore[no-untyped-def]
    view = save_connection(
        session, "sam", token="bad", verifier=FakeJira(fail_with="token 不對"), **ARGS
    )

    assert not view.is_verified
    assert view.last_error == "token 不對"
    assert get_connection(session, "sam") is not None  # 設定沒被丟掉


@needs_db
def test_blank_token_on_update_keeps_the_existing_one(session) -> None:  # type: ignore[no-untyped-def]
    save_connection(session, "sam", token="keep-me", verifier=FakeJira(), **ARGS)
    save_connection(session, "sam", token=None, verifier=FakeJira(), **{**ARGS, "project": "OTHER"})

    stored = get_connection(session, "sam")
    assert stored is not None
    assert decrypt(stored.jira_token_encrypted) == "keep-me"
    assert stored.jira_project == "OTHER"


@needs_db
def test_first_save_without_a_token_is_refused(session) -> None:  # type: ignore[no-untyped-def]
    import pytest

    with pytest.raises(ValueError, match="必須填入"):
        save_connection(session, "nobody", token=None, verifier=FakeJira(), **ARGS)


@needs_db
def test_recovering_from_a_failed_verification(session) -> None:  # type: ignore[no-untyped-def]
    save_connection(session, "sam", token="bad", verifier=FakeJira(fail_with="炸了"), **ARGS)
    view = save_connection(session, "sam", token="good", verifier=FakeJira(), **ARGS)

    assert view.is_verified
    assert view.last_error is None
