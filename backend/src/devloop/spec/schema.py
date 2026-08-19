"""規格報告的形狀 —— 這是整個系統的合約，頁面與儲存怎麼變，這裡盡量不動。

對應 SpecGate PRD §6 的資料模型，加上 IMPL-01 定案的兩件事：
question id 用跨版本穩定的語義 slug、每題附「以上皆非（說明必填）」。
"""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

QuestionType = Literal["single", "boolean", "text"]
ReportState = Literal["draft", "changes_requested", "frozen"]

NONE_OF_ABOVE = "none"


class Option(BaseModel):
    value: str
    label: str
    detail: str = ""
    cost: str = ""
    recommended: bool = False


class Question(BaseModel):
    """slug 跨版本穩定：題目還在就沿用，換了問題就換 slug。"""

    slug: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    type: QuestionType
    prompt: str
    options: list[Option] = []
    section_n: int | None = None
    required: bool = True

    @model_validator(mode="after")
    def single_choice_needs_options(self) -> Self:
        if self.type == "single" and not self.options:
            raise ValueError(f"單選題 {self.slug} 沒有選項")
        return self

    @model_validator(mode="after")
    def single_choice_offers_none_of_above(self) -> Self:
        # IMPL-01 / none-of-above：選項沒涵蓋到時要有出口，否則會逼出「隨便勾一個」
        if self.type == "single" and all(o.value != NONE_OF_ABOVE for o in self.options):
            raise ValueError(f"單選題 {self.slug} 缺少「以上皆非」選項")
        return self


class Answer(BaseModel):
    question_slug: str
    choice: str | None = None
    value: bool | None = None
    text: str | None = None
    note: str = ""

    @property
    def none_of_above(self) -> bool:
        return self.choice == NONE_OF_ABOVE

    @model_validator(mode="after")
    def none_of_above_requires_note(self) -> Self:
        if self.none_of_above and not self.note.strip():
            raise ValueError("選了「以上皆非」就必須寫下你要的是什麼")
        return self

    @property
    def answered(self) -> bool:
        if self.none_of_above:
            return bool(self.note.strip())
        return self.choice is not None or self.value is not None or bool((self.text or "").strip())


class Section(BaseModel):
    n: int
    title: str
    body_md: str


class SpecReport(BaseModel):
    """一版報告。改版時 version 遞增，答案靠 question slug 對回來。"""

    card: str
    title: str
    version: str
    state: ReportState = "draft"
    sections: list[Section] = []
    questions: list[Question] = []
    created_at: datetime | None = None

    @model_validator(mode="after")
    def slugs_are_unique(self) -> Self:
        slugs = [q.slug for q in self.questions]
        if len(slugs) != len(set(slugs)):
            raise ValueError("同一版報告裡有重複的 question slug")
        return self

    def unanswered(self, answers: list[Answer]) -> list[str]:
        """回傳還沒被正面回答的必答題 slug。沒答完不能凍結。"""
        by_slug = {a.question_slug: a for a in answers}
        return [
            q.slug
            for q in self.questions
            if q.required and not (by_slug.get(q.slug) or Answer(question_slug=q.slug)).answered
        ]
