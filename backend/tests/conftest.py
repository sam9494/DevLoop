"""測試用的資料庫連線與相依覆寫。

每個測試跑在一個最後會被 rollback 的交易裡 —— 測完資料庫是乾淨的。
沒有資料庫時整批跳過，這樣沒開 docker 也還能跑其他測試。
"""

from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from devloop.core.config import get_settings


def _database_available() -> bool:
    try:
        engine = sa.create_engine(get_settings().database_url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _database_available(), reason="資料庫沒起來（make up）")


@pytest.fixture
def db_connection() -> Iterator[sa.Connection]:
    engine = sa.create_engine(get_settings().database_url)
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
