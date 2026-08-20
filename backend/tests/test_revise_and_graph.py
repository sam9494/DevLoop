"""要求修改（閘門的第二顆鈕）與圖同步。"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from devloop.db.models import Card, Report
from devloop.graph.client import FakeGraph
from devloop.graph.rebuild import rebuild
from devloop.runner.claude import FakeRunner
from devloop.spec import service
from tests.conftest import needs_db
from tests.test_spec_service import REPORT

REPORT_V2 = {
    "sections": REPORT["sections"],
    "questions": [
        # 同一題沿用同一個 slug
        REPORT["questions"][0],
        # tenacity-dep 這版被拿掉，換成新的一題
        {"slug": "retry-policy", "type": "boolean", "section_n": 3, "prompt": "改用內建重試？"},
    ],
}


def _card(session) -> Card:  # type: ignore[no-untyped-def]
    card = Card(key="KAN-15", project="KAN", title="104 adapter", description="接 104")
    session.add(card)
    session.flush()
    return card


def _generate(session, card, payload=REPORT):  # type: ignore[no-untyped-def]
    job = service.enqueue_generation(
        session, card, workspace=Path("/tmp"), permission_mode="acceptEdits"
    )
    service.run_job(session, job, FakeRunner(result=json.dumps(payload)), timeout_s=60)
    return job


# ---------- 要求修改 ----------


@needs_db
def test_request_changes_marks_the_version_and_queues_a_revision(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None

    job = service.request_changes(
        session,
        report,
        section_n=3,
        reason="第 3 節沒寫出不做的代價",
        workspace=Path("/tmp"),
        permission_mode="plan",
    )

    assert report.state == "changes_requested"
    assert report.verdict_note == "第 3 節沒寫出不做的代價"
    assert job.kind == "revise_section"
    assert "第 3 節沒寫出不做的代價" in job.prompt
    assert "沿用原本的 slug" in job.prompt


@needs_db
def test_a_reason_is_required(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None

    with pytest.raises(service.SpecError, match="哪裡要改"):
        service.request_changes(
            session,
            report,
            section_n=3,
            reason="   ",
            workspace=Path("/tmp"),
            permission_mode="plan",
        )


@needs_db
def test_a_frozen_version_cannot_be_sent_back(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None
    q = {x.slug: x for x in service.questions_of(session, report)}
    service.save_answer(session, q["source-choice"], choice="a3")
    service.save_answer(session, q["tenacity-dep"], value=True)
    service.freeze(session, report)

    with pytest.raises(service.SpecError, match="凍結"):
        service.request_changes(
            session,
            report,
            section_n=3,
            reason="想改",
            workspace=Path("/tmp"),
            permission_mode="plan",
        )


@needs_db
def test_answers_carry_over_to_the_new_version_by_slug(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    v1 = service.latest_report(session, card)
    assert v1 is not None
    q1 = {x.slug: x for x in service.questions_of(session, v1)}
    service.save_answer(session, q1["source-choice"], choice="a3", note="104 之後再說")
    service.save_answer(session, q1["tenacity-dep"], value=True)

    service.request_changes(
        session,
        v1,
        section_n=3,
        reason="補上代價",
        workspace=Path("/tmp"),
        permission_mode="plan",
    )
    _generate(session, card, REPORT_V2)

    v2 = service.latest_report(session, card)
    assert v2 is not None and v2.id != v1.id
    assert v2.version == "v0.2"  # v0.1 被退回，重產的是 v0.2

    carried = service.answers_of(session, v2)
    assert carried["source-choice"].choice == "a3"
    assert carried["source-choice"].note == "104 之後再說"  # ← 同 slug，答案跟著走
    assert "retry-policy" not in carried  # ← 新題目沒有答案
    assert service.unanswered(session, v2) == [
        q for q in service.questions_of(session, v2) if q.slug == "retry-policy"
    ]


@needs_db
def test_the_old_version_is_kept_not_overwritten(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    v1 = service.latest_report(session, card)
    assert v1 is not None
    service.request_changes(
        session,
        v1,
        section_n=1,
        reason="目標寫太模糊",
        workspace=Path("/tmp"),
        permission_mode="plan",
    )
    _generate(session, card, REPORT_V2)

    versions = list(session.scalars(select(Report).where(Report.card_id == card.id)).all())
    assert len(versions) == 2
    assert {r.state for r in versions} == {"changes_requested", "draft"}


# ---------- 圖同步 ----------


@needs_db
def test_freezing_pushes_the_card_and_decisions_into_the_graph(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None
    q = {x.slug: x for x in service.questions_of(session, report)}
    service.save_answer(session, q["source-choice"], choice="a3")
    service.save_answer(session, q["tenacity-dep"], value=True)

    graph = FakeGraph()
    outcome = service.freeze(session, report, graph)

    assert outcome.graph_error is None
    kinds = {n.kind for n in graph.nodes.values()}
    assert kinds == {"Card", "Decision"}
    assert len(graph.edges) == 2  # 兩個決策各一條 PRODUCED
    assert all(kind == "PRODUCED" for _, _, kind in graph.edges)


@needs_db
def test_a_graph_failure_does_not_roll_back_the_freeze(session) -> None:  # type: ignore[no-untyped-def]
    class Broken(FakeGraph):
        def upsert_node(self, node) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError("Neo4j 沒開")

    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None
    q = {x.slug: x for x in service.questions_of(session, report)}
    service.save_answer(session, q["source-choice"], choice="a3")
    service.save_answer(session, q["tenacity-dep"], value=False)

    outcome = service.freeze(session, report, Broken())

    assert "Neo4j 沒開" in (outcome.graph_error or "")
    assert outcome.report.state == "frozen"  # 決策先落地
    assert len(outcome.decisions) == 2  # edges 表照樣有資料


@needs_db
def test_rebuild_reconstructs_the_whole_graph_from_postgres(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session)
    _generate(session, card)
    report = service.latest_report(session, card)
    assert report is not None
    q = {x.slug: x for x in service.questions_of(session, report)}
    service.save_answer(session, q["source-choice"], choice="a3")
    service.save_answer(session, q["tenacity-dep"], value=True)
    service.freeze(session, report, FakeGraph())  # 這個圖之後就丟掉

    # 假設 Neo4j 被清空或換了一台 —— 從 Postgres 應該長得回來
    fresh = FakeGraph()
    nodes, edges = rebuild(session, fresh)

    assert nodes == 3  # 1 張卡 + 2 個決策
    assert edges == 2
    assert str(card.id) in fresh.nodes
    assert fresh.nodes[str(card.id)].label == "KAN-15"


@needs_db
def test_rebuild_clears_before_writing(session) -> None:  # type: ignore[no-untyped-def]
    from devloop.graph.client import Node

    graph = FakeGraph()
    graph.upsert_node(Node(id="stale", kind="Card", label="不該留下的"))
    rebuild(session, graph)

    assert "stale" not in graph.nodes
