"""Jira 接點。

Protocol 在這裡定義，讓 spec 層永遠不知道 Jira 長什麼樣；
測試一律走 FakeJira，不打真實 API。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class Identity:
    account_id: str
    display_name: str
    email: str | None = None


@dataclass(frozen=True)
class Card:
    key: str
    title: str
    status: str
    url: str
    labels: list[str] = field(default_factory=list)
    description: str = ""


# ADF 裡「一個區塊結束」的節點。其餘節點一律往下遞迴，不認得的也照樣走 ——
# 遇到沒見過的節點就整段丟掉，是這個轉換最容易犯的錯。
_ADF_BLOCKS = {
    "paragraph",
    "heading",
    "listItem",
    "codeBlock",
    "blockquote",
    "rule",
    "panel",
    "tableRow",
    "mediaSingle",
}


def adf_to_text(node: Any) -> str:
    """Jira v3 的 description 是 ADF 巢狀文件，不是字串。

    只要能讀就好，不追求還原格式：抽出所有 text、在區塊邊界斷行、
    清單前面加一個 -。不認得的節點種類**繼續往下遞迴**，不整段丟掉。
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node

    out: list[str] = []

    def walk(item: Any, in_list: bool = False) -> None:
        if isinstance(item, list):
            for child in item:
                walk(child, in_list)
            return
        if not isinstance(item, dict):
            return

        kind = item.get("type")
        if kind == "text":
            out.append(str(item.get("text") or ""))
            return
        if kind == "hardBreak":
            out.append("\n")
            return
        if kind == "mention":
            out.append(str((item.get("attrs") or {}).get("text") or ""))
            return
        if kind == "listItem":
            out.append("\n- ")

        walk(item.get("content"), in_list or kind in ("bulletList", "orderedList"))

        if kind in _ADF_BLOCKS and kind != "listItem":
            out.append("\n")

    walk(node.get("content") if isinstance(node, dict) and "content" in node else node)
    text = "".join(out)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


class JiraError(RuntimeError):
    """憑證錯、站台錯、專案不存在 —— 一律走這裡，訊息要能直接顯示給使用者。"""


class JiraClient(Protocol):
    def verify(self) -> Identity: ...
    def open_cards(self, project: str) -> list[Card]: ...
    def transition(self, key: str, to_status: str) -> None: ...


class HttpJiraClient:
    def __init__(
        self,
        site: str,
        email: str,
        token: str,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = f"https://{site.strip().removeprefix('https://').rstrip('/')}"
        self._auth = (email, token)
        self._timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            r = self._client.get(f"{self._base}{path}", auth=self._auth, params=params)
        except httpx.HTTPError as exc:
            raise JiraError(f"連不上 {self._base} —— 站台網址是不是打錯了？") from exc
        if r.status_code in (401, 403):
            raise JiraError("Jira 拒絕了這組憑證：email 或 API token 不對，或 token 已失效")
        if r.status_code == 404:
            raise JiraError(f"Jira 找不到 {path} —— 站台網址或專案代號可能有誤")
        if r.status_code >= 400:
            raise JiraError(f"Jira 回了 {r.status_code}：{r.text[:200]}")
        payload: dict[str, Any] = r.json()
        return payload

    def verify(self) -> Identity:
        me = self._get("/rest/api/3/myself")
        return Identity(
            account_id=me["accountId"],
            display_name=me.get("displayName", ""),
            email=me.get("emailAddress"),
        )

    def open_cards(self, project: str) -> list[Card]:
        jql = f'project = "{project}" AND statusCategory != Done ORDER BY created ASC'
        data = self._get(
            "/rest/api/3/search/jql",
            {"jql": jql, "maxResults": "100", "fields": "summary,status,labels,description"},
        )
        cards = []
        for issue in data.get("issues", []):
            f = issue["fields"]
            cards.append(
                Card(
                    key=issue["key"],
                    title=f.get("summary", ""),
                    status=(f.get("status") or {}).get("name", ""),
                    url=f"{self._base}/browse/{issue['key']}",
                    labels=list(f.get("labels") or []),
                    description=adf_to_text(f.get("description")),
                )
            )
        return cards

    def transition(self, key: str, to_status: str) -> None:
        data = self._get(f"/rest/api/3/issue/{key}/transitions")
        match = [t for t in data.get("transitions", []) if t["to"]["name"] == to_status]
        if not match:
            available = ", ".join(t["to"]["name"] for t in data.get("transitions", []))
            raise JiraError(f"{key} 沒有通往「{to_status}」的轉換。可用的：{available}")
        r = self._client.post(
            f"{self._base}/rest/api/3/issue/{key}/transitions",
            auth=self._auth,
            json={"transition": {"id": match[0]["id"]}},
        )
        if r.status_code >= 400:
            raise JiraError(f"轉換 {key} 失敗：{r.status_code} {r.text[:200]}")


class FakeJira:
    """測試用。verify 失敗的情境靠 fail_with 觸發。"""

    def __init__(self, cards: list[Card] | None = None, fail_with: str | None = None) -> None:
        self._cards = cards or []
        self._fail_with = fail_with
        self.transitions: list[tuple[str, str]] = []
        self.verified_at: datetime | None = None

    def verify(self) -> Identity:
        if self._fail_with:
            raise JiraError(self._fail_with)
        self.verified_at = datetime.now(UTC)
        return Identity(account_id="acc-1", display_name="Sam", email="sam@example.com")

    def open_cards(self, project: str) -> list[Card]:
        if self._fail_with:
            raise JiraError(self._fail_with)
        return list(self._cards)

    def transition(self, key: str, to_status: str) -> None:
        self.transitions.append((key, to_status))
