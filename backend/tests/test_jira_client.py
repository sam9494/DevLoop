"""HttpJiraClient 打真實形狀的回應。

fixture 是從 Jira REST API v3 的真實 payload 錄下來的（issue、transitions、
使用者物件的欄位都照原樣），所以離線就能測，不需要憑證也不打真實網站。
"""

import json
from pathlib import Path

import httpx
import pytest

from devloop.jira.client import HttpJiraClient, JiraError

FIXTURES = Path(__file__).parent / "fixtures" / "jira"


def load(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((FIXTURES / f"{name}.json").read_text())


def client_with(handler) -> HttpJiraClient:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    return HttpJiraClient(
        "example.atlassian.net",
        "sam@example.com",
        "tok",
        client=httpx.Client(transport=transport),
    )


def ok(payload) -> httpx.Response:  # type: ignore[no-untyped-def]
    return httpx.Response(200, json=payload)


# ---------- verify ----------


def test_verify_returns_the_identity_behind_the_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        return ok(load("myself"))

    identity = client_with(handler).verify()

    assert identity.account_id == "557058:00000000-1111-2222-3333-444444444444"
    assert identity.display_name == "Example User"
    assert seen["url"] == "https://example.atlassian.net/rest/api/3/myself"
    assert seen["auth"].startswith("Basic ")  # email + token 走 basic auth


@pytest.mark.parametrize("status", [401, 403])
def test_a_bad_token_says_so_in_words_the_user_can_act_on(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=load("unauthorised"))

    with pytest.raises(JiraError, match="email 或 API token 不對"):
        client_with(handler).verify()


def test_a_wrong_site_is_reported_as_a_connection_problem() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nodename nor servname provided")

    with pytest.raises(JiraError, match="站台網址是不是打錯了"):
        client_with(handler).verify()


def test_the_site_can_be_given_with_or_without_the_scheme() -> None:
    for given in [
        "example.atlassian.net",
        "https://example.atlassian.net",
        "https://example.atlassian.net/",
    ]:
        c = HttpJiraClient(given, "s@x.com", "t")
        assert c._base == "https://example.atlassian.net"


# ---------- open_cards ----------


def test_open_cards_maps_the_real_issue_shape() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["jql"] = request.url.params.get("jql", "")
        return ok(load("search"))

    cards = client_with(handler).open_cards("KAN")

    assert seen["path"] == "/rest/api/3/search/jql"
    assert 'project = "KAN"' in seen["jql"]
    assert "statusCategory != Done" in seen["jql"]  # 只要未完成的卡

    assert [c.key for c in cards] == ["KAN-15", "KAN-16"]
    first = cards[0]
    assert first.status == "待辦事項"
    assert first.labels == ["ingestion", "phase-0", "week-1"]
    assert first.url == "https://example.atlassian.net/browse/KAN-15"


def test_missing_optional_fields_do_not_blow_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok(load("search"))

    second = client_with(handler).open_cards("KAN")[1]
    assert second.labels == []  # labels 是空陣列
    assert second.title  # summary 還在


def test_an_empty_project_is_an_empty_list_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok({"issues": []})

    assert client_with(handler).open_cards("KAN") == []


# ---------- transition ----------


def test_transition_looks_the_id_up_by_status_name() -> None:
    posted: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return ok(load("transitions"))
        posted["body"] = json.loads(request.content)
        posted["path"] = request.url.path
        return httpx.Response(204)

    client_with(handler).transition("KAN-15", "進行中")

    assert posted["path"] == "/rest/api/3/issue/KAN-15/transitions"
    assert posted["body"] == {"transition": {"id": "21"}}  # ← 名稱換成 id


def test_an_unknown_target_status_lists_what_is_actually_available() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok(load("transitions"))

    with pytest.raises(JiraError) as exc:
        client_with(handler).transition("KAN-15", "已部署")

    message = str(exc.value)
    assert "沒有通往「已部署」的轉換" in message
    assert "待辦事項" in message and "進行中" in message and "完成" in message


def test_a_rejected_transition_surfaces_the_status_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return ok(load("transitions"))
        return httpx.Response(400, text="Transition is not valid")

    with pytest.raises(JiraError, match="400"):
        client_with(handler).transition("KAN-15", "進行中")


def test_a_missing_issue_says_the_key_or_site_may_be_wrong() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})

    with pytest.raises(JiraError, match="站台網址或專案代號"):
        client_with(handler).transition("KAN-999", "進行中")


# ---------- ADF ----------


def test_the_card_description_actually_arrives() -> None:
    """真跑一次才發現的 bug：JQL 有要 description，組 Card 時卻漏掉了。

    後果是每一份規格報告都在看不到卡片內容的情況下產出 —— DEV-1 的報告
    第一句話就是「DEV-1 是空卡」，其實只是沒送過去。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return ok(load("search"))

    first = client_with(handler).open_cards("KAN")[0]

    assert "W1 收尾卡" in first.description
    assert "資料進得來" in first.description


def test_adf_is_flattened_into_readable_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok(load("search"))

    text = client_with(handler).open_cards("KAN")[0].description

    assert "驗收條件" in text  # heading
    assert "- SourceAdapter Protocol" in text  # bullet
    assert "def search(query): ..." in text  # code block


def test_an_unknown_adf_node_does_not_swallow_its_children() -> None:
    """最容易犯的錯：遇到沒見過的節點種類就整段丟掉。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return ok(load("search"))

    text = client_with(handler).open_cards("KAN")[0].description
    assert "這段藏在展開區塊裡，不能被丟掉。" in text


def test_a_card_without_a_description_is_empty_not_broken() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok(load("search"))

    assert client_with(handler).open_cards("KAN")[1].description == ""


def test_a_plain_string_description_still_works() -> None:
    from devloop.jira.client import adf_to_text

    assert adf_to_text("已經是字串了") == "已經是字串了"
    assert adf_to_text(None) == ""


def test_hard_breaks_and_mentions_survive() -> None:
    from devloop.jira.client import adf_to_text

    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "第一行"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "第二行 "},
                    {"type": "mention", "attrs": {"text": "@sam"}},
                ],
            }
        ],
    }
    text = adf_to_text(doc)
    assert "第一行\n第二行 @sam" in text
