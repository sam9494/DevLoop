"""規格報告的產生、答題與凍結 —— 主流程的核心。"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from devloop.db.models import (
    Answer,
    Card,
    Decision,
    Edge,
    Job,
    Question,
    Report,
    Risk,
    Section,
)
from devloop.graph.client import Edge as GraphEdge
from devloop.graph.client import GraphStore, Node
from devloop.runner.claude import LlmRunner, extract_json_object
from devloop.spec.knowledge import recall
from devloop.spec.prompt import SECTIONS, build_prompt


class SpecError(RuntimeError):
    """流程上不該發生的事（凍結還沒答完的報告之類），訊息要能直接顯示。"""


@dataclass
class FreezeOutcome:
    report: Report
    decisions: list[Decision]
    edges: list[Edge]
    graph_error: str | None = None


def latest_report(session: Session, card: Card) -> Report | None:
    # 用 id 排序而不是 created_at：Postgres 的 now() 是「交易起始時間」，
    # 同一交易裡建的兩份報告時間戳會一模一樣。uuid7 是時間有序的，排序才確定。
    return session.scalar(
        select(Report).where(Report.card_id == card.id).order_by(Report.id.desc())
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
    memory = recall(session, card.project, card)
    prompt = build_prompt(
        card.key,
        card.title,
        card.description or "",
        knowledge=memory.as_prompt_block(),
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


def request_changes(
    session: Session,
    report: Report,
    *,
    section_n: int,
    reason: str,
    workspace: Path,
    permission_mode: str,
) -> Job:
    """要求修改：把這一版標成 changes_requested，排一個改版的 job。

    閘門的定義是 approve / 要求修改兩顆鈕（PRD §5 P0）。只有 approve 的話，
    「要求修改比例 20–40%」這個成功指標永遠量不到。
    """
    if report.state == "frozen":
        raise SpecError("已經凍結的版本不能再要求修改")
    if not reason.strip():
        raise SpecError("要求修改必須寫下哪裡要改")

    card = session.get(Card, report.card_id)
    if card is None:
        raise SpecError("報告沒有對應的卡")

    report.state = "changes_requested"
    report.verdict_note = reason.strip()
    session.flush()

    return enqueue_generation(
        session,
        card,
        workspace=workspace,
        permission_mode=permission_mode,
        revision_section=section_n,
        revision_reason=reason.strip(),
    )


def run_job(session: Session, job: Job, runner: LlmRunner, timeout_s: int) -> Job:
    """worker 拿到一個 job 之後走的路徑。成本與權限拒絕一律落地。"""
    job.status = "running"
    job.started_at = datetime.now(UTC)
    session.flush()

    outcome = runner.run(job.prompt, Path(job.cwd), job.permission_mode, timeout_s, job_id=job.id)

    job.claude_session_id = outcome.session_id
    job.cost_usd = outcome.cost_usd
    job.num_turns = outcome.num_turns
    job.permission_denials = outcome.permission_denials or None
    job.stdout = outcome.transcript or outcome.result_text
    job.finished_at = datetime.now(UTC)

    if not outcome.ok:
        if outcome.cancelled:
            job.status = "cancelled"
        elif "超過" in (outcome.error or ""):
            job.status = "timeout"
        else:
            job.status = "failed"
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

    previous = _previous_report(session, card, report)

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

    _replace_risks(session, card, payload.get("risks") or [])

    if previous is not None:
        _carry_over_answers(session, previous, report)
    return report


def _replace_risks(session: Session, card: Card, raw_risks: list[dict[str, Any]]) -> None:
    """一張卡的風險以最新一版報告為準 —— 改版後舊的那批就不算數了。"""
    for stale in session.scalars(select(Risk).where(Risk.card_id == card.id)).all():
        session.delete(stale)
    session.flush()

    for raw in raw_risks:
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        owner = raw.get("owner_card")
        session.add(
            Risk(
                card_id=card.id,
                slug=str(raw.get("slug") or "risk"),
                text=text,
                owner_card_key=str(owner).strip() if owner else None,
            )
        )
    session.flush()


def _previous_report(session: Session, card: Card, current: Report) -> Report | None:
    rows = list(
        session.scalars(
            select(Report).where(Report.card_id == card.id).order_by(Report.id.desc())
        ).all()
    )
    return next((r for r in rows if r.id != current.id), None)


def _carry_over_answers(session: Session, previous: Report, current: Report) -> None:
    """slug 跨版本穩定的用途就在這裡 —— 同一題沿用同一個 slug，答案才對得回來。"""
    old = answers_of(session, previous)
    for question in questions_of(session, current):
        source = old.get(question.slug)
        if source is None:
            continue
        session.add(
            Answer(
                question_id=question.id,
                choice=source.choice,
                value=source.value,
                text=source.text,
                none_of_above=source.none_of_above,
                note=source.note,
            )
        )
    session.flush()


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


def freeze(session: Session, report: Report, graph: GraphStore | None = None) -> FreezeOutcome:
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

    risks = list(session.scalars(select(Risk).where(Risk.card_id == card.id)).all())
    for risk in risks:
        session.merge(Edge(from_id=card.id, to_id=risk.id, kind="RAISED"))
    session.flush()

    report.state = "frozen"
    report.version = "v1.0"
    report.frozen_at = datetime.now(UTC)
    session.flush()

    # 圖是 edges 表的投影 —— 同步失敗不回捲凍結，之後 rebuild-graph 補得回來
    graph_error: str | None = None
    if graph is not None:
        try:
            sync_to_graph(graph, card, decisions, risks)
        except Exception as exc:
            graph_error = str(exc)

    return FreezeOutcome(report=report, decisions=decisions, edges=edges, graph_error=graph_error)


def sync_to_graph(
    graph: GraphStore,
    card: Card,
    decisions: list[Decision],
    risks: list[Risk] | None = None,
) -> None:
    graph.upsert_node(Node(id=str(card.id), kind="Card", label=card.key, summary=card.title))
    for risk in risks or []:
        graph.upsert_node(Node(id=str(risk.id), kind="Risk", label=risk.slug, summary=risk.text))
        graph.upsert_edge(GraphEdge(from_id=str(card.id), to_id=str(risk.id), kind="RAISED"))
    for decision in decisions:
        graph.upsert_node(
            Node(id=str(decision.id), kind="Decision", label=decision.slug, summary=decision.text)
        )
        graph.upsert_edge(GraphEdge(from_id=str(card.id), to_id=str(decision.id), kind="PRODUCED"))


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
