"""把 Jira 上未完成的卡鏡像進資料庫。

Jira 永遠是卡狀態的真相，這裡只是快取 —— 所以同步是覆寫，不做合併。
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from devloop.db.models import Card
from devloop.jira.client import JiraClient


def sync_cards(session: Session, client: JiraClient, project: str) -> list[Card]:
    remote = client.open_cards(project)
    existing = {
        c.key: c for c in session.scalars(select(Card).where(Card.project == project)).all()
    }
    now = datetime.now(UTC)

    for item in remote:
        card = existing.get(item.key)
        if card is None:
            card = Card(key=item.key)
            session.add(card)
        card.project = project
        card.title = item.title
        card.jira_status = item.status
        card.url = item.url
        card.labels = item.labels
        card.description = item.description
        card.synced_at = now

    session.flush()
    keys = [c.key for c in remote]
    if not keys:
        return []
    return list(session.scalars(select(Card).where(Card.key.in_(keys)).order_by(Card.key)).all())
