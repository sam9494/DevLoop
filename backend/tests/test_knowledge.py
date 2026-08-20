"""知識回想 —— 閉環唯一還沒閉的那一段。

判準來自 docs/prototype/devloop.html 第 ⑥ 步：
「R-007 這條風險是在 KAN-15 被記下的，但真正該處理它的是 KAN-16。
  沒有知識庫，這條風險會消失在聊天記錄裡；有了它，下一張卡一開始就被迫面對。」
"""

import json
from pathlib import Path

from devloop.db.models import Card, Decision, Risk
from devloop.graph.client import FakeGraph
from devloop.runner.claude import FakeRunner
from devloop.spec import service
from devloop.spec.knowledge import recall, terms_of
from tests.conftest import needs_db


def _card(session, key: str, title: str, labels=None) -> Card:  # type: ignore[no-untyped-def]
    card = Card(key=key, project="KAN", title=title, labels=labels or [])
    session.add(card)
    session.flush()
    return card


# ---------- 抽詞 ----------


def test_english_identifiers_are_picked_up() -> None:
    terms = terms_of("embedding + HNSW 索引")
    assert "embedding" in terms and "hnsw" in terms


def test_short_and_common_words_are_dropped() -> None:
    terms = terms_of("the API for Phase 0")
    assert "the" not in terms and "phase" not in terms
    assert "api" in terms


def test_labels_count_as_terms() -> None:
    assert "ingestion" in terms_of("打標模組", ["ingestion", "phase-0"])


def test_chinese_is_cut_into_runs() -> None:
    terms = terms_of("打標模組")
    assert "打標" in terms and "標模組" in terms


# ---------- 回想 ----------


@needs_db
def test_nothing_to_recall_yet(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session, "KAN-16", "打標模組")
    memory = recall(session, "KAN", card)

    assert memory.count == 0
    assert memory.as_prompt_block() == ""  # 不要塞空白段落進提示詞


@needs_db
def test_a_decision_from_another_card_is_recalled_by_keyword(session) -> None:  # type: ignore[no-untyped-def]
    old = _card(session, "KAN-15", "104 adapter")
    session.add(
        Decision(
            card_id=old.id, slug="source-choice", text="第一個來源接誰 → Remotive（有正式 API）"
        )
    )
    session.flush()

    new = _card(session, "KAN-20", "推薦 API + 前端 prototype")
    memory = recall(session, "KAN", new)

    assert memory.count == 1
    hit = memory.decisions[0]
    assert hit.source_card == "KAN-15"
    assert "api" in hit.reason.lower()


@needs_db
def test_a_cards_own_decisions_are_not_recalled_back_at_it(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session, "KAN-16", "打標模組 embedding")
    session.add(Decision(card_id=card.id, slug="x", text="embedding 用哪個模型"))
    session.flush()

    assert recall(session, "KAN", card).count == 0


@needs_db
def test_a_risk_assigned_to_this_card_always_surfaces(session) -> None:  # type: ignore[no-untyped-def]
    """判準那句話：KAN-15 記下的風險，KAN-16 一開始就被迫面對。"""
    old = _card(session, "KAN-15", "104 adapter")
    session.add(
        Risk(
            card_id=old.id,
            slug="chinese-jd-quality",
            text="中文 JD 品質未驗",
            owner_card_key="KAN-16",
        )
    )
    session.flush()

    # 標題完全沒有共同關鍵字，靠的是指派關係
    new = _card(session, "KAN-16", "打標模組")
    memory = recall(session, "KAN", new)

    assert memory.count == 1
    hit = memory.risks[0]
    assert hit.source_card == "KAN-15"
    assert hit.reason == "指派給這張卡"
    assert "中文 JD 品質未驗" in memory.as_prompt_block()


@needs_db
def test_a_resolved_risk_stops_haunting_people(session) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    old = _card(session, "KAN-15", "104 adapter")
    session.add(
        Risk(
            card_id=old.id,
            slug="x",
            text="中文 JD 品質未驗",
            owner_card_key="KAN-16",
            resolved_at=datetime.now(UTC),
        )
    )
    session.flush()

    assert recall(session, "KAN", _card(session, "KAN-16", "打標模組")).count == 0


