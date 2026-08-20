"""專案根目錄的檢查 —— claude 只能在這個目錄底下動作，所以要先看清楚它是什麼。"""

import pytest

from devloop.jira.client import FakeJira
from devloop.jira.connections import inspect_workspace, save_connection
from tests.conftest import needs_db

BASE = {"site": "t.atlassian.net", "email": "s@x.com", "project": "DEV"}


def test_a_real_git_repo_is_clean(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".git").mkdir()
    status = inspect_workspace(str(tmp_path))

    assert status.is_dir and status.is_git_repo
    assert status.error is None
    assert status.warning is None


def test_a_directory_without_git_is_usable_but_warned(tmp_path) -> None:  # type: ignore[no-untyped-def]
    status = inspect_workspace(str(tmp_path))

    assert status.error is None  # 還是能跑
    assert "沒有還原點" in (status.warning or "")  # 但你該知道


def test_a_missing_path_is_an_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    status = inspect_workspace(str(tmp_path / "不存在"))
    assert "找不到這個路徑" in (status.error or "")


def test_a_file_is_not_a_workspace(tmp_path) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "README.md"
    f.write_text("x")
    assert "不是資料夾" in (inspect_workspace(str(f)).error or "")


def test_an_empty_path_is_an_error() -> None:
    assert "還沒設定" in (inspect_workspace("").error or "")


def test_home_shorthand_is_expanded() -> None:
    assert inspect_workspace("~").is_dir


@needs_db
def test_the_workspace_is_stored_next_to_the_project_code(session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".git").mkdir()
    view = save_connection(
        session, "sam", token="tok", workspace=str(tmp_path), verifier=FakeJira(), **BASE
    )

    assert view.workspace.path == str(tmp_path)
    assert view.workspace.is_git_repo
    assert view.is_usable


@needs_db
def test_a_broken_workspace_makes_the_connection_unusable_even_when_jira_is_fine(  # type: ignore[no-untyped-def]
    session, tmp_path
) -> None:
    view = save_connection(
        session,
        "sam",
        token="tok",
        workspace=str(tmp_path / "沒這個"),
        verifier=FakeJira(),
        **BASE,
    )

    assert view.is_verified  # Jira 那邊是通的
    assert not view.is_usable  # 但還是不能跑 job
    assert "找不到這個路徑" in (view.workspace.error or "")


@needs_db
def test_switching_project_also_switches_workspace(session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """DevLoop 完成後要做的第一件事：從管 DEV 切到管 KAN，不用改任何檔案。"""
    dev = tmp_path / "DevLoop"
    kan = tmp_path / "JobRadar"
    for d in (dev, kan):
        (d / ".git").mkdir(parents=True)

    save_connection(session, "sam", token="tok", workspace=str(dev), verifier=FakeJira(), **BASE)
    view = save_connection(
        session,
        "sam",
        token=None,
        workspace=str(kan),
        verifier=FakeJira(),
        **{**BASE, "project": "KAN"},
    )

    assert view.jira_project == "KAN"
    assert view.workspace.path == str(kan)
    assert view.is_usable


@needs_db
def test_the_first_save_still_needs_a_token(session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="必須填入"):
        save_connection(
            session,
            "nobody",
            token=None,
            workspace=str(tmp_path),
            verifier=FakeJira(),
            **BASE,
        )
