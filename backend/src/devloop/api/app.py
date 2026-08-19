from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from devloop.api.deps import get_jira_override, get_session, get_verifier
from devloop.api.render import to_html
from devloop.core.config import get_settings
from devloop.core.crypto import SecretKeyMissingError
from devloop.core.logging import configure
from devloop.db.models import Card, Connection, Job, Report, Section
from devloop.db.session import SessionLocal
from devloop.jira.client import JiraClient, JiraError
from devloop.jira.connections import client_for, get_connection, save_connection, to_view
from devloop.jira.sync import sync_cards
from devloop.runner.claude import ClaudeCliRunner
from devloop.runner.worker import Worker
from devloop.spec import service

settings = get_settings()
configure(settings.log_level)

_ROOT = Path(__file__).resolve().parents[3]

# 第一刀還沒有登入 —— 所有設定都掛在這個固定的 owner 底下。
# 加登入時只要把它換成 session 裡的使用者，schema 不必動。
OWNER_KEY = "sam"


def _handle_job(session: Session, job: Job) -> None:
    service.run_job(session, job, ClaudeCliRunner(settings.claude_bin), settings.claude_timeout_s)


worker = Worker(SessionLocal, _handle_job)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="DevLoop", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=_ROOT / "static"), name="static")
templates = Jinja2Templates(directory=str(_ROOT / "templates"))


def require_connection(session: Annotated[Session, Depends(get_session)]) -> Connection:
    """沒有已驗證的連線就拉不到卡，主流程整條空轉 —— 所以先擋到設定頁。"""
    conn = get_connection(session, OWNER_KEY)
    if conn is None or conn.verified_at is None:
        raise HTTPException(status_code=307, headers={"Location": "/settings"})
    return conn


SessionDep = Annotated[Session, Depends(get_session)]
ConnDep = Annotated[Connection, Depends(require_connection)]


def get_jira(
    conn: ConnDep, override: Annotated[JiraClient | None, Depends(get_jira_override)]
) -> JiraClient:
    return override if override is not None else client_for(conn)


JiraDep = Annotated[JiraClient, Depends(get_jira)]


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "env": settings.env}


@app.get("/health/ready")
def ready(session: SessionDep) -> dict[str, Any]:
    from sqlalchemy import text

    session.execute(text("SELECT 1"))
    return {"status": "ready"}


# ---------- 設定 ----------


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: SessionDep) -> HTMLResponse:
    conn = get_connection(session, OWNER_KEY)
    view = to_view(conn) if conn else None
    return templates.TemplateResponse(
        request, "settings.html", {"conn": view, "connection": view, "nav": "settings"}
    )


