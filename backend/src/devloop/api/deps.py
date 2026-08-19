"""HTTP 層的相依 —— 抽出來是為了測試時能換掉真的資料庫與真的 Jira。"""

from collections.abc import Iterator

from sqlalchemy.orm import Session

from devloop.db.session import SessionLocal
from devloop.jira.client import JiraClient


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_verifier() -> JiraClient | None:
    """None 代表「用這筆連線自己的憑證去打真的 Jira」。測試時覆寫成 FakeJira。"""
    return None
