"""測試用的資料庫連線與相依覆寫。

每個測試跑在一個最後會被 rollback 的交易裡 —— 測完資料庫是乾淨的。
沒有資料庫時整批跳過，這樣沒開 docker 也還能跑其他測試。
"""

from collections.abc import Iterator

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
def session() -> Iterator[Session]:
    engine = sa.create_engine(get_settings().database_url)
    connection = engine.connect()
    transaction = connection.begin()
    s = Session(bind=connection, expire_on_commit=False)
    try:
        yield s
    finally:
        s.close()
        transaction.rollback()
        connection.close()