@app.post("/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    session: SessionDep,
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
    except (SecretKeyMissingError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {"conn": None, "connection": None, "nav": "settings", "fatal": str(exc)},
            status_code=400,
        )
    return templates.TemplateResponse(
        request, "settings.html", {"conn": view, "connection": view, "nav": "settings"}
    )


# ---------- 卡片列表 ----------


@dataclass
class CardRow:
    card: Card
    report: Report | None
    job_running: bool
    job_error: str | None

    @property
    def gate_class(self) -> str:
        if self.report is not None and self.report.state == "frozen":
            return "frozen"
        if self.report is not None:
            return "gate"
        return "running" if self.job_running else ""

    @property
    def gate_label(self) -> str:
        if self.report is not None and self.report.state == "frozen":
            return "已凍結"
        if self.report is not None:
            return "待審"
        return "產生中" if self.job_running else "尚未產生規格"


def _card_rows(session: Session) -> list[CardRow]:
    rows = []
    for card in session.scalars(select(Card).order_by(Card.key)).all():
        report = service.latest_report(session, card)
        jobs = list(
            session.scalars(
                select(Job).where(Job.card_id == card.id).order_by(Job.queued_at.desc()).limit(1)
            ).all()
        )
        last = jobs[0] if jobs else None
        rows.append(
            CardRow(
                card=card,
                report=report,
                job_running=last is not None and last.status in ("queued", "running"),
                job_error=last.error
                if last is not None and last.status in ("failed", "timeout")
                else None,
            )
        )
    return rows


@app.get("/", response_class=HTMLResponse)
def cards_page(
    request: Request, session: SessionDep, conn: ConnDep, synced: int = 0, error: str = ""
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "cards.html",
        {
            "cards": _card_rows(session),
            "connection": to_view(conn),
            "nav": "cards",
            "synced": synced or None,
            "error": error or None,
        },
    )


@app.post("/cards/sync")
def cards_sync(session: SessionDep, conn: ConnDep, jira: JiraDep) -> RedirectResponse:
    try:
        cards = sync_cards(session, jira, conn.jira_project)
    except JiraError as exc:
        return RedirectResponse(f"/?error={exc}", status_code=303)
    return RedirectResponse(f"/?synced={len(cards)}", status_code=303)


@app.post("/cards/{key}/generate")
def card_generate(key: str, session: SessionDep, conn: ConnDep) -> RedirectResponse:
    card = session.scalar(select(Card).where(Card.key == key))
    if card is None:
        raise HTTPException(status_code=404, detail=f"沒有 {key} 這張卡")
    service.enqueue_generation(
        session,
        card,
        workspace=settings.workspace_root,
        permission_mode="acceptEdits",
    )
    return RedirectResponse("/", status_code=303)


# ---------- 規格頁 ----------


@dataclass
class QuestionView:
    slug: str
    type: str
    prompt: str
    options: list[dict[str, Any]]
    choice: str | None
    value: bool | None
    text: str | None
    note: str
    is_answered: bool


def _build_view(session: Session, report: Report) -> tuple[list[Any], int, int]:
    answers = service.answers_of(session, report)
    questions = service.questions_of(session, report)
    missing = {q.slug for q in service.unanswered(session, report)}

    views: dict[Any, list[QuestionView]] = {}
    for q in questions:
        a = answers.get(q.slug)
        views.setdefault(q.section_id, []).append(
            QuestionView(
                slug=q.slug,
                type=q.type,
                prompt=q.prompt,
                options=list(q.options or []),
                choice=a.choice if a else None,
                value=a.value if a else None,
                text=a.text if a else None,
                note=(a.note if a else "") or "",
                is_answered=q.slug not in missing and a is not None,
            )
        )

    sections = []
    for section in session.scalars(
        select(Section).where(Section.report_id == report.id).order_by(Section.n)
    ).all():
        sections.append(
            {
                "n": section.n,
                "title": section.title,
                "body_html": to_html(section.body_md),
                "questions": views.get(section.id, []),
            }
        )

    required = [q for q in questions if q.required]
    return sections, len(required) - len(missing), len(required)


@app.get("/cards/{key}", response_class=HTMLResponse)
def card_page(
    request: Request, key: str, session: SessionDep, conn: ConnDep, message: str = ""
) -> HTMLResponse:
    card = session.scalar(select(Card).where(Card.key == key))
    if card is None:
        raise HTTPException(status_code=404, detail=f"沒有 {key} 這張卡")
    report = service.latest_report(session, card)
    if report is None:
        return RedirectResponse("/", status_code=303)  # type: ignore[return-value]

    sections, answered, required = _build_view(session, report)
    return templates.TemplateResponse(
        request,
        "card.html",
        {
            "card": card,
            "report": report,
            "sections": sections,
            "answered_count": answered,
            "required_count": required,
            "frozen": report.state == "frozen",
            "connection": to_view(conn),
            "nav": "cards",
            "message": message or None,
            "message_kind": "ok",
        },
    )


def _apply_form(session: Session, report: Report, form: dict[str, str]) -> None:
    for question in service.questions_of(session, report):
        slug = question.slug
        note = (form.get(f"note__{slug}") or "").strip()
        choice = form.get(f"choice__{slug}")
        raw_value = form.get(f"value__{slug}")
        text = form.get(f"text__{slug}")

        if choice is None and raw_value is None and not (text or "").strip() and not note:
            continue

        service.save_answer(
            session,
            question,
            choice=choice,
            value=None if raw_value is None else raw_value == "yes",
            text=text,
            note=note,
        )


@app.post("/cards/{key}/answers")
async def card_answers(key: str, request: Request, session: SessionDep, conn: ConnDep):  # type: ignore[no-untyped-def]
    card = session.scalar(select(Card).where(Card.key == key))
    if card is None:
        raise HTTPException(status_code=404, detail=f"沒有 {key} 這張卡")
    report = service.latest_report(session, card)
    if report is None or report.state == "frozen":
        return RedirectResponse(f"/cards/{key}", status_code=303)

    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        _apply_form(session, report, form)
    except service.SpecError as exc:
        return RedirectResponse(f"/cards/{key}?message={exc}", status_code=303)
    return RedirectResponse(f"/cards/{key}?message=答案存好了", status_code=303)


@app.post("/cards/{key}/freeze")
async def card_freeze(  # type: ignore[no-untyped-def]
    key: str, request: Request, session: SessionDep, conn: ConnDep, jira: JiraDep
):
    card = session.scalar(select(Card).where(Card.key == key))
    if card is None:
        raise HTTPException(status_code=404, detail=f"沒有 {key} 這張卡")
    report = service.latest_report(session, card)
    if report is None:
        return RedirectResponse("/", status_code=303)

    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        _apply_form(session, report, form)
        service.freeze(session, report)
    except service.SpecError as exc:
        return RedirectResponse(f"/cards/{key}?message={exc}", status_code=303)

    # 凍結成功才動 Jira —— 決策先落地，外部系統後動
    try:
        jira.transition(card.key, "進行中")
        card.jira_status = "進行中"
    except JiraError as exc:
        return RedirectResponse(
            f"/cards/{key}?message=已凍結，但 Jira 沒動到：{exc}", status_code=303
        )
    return RedirectResponse(f"/cards/{key}?message=已凍結，卡也移到進行中了", status_code=303)


@app.get("/cards/{key}/review.json", response_class=PlainTextResponse)
def card_review_json(key: str, session: SessionDep, conn: ConnDep) -> PlainTextResponse:
    card = session.scalar(select(Card).where(Card.key == key))
    if card is None:
        raise HTTPException(status_code=404, detail=f"沒有 {key} 這張卡")
    report = service.latest_report(session, card)
    if report is None:
        raise HTTPException(status_code=404, detail="這張卡還沒有報告")
    return PlainTextResponse(
        service.report_as_json(session, report),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{key}-review.json"'},
    )
