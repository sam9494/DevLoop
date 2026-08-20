from devloop.jira.client import Card as RemoteCard
from devloop.jira.client import FakeJira
from devloop.jira.sync import sync_cards
from tests.conftest import needs_db

K15 = RemoteCard(
    key="KAN-15",
    title="104 adapter",
    status="待辦事項",
    url="https://x/browse/KAN-15",
    labels=["phase-0"],
)


@needs_db
def test_sync_inserts_new_cards(session) -> None:  # type: ignore[no-untyped-def]
    cards = sync_cards(session, FakeJira([K15]), "KAN")
    assert [c.key for c in cards] == ["KAN-15"]
    assert cards[0].labels == ["phase-0"]


@needs_db
def test_sync_overwrites_instead_of_duplicating(session) -> None:  # type: ignore[no-untyped-def]
    sync_cards(session, FakeJira([K15]), "KAN")
    moved = RemoteCard(key="KAN-15", title="104 adapter（改標題）", status="進行中", url=K15.url)
    cards = sync_cards(session, FakeJira([moved]), "KAN")

    assert len(cards) == 1
    assert cards[0].title == "104 adapter（改標題）"
    assert cards[0].jira_status == "進行中"


@needs_db
def test_empty_project_is_not_an_error(session) -> None:  # type: ignore[no-untyped-def]
    assert sync_cards(session, FakeJira([]), "KAN") == []


@needs_db
def test_cards_are_tagged_with_the_project_they_came_from(session) -> None:  # type: ignore[no-untyped-def]
    cards = sync_cards(session, FakeJira([K15]), "KAN")
    assert cards[0].project == "KAN"


@needs_db
def test_syncing_one_project_does_not_touch_another(session) -> None:  # type: ignore[no-untyped-def]
    """換專案時舊專案的卡要留著但不混進來 —— DevLoop 從管 KAN 改成管自己時踩到的。"""
    from devloop.jira.client import Card as RemoteCard

    sync_cards(session, FakeJira([K15]), "KAN")
    dev = RemoteCard(
        key="DEV-1", title="閉環開發系統", status="進行中", url="https://x/browse/DEV-1"
    )
    returned = sync_cards(session, FakeJira([dev]), "DEV")

    assert [c.key for c in returned] == ["DEV-1"]  # 只回傳這個專案的

    from sqlalchemy import select

    from devloop.db.models import Card

    everything = {c.key: c.project for c in session.scalars(select(Card)).all()}
    assert everything == {"KAN-15": "KAN", "DEV-1": "DEV"}  # 舊的還在，沒被清掉
