"""決策匯總。

PRD §1 說決策會被沖走，是因為它「只存在聊天記錄裡」。存進資料庫只解決一半 ——
看不到等於沒存。這一頁把一個專案做過的所有決定攤在同一張紙上。

指標的誠實聲明：我們量得到的是**牆上時鐘**（報告產出 → 凍結），量不到 Sam
真的盯著螢幕的時間。所以顯示的是「產出→凍結」，不是「審核耗時」。
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from devloop.db.models import Answer, Card, Decision, Question, Report, Risk


@dataclass
class DecisionRow:
    card_key: str
    card_title: str
    slug: str
    question: str
    choice_label: str
    note: str
    none_of_above: bool
    decided_at: datetime


@dataclass
class Metrics:
    gated_cards: int = 0
    revised_cards: int = 0
    decisions: int = 0
    none_of_above: int = 0
    open_risks: int = 0
    elapsed_minutes: list[float] = field(default_factory=list)

    @property
    def revise_rate(self) -> float | None:
        if not self.gated_cards:
            return None
        return self.revised_cards / self.gated_cards

    @property
    def revise_verdict(self) -> str:
        """PRD §9：太低代表橡皮圖章，太高代表報告品質不穩。這個指標同時監督兩邊。"""
        rate = self.revise_rate
        if rate is None:
            return "還沒有資料"
        if rate < 0.2:
            return "偏低 —— 可能在當橡皮圖章"
        if rate > 0.4:
            return "偏高 —— 報告品質可能不穩"
        return "落在 PRD 目標區間"

    @property
    def none_of_above_rate(self) -> float | None:
        if not self.decisions:
            return None
        return self.none_of_above / self.decisions

    @property
    def median_elapsed_minutes(self) -> float | None:
        if not self.elapsed_minutes:
            return None
        ordered = sorted(self.elapsed_minutes)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2


@dataclass
class Summary:
    project: str
    rows: list[DecisionRow] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)


def _choice_label(question: Question | None, answer: Answer | None) -> str:
    if question is None or answer is None:
        return ""
    if answer.none_of_above:
        return "以上皆非"
    if question.type == "boolean":
        return "是" if answer.value else "否"
    if question.type == "text":
        return (answer.text or "").strip()
    for option in question.options or []:
        if option.get("value") == answer.choice:
            return str(option.get("label") or answer.choice or "")
    return answer.choice or ""


def build(session: Session, project: str) -> Summary:
    cards = {c.id: c for c in session.scalars(select(Card).where(Card.project == project)).all()}
    if not cards:
        return Summary(project=project)

    rows: list[DecisionRow] = []
    metrics = Metrics()

    for decision in session.scalars(
        select(Decision).where(Decision.card_id.in_(cards)).order_by(Decision.id.desc())
    ).all():
        card = cards[decision.card_id]
        answer = session.get(Answer, decision.answer_id) if decision.answer_id else None
        question = session.get(Question, answer.question_id) if answer else None
        rows.append(
            DecisionRow(
                card_key=card.key,
                card_title=card.title,
                slug=decision.slug,
                question=question.prompt if question else decision.text,
                choice_label=_choice_label(question, answer),
                note=(answer.note if answer else "") or "",
                none_of_above=bool(answer and answer.none_of_above),
                decided_at=decision.created_at,
            )
        )

    metrics.decisions = len(rows)
    metrics.none_of_above = sum(1 for r in rows if r.none_of_above)

    for card in cards.values():
        reports = list(
            session.scalars(
                select(Report).where(Report.card_id == card.id).order_by(Report.id)
            ).all()
        )
        frozen = [r for r in reports if r.state == "frozen"]
        if not frozen:
            continue
        metrics.gated_cards += 1
        if any(r.state == "changes_requested" for r in reports):
            metrics.revised_cards += 1
        first = reports[0]
        last = frozen[-1]
        if last.frozen_at and first.created_at:
            metrics.elapsed_minutes.append((last.frozen_at - first.created_at).total_seconds() / 60)

    metrics.open_risks = len(
        list(
            session.scalars(
                select(Risk).where(Risk.card_id.in_(cards), Risk.resolved_at.is_(None))
            ).all()
        )
    )

    return Summary(project=project, rows=rows, metrics=metrics)


def as_markdown(summary: Summary) -> str:
    """ADR 用、面試素材用。刻意是純文字 —— 貼到哪裡都不會壞。"""
    m = summary.metrics
    lines = [
        f"# {summary.project} 的技術決策紀錄",
        "",
        f"- 過閘的卡：{m.gated_cards} 張",
        f"- 決策數：{m.decisions}",
    ]
    if m.revise_rate is not None:
        lines.append(f"- 要求修改比例：{m.revise_rate:.0%}（{m.revise_verdict}）")
    if m.none_of_above_rate is not None:
        lines.append(
            f"- 「以上皆非」比例：{m.none_of_above_rate:.0%} "
            "（偏高代表選項沒涵蓋到，是報告品質的訊號）"
        )
    if m.median_elapsed_minutes is not None:
        lines.append(
            f"- 產出→凍結的中位時間：{m.median_elapsed_minutes:.0f} 分鐘"
            "（牆上時鐘，含沒在看的時間）"
        )
    lines.append(f"- 未解的風險：{m.open_risks} 條")
    lines += ["", "---", ""]

    current = None
    for row in summary.rows:
        if row.card_key != current:
            current = row.card_key
            lines += [f"## {row.card_key} — {row.card_title}", ""]
        lines.append(f"### {row.slug}")
        lines.append("")
        lines.append(f"**問題**：{row.question}")
        lines.append("")
        marker = "⚠️ 以上皆非" if row.none_of_above else "決定"
        lines.append(f"**{marker}**：{row.choice_label}")
        if row.note:
            lines += ["", f"**備註**：{row.note}"]
        lines += ["", f"_{row.decided_at.astimezone():%Y-%m-%d %H:%M}_", ""]

    if not summary.rows:
        lines.append("_還沒有任何已凍結的決策。_")
    return "\n".join(lines)
