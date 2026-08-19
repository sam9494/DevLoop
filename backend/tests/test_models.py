"""schema 層面的斷言 —— 不需要真的連資料庫。"""

from devloop.db.models import Answer, Base, Edge, Question


def test_all_expected_tables_declared() -> None:
    expected = {
        "cards",
        "reports",
        "sections",
        "questions",
        "answers",
        "jobs",
        "decisions",
        "edges",
    }
    assert expected <= set(Base.metadata.tables)


def test_none_of_above_requires_note_constraint_exists() -> None:
    names = {c.name for c in Answer.__table__.constraints if c.name}
    assert "answers_none_of_above_needs_note" in names


def test_question_slug_is_unique_per_report() -> None:
    uniques = [
        tuple(col.name for col in c.columns)
        for c in Question.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    ]
    assert ("report_id", "slug") in uniques


def test_edge_primary_key_is_the_triple() -> None:
    assert [c.name for c in Edge.__table__.primary_key.columns] == ["from_id", "to_id", "kind"]
