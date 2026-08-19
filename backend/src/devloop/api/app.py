from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from devloop.api.deps import get_session, get_verifier
from devloop.core.config import get_settings
from devloop.core.crypto import SecretKeyMissingError
from devloop.core.logging import configure
from devloop.db.session import session_scope
from devloop.jira.client import JiraClient
from devloop.jira.connections import get_connection, save_connection, to_view

settings = get_settings()
configure(settings.log_level)

_ROOT = Path(__file__).resolve().parents[3]

app = FastAPI(title="DevLoop", version="0.1.0")
app.mount("/static", StaticFiles(directory=_ROOT / "static"), name="static")
templates = Jinja2Templates(directory=str(_ROOT / "templates"))

# 第一刀還沒有登入 —— 所有設定都掛在這個固定的 owner 底下。
# 加登入時只要把它換成 session 裡的使用者，schema 不必動。
OWNER_KEY = "sam"


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "env": settings.env}


@app.get("/health/ready")
def ready() -> dict[str, Any]:
    """DB 連得上才算 ready —— JobRadar KAN-14 償還過的那筆債，這裡不重蹈。"""
    from sqlalchemy import text

    with session_scope() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.get("/", response_class=RedirectResponse)
def index() -> RedirectResponse:
    return RedirectResponse("/settings", status_code=302)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> HTMLResponse:
    conn = get_connection(session, OWNER_KEY)
    view = to_view(conn) if conn else None
    return templates.TemplateResponse(request, "settings.html", {"conn": view})


@app.post("/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    verifier: Annotated[JiraClient | None, Depends(get_verifier)],
    site: Annotated[str, Form()],
    email: Annotated[str, Form()],
    project: Annotated[str, Form()],
    token: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        view = save_connection(
            session,
            OWNER_KEY,
            site=site.strip(),
            email=email.strip(),
            project=project.strip().upper(),
            token=token.strip() or None,
            verifier=verifier,
        )
    except SecretKeyMissingError as exc:
        return templates.TemplateResponse(
            request, "settings.html", {"conn": None, "fatal": str(exc)}, status_code=500
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "settings.html", {"conn": None, "fatal": str(exc)}, status_code=400
        )
    return templates.TemplateResponse(request, "settings.html", {"conn": view})
