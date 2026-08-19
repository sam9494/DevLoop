from typing import Any

from fastapi import FastAPI

from devloop.core.config import get_settings
from devloop.core.logging import configure

settings = get_settings()
configure(settings.log_level)

app = FastAPI(title="DevLoop", version="0.1.0")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "env": settings.env}


@app.get("/health/ready")
def ready() -> dict[str, Any]:
    """DB 連得上才算 ready —— JobRadar KAN-14 償還過的那筆債，這裡不重蹈。"""
    from sqlalchemy import text

    from devloop.db.session import session_scope

    with session_scope() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ready"}
