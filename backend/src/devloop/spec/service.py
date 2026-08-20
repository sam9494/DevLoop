"""規格報告的產生、答題與凍結 —— 主流程的核心。"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from devloop.db.models import Answer, Card, Decision, Edge, Job, Question, Report, Section
from devloop.runner.claude import LlmRunner, extract_json_object
from devloop.spec.prompt import SECTIONS, build_prompt


class SpecError(RuntimeError):
    """流程上不該發生的事（凍結還沒答完的報告之類），訊息要能直接顯示。"""


@dataclass
class FreezeOutcome:
    report: Report
    decisions: list[Decision]
    edges: list[Edge]


def latest_report(session: Session, card: Card) -> Report | None:
    return session.scalar(
        select(Report).where(Report.card_id == card.id).order_by(Report.created_at.desc())
    )


def _next_version(session: Session, card: Card, final: bool = False) -> str:
    if final:
        return "v1.0"
    count = len(list(session.scalars(select(Report).where(Report.card_id == card.id)).all()))
    return f"v0.{count + 1}"


def enqueue_generation(
    session: Session,
    card: Card,
    *,
    workspace: Path,
    permission_mode: str,
    revision_section: int | None = None,
    revision_reason: str = "",
) -> Job:
    """只排隊，不執行 —— 執行是 worker 的事，HTTP 請求不該等在那裡。"""
    prompt = build_prompt(
        card.key,
        card.title,
        card.description or "",
        revision_section=revision_section,
        revision_reason=revision_reason,
    )
    job = Job(
        card_id=card.id,
        kind="generate_spec" if revision_section is None else "revise_section",
        status="queued",
        prompt=prompt,
        cwd=str(workspace),
        permission_mode=permission_mode,
    )
    session.add(job)
    session.flush()
    return job


def run_job(session: Session, job: Job, runner: LlmRunner, timeout_s: int) -> Job:
    """worker 拿到一個 job 之後走的路徑。成本與權限拒絕一律落地。"""
    job.status = "running"
    job.started_at = datetime.now(UTC)
    session.flush()

    outcome = runner.run(job.prompt, Path(job.cwd), job.permission_mode, timeout_s)

    job.claude_session_id = outcome.session_id
    job.cost_usd = outcome.cost_usd
    job.num_turns = outcome.num_turns
    job.permission_denials = outcome.permission_denials or None
    job.stdout = outcome.transcript or outcome.result_text
    job.finished_at = datetime.now(UTC)

    if not outcome.ok:
        job.status = "timeout" if "超過" in (outcome.error or "") else "failed"
        job.error = outcome.error
        session.flush()
        return job

    card = session.get(Card, job.card_id) if job.card_id else None
    if card is None:
        job.status = "failed"
        job.error = "job 沒有對應的卡"
        session.flush()
        return job

    try:
        payload = extract_json_object(outcome.result_text)
        report = _persist_report(session, card, payload)
    except (ValueError, KeyError, TypeError) as exc:
        job.status = "failed"
        job.error = f"報告解析失敗：{exc}"
        session.flush()
        return job

    report.generated_by_job_id = job.id
    job.status = "succeeded"
    session.flush()
    return job


def _persist_report(session: Session, card: Card, payload: dict[str, Any]) -> Report:
    report = Report(card_id=card.id, version=_next_version(session, card), state="draft")
    session.add(report)
    session.flush()

    titles = dict(SECTIONS)
    by_n: dict[int, Section] = {}
    for raw in payload.get("sections", []):
        n = int(raw["n"])
        section = Section(
            report_id=report.id,
            n=n,
            title=str(raw.get("title") or titles.get(n, f"第 {n} 節")),
            body_md=str(raw.get("body_md") or ""),
        )
        session.add(section)
        by_n[n] = section
    session.flush()

    for ordinal, raw in enumerate(payload.get("questions", [])):
        section_n = raw.get("section_n")
        session.add(
            Question(
                report_id=report.id,
                section_id=by_n[section_n].id if section_n in by_n else None,
                slug=str(raw["slug"]),
                type=str(raw.get("type") or "single"),
                prompt=str(raw.get("prompt") or ""),
                options=raw.get("options") or None,
                required=bool(raw.get("required", True)),
                ordinal=ordinal,
            )
        )
    session.flush()
    return report


def questions_of(session: Session, report: Report) -> list[Question]:
    return list(
        session.scalars(
            select(Question).where(Question.report_id == report.id).order_by(Question.ordinal)
        ).all()
    )


def answers_of(session: Session, report: Report) -> dict[str, Answer]:
    rows = session.scalars(
        select(Answer).join(Question).where(Question.report_id == report.id)
    ).all()
    by_question = {a.question_id: a for a in rows}
    return {q.slug: by_question[q.id] for q in questions_of(session, report) if q.id in by_question}


def save_answer(
    session: Session,
    question: Question,
    *,
    choice: str | None = None,
    value: bool | None = None,
    text: str | None = None,
    note: str = "",
) -> Answer:
    none_of_above = choice == "none"
    if none_of_above and not note.strip():
        raise SpecError("選了「以上皆非」就必須寫下你要的是什麼")

    answer = session.scalar(select(Answer).where(Answer.question_id == question.id))
    if answer is None:
        answer = Answer(question_id=question.id)
        session.add(answer)
    answer.choice = choice
    answer.value = value
    answer.text = text
    answer.none_of_above = none_of_above
    answer.note = note
    answer.answered_at = datetime.now(UTC)
    session.flush()
    return answer


def _is_answered(question: Question, answer: Answer | None) -> bool:
    if answer is None:
        return False
    if answer.none_of_above:
        return bool((answer.note or "").strip())
    if question.type == "boolean":
        return answer.value is not None
    if question.type == "text":
        return bool((answer.text or "").strip())
    return answer.choice is not None


def unanswered(session: Session, report: Report) -> list[Question]:
    by_slug = answers_of(session, report)
    return [
        q
        for q in questions_of(session, report)
        if q.required and not _is_answered(q, by_slug.get(q.slug))
    ]


def _decision_text(question: Question, answer: Answer) -> str:
    if answer.none_of_above:
        return f"{question.prompt} → 以上皆非：{answer.note}"
    if question.type == "boolean":
        return f"{question.prompt} → {'是' if answer.value else '否'}"
    if question.type == "text":
        return f"{question.prompt} → {answer.text}"
    label = answer.choice or ""
    for option in question.options or []:
        if option.get("value") == answer.choice:
            label = str(option.get("label") or answer.choice)
            break
    return f"{question.prompt} → {label}"


def freeze(session: Session, report: Report) -> FreezeOutcome:
    """凍結：鎖版本、把答案萃成決策、寫進圖。沒答完不讓過。"""
    if report.state == "frozen":
        raise SpecError("這一版已經凍結過了")

    missing = unanswered(session, report)
    if missing:
        raise SpecError(
            "還有 " + str(len(missing)) + " 題沒答：" + "、".join(q.slug for q in missing)
        )

    card = session.get(Card, report.card_id)
    if card is None:
        raise SpecError("報告沒有對應的卡")

    by_slug = answers_of(session, report)
    decisions: list[Decision] = []
    edges: list[Edge] = []

    for question in questions_of(session, report):
        answer = by_slug.get(question.slug)
        if answer is None:
            continue
        decision = Decision(
            card_id=card.id,
            answer_id=answer.id,
            slug=question.slug,
            text=_decision_text(question, answer),
        )
        session.add(decision)
        session.flush()
        edge = Edge(from_id=card.id, to_id=decision.id, kind="PRODUCED")
        session.merge(edge)
        decisions.append(decision)
        edges.append(edge)

    report.state = "frozen"
    report.version = "v1.0"
    report.frozen_at = datetime.now(UTC)
    session.flush()
    return FreezeOutcome(report=report, decisions=decisions, edges=edges)


def report_as_json(session: Session, report: Report) -> str:
    """決策紀錄的可攜形式 —— 跟 docs/decisions/*.json 是同一個形狀。"""
    card = session.get(Card, report.card_id)
    by_slug = answers_of(session, report)
    payload = {
        "schema_version": 1,
        "card": card.key if card else None,
        "title": card.title if card else None,
        "report_version": report.version,
        "state": report.state,
        "frozen_at": report.frozen_at.isoformat() if report.frozen_at else None,
        "answers": [
            {
                "id": q.slug,
                "question": q.prompt,
                "choice": a.choice,
                "value": a.value,
                "text": a.text,
                "none_of_above": a.none_of_above,
                "note": a.note or "",
            }
            for q in questions_of(session, report)
            if (a := by_slug.get(q.slug)) is not None
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