@needs_db
def test_assigned_risks_outrank_keyword_hits(session) -> None:  # type: ignore[no-untyped-def]
    old = _card(session, "KAN-15", "104 adapter")
    session.add(Decision(card_id=old.id, slug="d", text="打標模組要不要快取"))
    session.add(Risk(card_id=old.id, slug="r", text="限速設定未驗", owner_card_key="KAN-16"))
    session.flush()

    memory = recall(session, "KAN", _card(session, "KAN-16", "打標模組"))
    assert memory.hits[0].reason == "指派給這張卡"


@needs_db
def test_the_prompt_block_tells_the_model_what_to_do_with_it(session) -> None:  # type: ignore[no-untyped-def]
    old = _card(session, "KAN-15", "104 adapter")
    session.add(Decision(card_id=old.id, slug="d", text="embedding 納入成本帳本"))
    session.flush()

    block = recall(session, "KAN", _card(session, "KAN-17", "embedding + HNSW")).as_prompt_block()
    assert "第 2 節「現況」必須明確處理上面每一條" in block
    assert "不要重新討論已經定案的東西" in block


# ---------- 端到端 ----------

REPORT = {
    "sections": [{"n": 3, "title": "關鍵決策點", "body_md": "x"}],
    "risks": [
        {"slug": "chinese-jd-quality", "text": "中文 JD 品質未驗", "owner_card": "KAN-16"},
        {"slug": "no-owner", "text": "限速策略還沒定", "owner_card": None},
    ],
    "questions": [
        {
            "slug": "source-choice",
            "type": "single",
            "section_n": 3,
            "prompt": "來源接誰",
            "options": [
                {"value": "a3", "label": "Remotive"},
                {"value": "none", "label": "以上皆非"},
            ],
        },
    ],
}


def _run(session, card, payload=REPORT):  # type: ignore[no-untyped-def]
    job = service.enqueue_generation(session, card, workspace=Path("/tmp"), permission_mode="plan")
    service.run_job(session, job, FakeRunner(result=json.dumps(payload, ensure_ascii=False)), 60)
    return job


@needs_db
def test_risks_land_in_the_database_with_their_owner(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session, "KAN-15", "104 adapter")
    _run(session, card)

    from sqlalchemy import select

    risks = {r.slug: r for r in session.scalars(select(Risk)).all()}
    assert risks["chinese-jd-quality"].owner_card_key == "KAN-16"
    assert risks["no-owner"].owner_card_key is None


@needs_db
def test_the_loop_actually_closes(session) -> None:  # type: ignore[no-untyped-def]
    """KAN-15 產報告記下風險 → 凍結 → KAN-16 開卡時提示詞裡就有它。"""
    k15 = _card(session, "KAN-15", "104 adapter")
    _run(session, k15)
    report = service.latest_report(session, k15)
    assert report is not None
    q = service.questions_of(session, report)[0]
    service.save_answer(session, q, choice="a3")
    outcome = service.freeze(session, report, FakeGraph())
    assert outcome.graph_error is None

    k16 = _card(session, "KAN-16", "打標模組")
    job = service.enqueue_generation(session, k16, workspace=Path("/tmp"), permission_mode="plan")

    assert "中文 JD 品質未驗" in job.prompt  # ← 這就是閉環
    assert "來自 KAN-15" in job.prompt
    assert "指派給這張卡" in job.prompt


@needs_db
def test_freezing_puts_risks_on_the_graph(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session, "KAN-15", "104 adapter")
    _run(session, card)
    report = service.latest_report(session, card)
    assert report is not None
    service.save_answer(session, service.questions_of(session, report)[0], choice="a3")

    graph = FakeGraph()
    service.freeze(session, report, graph)

    kinds = sorted({n.kind for n in graph.nodes.values()})
    assert kinds == ["Card", "Decision", "Risk"]
    assert any(kind == "RAISED" for _, _, kind in graph.edges)


@needs_db
def test_a_new_version_replaces_the_old_risks(session) -> None:  # type: ignore[no-untyped-def]
    card = _card(session, "KAN-15", "104 adapter")
    _run(session, card)
    _run(session, card, {**REPORT, "risks": [{"slug": "only-one", "text": "只剩這一條"}]})

    from sqlalchemy import select

    assert [r.slug for r in session.scalars(select(Risk)).all()] == ["only-one"]
