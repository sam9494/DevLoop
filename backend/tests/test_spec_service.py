"""規格流程：產生 → 答題 → 凍結。"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from devloop.db.models import Card, Decision, Edge
from devloop.runner.claude import FakeRunner
from devloop.spec import service
from tests.conftest import needs_db

REPORT = {
    "sections": [
        {"n": 1, "title": "目標", "body_md": "讓資料進得來"},
        {"n": 3, "title": "關鍵決策點", "body_md": "見下"},
    ],
    "questions": [
        {
            "slug": "source-choice",
            "type": "single",
            "section_n": 3,
            "prompt": "第一個來源接誰",
            "options": [
                {"value": "a3", "label": "Remotive", "cost": "英文為主", "recommended": True},
                {"value": "none", "label": "以上皆非"},
            ],
        },
        {"slug": "tenacity-dep", "type": "boolean", "section_n": 3, "prompt": "加 tenacity？"},
        {
            "slug": "extra-notes",
            "type": "text",
            "section_n": 3,
            "prompt": "其他條件",
            "required": False,
        },
    ],
}


def _card(session) -> Card:  # type: ignore[no-untyped-def]
    card = Card(key="KAN-15", title="104 adapter", description="接 104")
    session.add(card)
    session.flush()
    return card


def _generate(session, card, payload=REPORT):  # type: ignore[no-untyped-def]
    job = service.enqueue_generation(
        session, card, workspace=Path("/tmp"), permission_mode="acceptEdits"
    )
    runner = FakeRunner(result=json.dumps(payload, ensure_ascii=False))
    service.run_job(session, job, runner, timeout_s=60)
    return job, runner


@needs_db
def test_generation_persists_sections_and_questions(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    job, _ = _generate(session, card)

    assert job.status == "succeeded"
    assert job.cost_usd == 0.09  # 成本一定要落地
    report = service.latest_report(session, card)
    assert report is not None and report.version == "v0.1"
    assert [q.slug for q in service.questions_of(session, report)] == [
        "source-choice",
        "tenacity-dep",
        "extra-notes",
    ]


@needs_db
def test_the_prompt_carries_the_card_and_the_cwd_is_the_workspace(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _, runner = _generate(session, card)

    prompt, cwd, mode = runner.calls[0]
    assert "KAN-15" in prompt and "接 104" in prompt
    assert cwd == Path("/tmp")  # ← 作用範圍的防線
    assert mode == "acceptEdits"


@needs_db
def test_a_failed_run_records_the_error_and_makes_no_report(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    job = service.enqueue_generation(session, card, workspace=Path("/tmp"), permission_mode="plan")
    service.run_job(session, job, FakeRunner(error="超過 900 秒沒跑完，已中止"), timeout_s=60)

    assert job.status == "timeout"
    assert service.latest_report(session, card) is None


@needs_db
def test_unparseable_output_fails_the_job_rather_than_saving_junk(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    job = service.enqueue_generation(session, card, workspace=Path("/tmp"), permission_mode="plan")
    service.run_job(session, job, FakeRunner(result="我今天不想輸出 JSON"), timeout_s=60)

    assert job.status == "failed"
    assert "解析失敗" in (job.error or "")
    assert service.latest_report(session, card) is None


@needs_db
def test_optional_questions_do_not_block_the_gate(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None

    assert {q.slug for q in service.unanswered(session, report)} == {
        "source-choice",
        "tenacity-dep",
    }


@needs_db
def test_freeze_is_refused_until_every_required_question_is_answered(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None

    with pytest.raises(service.SpecError, match="沒答"):
        service.freeze(session, report)


@needs_db
def test_none_of_above_without_a_note_is_refused(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None
    question = service.questions_of(session, report)[0]

    with pytest.raises(service.SpecError, match="以上皆非"):
        service.save_answer(session, question, choice="none", note="  ")


@needs_db
def test_freezing_locks_the_version_and_writes_decisions_into_the_graph(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None
    q = {x.slug: x for x in service.questions_of(session, report)}

    service.save_answer(session, q["source-choice"], choice="a3")
    service.save_answer(session, q["tenacity-dep"], value=True)
    outcome = service.freeze(session, report)

    assert outcome.report.state == "frozen"
    assert outcome.report.version == "v1.0"
    assert outcome.report.frozen_at is not None

    texts = [d.text for d in session.scalars(select(Decision)).all()]
    assert "第一個來源接誰 → Remotive" in texts  # 存的是標籤，不是 a3
    assert "加 tenacity？ → 是" in texts

    edges = list(session.scalars(select(Edge)).all())
    assert {e.kind for e in edges} == {"PRODUCED"}
    assert all(e.from_id == card.id for e in edges)


@needs_db
def test_freezing_twice_is_refused(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None
    q = {x.slug: x for x in service.questions_of(session, report)}
    service.save_answer(session, q["source-choice"], choice="a3")
    service.save_answer(session, q["tenacity-dep"], value=False)
    service.freeze(session, report)

    with pytest.raises(service.SpecError, match="已經凍結"):
        service.freeze(session, report)


@needs_db
def test_none_of_above_with_a_note_counts_as_answered_and_becomes_a_decision(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None
    q = {x.slug: x for x in service.questions_of(session, report)}

    service.save_answer(session, q["source-choice"], choice="none", note="我要先接 104")
    service.save_answer(session, q["tenacity-dep"], value=True)
    outcome = service.freeze(session, report)

    texts = [d.text for d in outcome.decisions]
    assert "第一個來源接誰 → 以上皆非：我要先接 104" in texts


@needs_db
def test_review_json_has_the_same_shape_as_the_specgate_records(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None
    q = {x.slug: x for x in service.questions_of(session, report)}
    service.save_answer(session, q["source-choice"], choice="a3", note="之後再說")
    service.save_answer(session, q["tenacity-dep"], value=True)
    service.freeze(session, report)

    payload = json.loads(service.report_as_json(session, report))
    assert payload["card"] == "KAN-15"
    assert payload["report_version"] == "v1.0"
    first = payload["answers"][0]
    assert first["id"] == "source-choice"  # 語義 slug，不是 A/B/C
    assert first["note"] == "之後再說"
