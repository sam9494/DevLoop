import pytest
from pydantic import ValidationError

from devloop.spec.schema import Answer, Option, Question, SpecReport

NONE = Option(value="none", label="以上皆非 —— 我要的是別的")


def _single(slug: str = "source-choice") -> Question:
    return Question(
        slug=slug,
        type="single",
        prompt="第一個來源接誰",
        options=[Option(value="a3", label="Remotive"), NONE],
    )


def test_none_of_above_without_note_is_rejected() -> None:
    with pytest.raises(ValidationError, match="以上皆非"):
        Answer(question_slug="source-choice", choice="none")


def test_none_of_above_with_note_is_accepted_and_counts_as_answered() -> None:
    a = Answer(question_slug="source-choice", choice="none", note="我要的是別的做法")
    assert a.none_of_above and a.answered


def test_blank_note_is_not_a_note() -> None:
    with pytest.raises(ValidationError):
        Answer(question_slug="source-choice", choice="none", note="   ")


def test_single_choice_must_offer_none_of_above() -> None:
    with pytest.raises(ValidationError, match="以上皆非"):
        Question(
            slug="source-choice",
            type="single",
            prompt="第一個來源接誰",
            options=[Option(value="a3", label="Remotive")],
        )


def test_slug_must_be_kebab_case() -> None:
    with pytest.raises(ValidationError):
        _single(slug="Source_Choice")


def test_duplicate_slugs_in_one_report_are_rejected() -> None:
    with pytest.raises(ValidationError, match="重複"):
        SpecReport(
            card="KAN-15",
            title="t",
            version="v0.1",
            questions=[_single(), _single()],
        )


def test_unanswered_lists_required_questions_only() -> None:
    optional = _single(slug="nice-to-have")
    optional.required = False
    report = SpecReport(card="KAN-15", title="t", version="v0.1", questions=[_single(), optional])
    assert report.unanswered([]) == ["source-choice"]
    answered = [Answer(question_slug="source-choice", choice="a3")]
    assert report.unanswered(answered) == []
