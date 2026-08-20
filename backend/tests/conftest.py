"""測試用的資料庫。

**跑在獨立的 devloop_test 資料庫上**，不碰開發用的那個 —— 一開始沒分開，
結果同步進來的真實 Jira 卡（KAN-15…）跟測試建的卡撞主鍵，21 個測試一起紅。

每個測試跑在一個最後會被 rollback 的交易裡，測完資料庫是乾淨的。
"""

from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from devloop.core.config import get_settings
from devloop.db.models import Base


def _render(url: sa.engine.URL) -> str:
    # str(URL) 會把密碼遮成 ***，拿去連線一定認證失敗。踩過一次。
    return url.render_as_string(hide_password=False)


def _test_database_url() -> str:
    url = sa.engine.make_url(get_settings().database_url)
    return _render(url.set(database=(url.database or "devloop") + "_test"))


def _ensure_test_database() -> bool:
    """建立測試資料庫與資料表。連不上就讓整批測試跳過。"""
    target = sa.engine.make_url(_test_database_url())
    admin = sa.create_engine(
        _render(target.set(database="postgres")),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 2},
    )
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            ).scalar()
            if not exists:
                conn.execute(sa.text(f'CREATE DATABASE "{target.database}"'))
    except Exception:
        return False
    finally:
        admin.dispose()

    engine = sa.create_engine(_render(target))
    try:
        Base.metadata.create_all(engine)
    except Exception:
        return False
    finally:
        engine.dispose()
    return True


_READY = _ensure_test_database()

needs_db = pytest.mark.skipif(not _READY, reason="測試資料庫起不來（make up）")


@pytest.fixture
def db_connection() -> Iterator[sa.Connection]:
    engine = sa.create_engine(_test_database_url())
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def session(db_connection: sa.Connection) -> Iterator[Session]:
    s = Session(bind=db_connection, expire_on_commit=False)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def session_factory(db_connection: sa.Connection) -> Callable[[], Session]:
    """worker 會自己開關 session —— 給它獨立的，但綁在同一條連線上，
    這樣測試看得到它寫的東西，而它 close() 也不會弄壞測試的 session。"""

    def make() -> Session:
        return Session(bind=db_connection, expire_on_commit=False)

    return make
