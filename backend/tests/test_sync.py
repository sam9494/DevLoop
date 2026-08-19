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
