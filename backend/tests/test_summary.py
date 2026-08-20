"""決策匯總 —— 存進資料庫只解決一半，看不到等於沒存。"""

import json
from pathlib import Path

from devloop.db.models import Card
from devloop.graph.client import FakeGraph
from devloop.runner.claude import FakeRunner
from devloop.spec import service
from devloop.spec import summary as summary_service
from tests.conftest import needs_db

REPORT = {
    "sections": [{"n": 3, "title": "關鍵決策點", "body_md": "x"}],
    "questions": [
        {
            "slug": "source-choice",
            "type": "single",
            "section_n": 3,
            "prompt": "第一個來源接誰",
            "options": [
                {"value": "a3", "label": "Remotive（有正式 API）"},
                {"value": "none", "label": "以上皆非"},
            ],
        },
        {"slug": "tenacity-dep", "type": "boolean", "section_n": 3, "prompt": "加 tenacity？"},
    ],
    "risks": [{"slug": "jd-quality", "text": "中文 JD 品質未驗", "owner_card": "KAN-16"}],
}


def _card(session, key: str, title: str = "t") -> Card:  # type: ignore[no-untyped-def]
    card = Card(key=key, project="KAN", title=title)
    session.add(card)
    session.flush()
    return card


def _gate(session, card, answers: dict[str, object], revise: bool = False):  # type: ignore[no-untyped-def]
    job = service.enqueue_generation(session, card, workspace=Path("/tmp"), permission_mode="plan")
    service.run_job(session, job, FakeRunner(result=json.dumps(REPORT, ensure_ascii=False)), 60)
    report = service.latest_report(session, card)
    assert report is not None

    if revise:
        service.request_changes(
            session,
            report,
            section_n=3,
            reason="不夠清楚",
            workspace=Path("/tmp"),
            permission_mode="plan",
        )
        job = service.enqueue_generation(
            session, card, workspace=Path("/tmp"), permission_mode="plan"
        )
        service.run_job(session, job, FakeRunner(result=json.dumps(REPORT, ensure_ascii=False)), 60)
        report = service.latest_report(session, card)
        assert report is not None

    by_slug = {q.slug: q for q in service.questions_of(session, report)}
    for slug, value in answers.items():
        q = by_slug[slug]
        if q.type == "boolean":
            service.save_answer(session, q, value=bool(value))
        elif value == "none":
            service.save_answer(session, q, choice="none", note="我要的是別的做法")
        else:
            service.save_answer(session, q, choice=str(value), note="之後再說")
    return service.freeze(session, report, FakeGraph())


# ---------- 內容 ----------


@needs_db
def test_an_empty_project_is_not_an_error(session) -> None:  # type: ignore[no-untyped-def]
    s = summary_service.build(session, "KAN")
    assert s.rows == []
    assert "還沒有任何已凍結的決策" in summary_service.as_markdown(s)


@needs_db
def test_only_frozen_decisions_show_up(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session, "KAN-15")
    job = service.enqueue_generation(session, card, workspace=Path("/tmp"), permission_mode="plan")
    service.run_job(session, job, FakeRunner(result=json.dumps(REPORT)), 60)
    report = service.latest_report(session, card)
    assert report is not None
    service.save_answer(session, service.questions_of(session, report)[0], choice="a3")

    # 答了但沒凍結 —— 不算數
    assert summary_service.build(session, "KAN").rows == []


@needs_db
def test_the_choice_is_shown_as_its_label_not_its_value(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session, "KAN-15", "104 adapter")
    _gate(session, card, {"source-choice": "a3", "tenacity-dep": True})

    rows = {r.slug: r for r in summary_service.build(session, "KAN").rows}
    assert rows["source-choice"].choice_label == "Remotive（有正式 API）"
    assert rows["source-choice"].note == "之後再說"
    assert rows["tenacity-dep"].choice_label == "是"
    assert rows["source-choice"].card_key == "KAN-15"


