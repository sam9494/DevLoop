"""DevLoop 第一刀的資料表 —— 決策紀錄的真相來源。

慣例（抄 JobRadar）：uuid7 主鍵、enum 用 text + CHECK、原始 payload 落 JSONB、
時間一律 timestamptz。Neo4j 只是這裡的投影，隨時可從 edges 重建。
"""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from devloop.db.ids import uuid7

TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)


class Base(DeclarativeBase):
    pass


class Card(Base):
    """一張 Jira 卡在 DevLoop 這邊的鏡像。Jira 仍是卡狀態的真相。"""

    __tablename__ = "cards"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    key: Mapped[str] = mapped_column(unique=True)  # KAN-15
    title: Mapped[str]
    jira_status: Mapped[str | None]
    url: Mapped[str | None]
    labels: Mapped[list[str] | None] = mapped_column(JSONB)
    description: Mapped[str | None]
    synced_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=sa.func.now())


class Report(Base):
    """一張卡的一版規格報告。改版會產生新的一列，舊的留著。"""

    __tablename__ = "reports"
    __table_args__ = (
        sa.UniqueConstraint("card_id", "version"),
        sa.CheckConstraint(
            "state IN ('draft','changes_requested','frozen')", name="reports_state_check"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    card_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("cards.id"), index=True)
    version: Mapped[str]  # v0.1 / v0.2 / v1.0
    state: Mapped[str] = mapped_column(default="draft")
    verdict_note: Mapped[str | None]
    generated_by_job_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=sa.func.now())
    frozen_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


class Section(Base):
    """八節報告的其中一節。題目掛在節底下，所以題目才能穿插在內文中。"""

    __tablename__ = "sections"
    __table_args__ = (sa.UniqueConstraint("report_id", "n"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    report_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("reports.id"), index=True)
    n: Mapped[int] = mapped_column(sa.SmallInteger)
    title: Mapped[str]
    body_md: Mapped[str]


class Question(Base):
    """slug 跨版本穩定 —— 報告改版時同一題沿用同一個 slug（IMPL-01 / question-id）。"""

    __tablename__ = "questions"
    __table_args__ = (
        sa.UniqueConstraint("report_id", "slug"),
        sa.CheckConstraint("type IN ('single','boolean','text')", name="questions_type_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    report_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("reports.id"), index=True)
    section_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("sections.id"))
    slug: Mapped[str]  # source-choice / tenacity-dep
    type: Mapped[str]
    prompt: Mapped[str]
    options: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    required: Mapped[bool] = mapped_column(default=True)
    ordinal: Mapped[int] = mapped_column(sa.SmallInteger, default=0)


class Answer(Base):
    """none_of_above 為真時 note 必須有內容 —— 應用層以 pydantic 驗，資料層也擋一次。"""

    __tablename__ = "answers"
    __table_args__ = (
        sa.UniqueConstraint("question_id"),
        sa.CheckConstraint(
            "NOT none_of_above OR (note IS NOT NULL AND length(btrim(note)) > 0)",
            name="answers_none_of_above_needs_note",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    question_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("questions.id"), index=True)
    choice: Mapped[str | None]
    value: Mapped[bool | None]
    text: Mapped[str | None]
    none_of_above: Mapped[bool] = mapped_column(default=False)
    note: Mapped[str | None]
    answered_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=sa.func.now())


class Job(Base):
    """一次本地 Claude Code 呼叫。成本與權限拒絕都落地 —— 探針量到每次啟動約 US$0.09。"""

    __tablename__ = "jobs"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','timeout','cancelled')",
            name="jobs_status_check",
        ),
        sa.CheckConstraint("kind IN ('generate_spec','revise_section')", name="jobs_kind_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    card_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("cards.id"), index=True)
    kind: Mapped[str]
    status: Mapped[str] = mapped_column(default="queued", index=True)
    prompt: Mapped[str]
    cwd: Mapped[str]
    permission_mode: Mapped[str]
    claude_session_id: Mapped[str | None]
    cost_usd: Mapped[float | None] = mapped_column(sa.Numeric(10, 6))
    num_turns: Mapped[int | None] = mapped_column(sa.SmallInteger)
    permission_denials: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    git_head_before: Mapped[str | None] = mapped_column(sa.CHAR(40))
    git_head_after: Mapped[str | None] = mapped_column(sa.CHAR(40))
    stdout: Mapped[str | None]
    error: Mapped[str | None]
    queued_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=sa.func.now())
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


class Decision(Base):
    """凍結時從答案萃取出來的決策 —— 這是之後餵給下一張卡的東西。"""

    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    card_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("cards.id"), index=True)
    answer_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("answers.id"))
    slug: Mapped[str]
    text: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=sa.func.now())


class Edge(Base):
    """圖的邊。Neo4j 是這張表的投影 —— 砍掉可以整個重建（rebuild-graph）。"""

    __tablename__ = "edges"
    __table_args__ = (
        sa.CheckConstraint(
            "kind IN ('PRODUCED','FEEDS','RAISED','REVISES')", name="edges_kind_check"
        ),
    )

    from_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    to_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=sa.func.now())
