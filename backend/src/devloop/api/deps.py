"""HTTP 層的相依 —— 抽出來是為了測試時能換掉真的資料庫、真的 Jira、真的 Claude Code。"""

from collections.abc import Iterator

from sqlalchemy.orm import Session

from devloop.db.session import SessionLocal
from devloop.graph.client import GraphStore
from devloop.jira.client import JiraClient
from devloop.runner.claude import LlmRunner


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


def get_jira_override() -> JiraClient | None:
    """路由要打 Jira 時先問這裡；None 代表照連線設定建真的 client。"""
    return None


def get_runner() -> LlmRunner | None:
    """None 代表用真的 Claude Code CLI。測試覆寫成 FakeRunner。"""
    return None


def get_graph() -> GraphStore | None:
    """None 代表用真的 Neo4j。測試覆寫成 FakeGraph。"""
    return None