@needs_db
def test_none_of_above_is_flagged(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session, "KAN-15")
    _gate(session, card, {"source-choice": "none", "tenacity-dep": False})

    row = next(r for r in summary_service.build(session, "KAN").rows if r.slug == "source-choice")
    assert row.none_of_above
    assert row.choice_label == "以上皆非"
    assert row.note == "我要的是別的做法"


@needs_db
def test_another_project_is_not_mixed_in(session) -> None:  # type: ignore[no-untyped-def]
    _gate(session, _card(session, "KAN-15"), {"source-choice": "a3", "tenacity-dep": True})
    other = Card(key="DEV-1", project="DEV", title="別的專案")
    session.add(other)
    session.flush()
    _gate(session, other, {"source-choice": "a3", "tenacity-dep": True})

    assert {r.card_key for r in summary_service.build(session, "KAN").rows} == {"KAN-15"}


# ---------- 指標 ----------


@needs_db
def test_revise_rate_is_flagged_as_rubber_stamping_when_nobody_pushes_back(session) -> None:  # type: ignore[no-untyped-def]
    for key in ["KAN-15", "KAN-16", "KAN-17"]:
        _gate(session, _card(session, key), {"source-choice": "a3", "tenacity-dep": True})

    m = summary_service.build(session, "KAN").metrics
    assert m.gated_cards == 3
    assert m.revise_rate == 0
    assert "橡皮圖章" in m.revise_verdict


@needs_db
def test_a_healthy_revise_rate_is_recognised(session) -> None:  # type: ignore[no-untyped-def]
    _gate(
        session,
        _card(session, "KAN-15"),
        {"source-choice": "a3", "tenacity-dep": True},
        revise=True,
    )
    _gate(session, _card(session, "KAN-16"), {"source-choice": "a3", "tenacity-dep": True})
    _gate(session, _card(session, "KAN-17"), {"source-choice": "a3", "tenacity-dep": True})

    m = summary_service.build(session, "KAN").metrics
    assert m.revised_cards == 1
    assert abs((m.revise_rate or 0) - 1 / 3) < 0.01
    assert m.revise_verdict == "落在 PRD 目標區間"


@needs_db
def test_none_of_above_rate_counts_across_cards(session) -> None:  # type: ignore[no-untyped-def]
    _gate(session, _card(session, "KAN-15"), {"source-choice": "none", "tenacity-dep": True})
    _gate(session, _card(session, "KAN-16"), {"source-choice": "a3", "tenacity-dep": True})

    m = summary_service.build(session, "KAN").metrics
    assert m.decisions == 4
    assert m.none_of_above == 1
    assert m.none_of_above_rate == 0.25


@needs_db
def test_open_risks_are_counted(session) -> None:  # type: ignore[no-untyped-def]
    _gate(session, _card(session, "KAN-15"), {"source-choice": "a3", "tenacity-dep": True})
    assert summary_service.build(session, "KAN").metrics.open_risks == 1


@needs_db
def test_elapsed_time_is_measured_from_the_first_report(session) -> None:  # type: ignore[no-untyped-def]
    _gate(session, _card(session, "KAN-15"), {"source-choice": "a3", "tenacity-dep": True})

    m = summary_service.build(session, "KAN").metrics
    assert m.median_elapsed_minutes is not None
    assert m.median_elapsed_minutes >= 0  # 測試裡幾乎是 0，重點是有量到


# ---------- Markdown ----------


@needs_db
def test_the_markdown_is_something_you_could_paste_into_an_adr(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session, "KAN-15", "104 adapter")
    _gate(session, card, {"source-choice": "none", "tenacity-dep": True})

    text = summary_service.as_markdown(summary_service.build(session, "KAN"))

    assert text.startswith("# KAN 的技術決策紀錄")
    assert "## KAN-15 — 104 adapter" in text
    assert "### source-choice" in text
    assert "**問題**：第一個來源接誰" in text
    assert "**⚠️ 以上皆非**：以上皆非" in text
    assert "**備註**：我要的是別的做法" in text
    assert "要求修改比例" in text
    assert "未解的風險：1 條" in text
