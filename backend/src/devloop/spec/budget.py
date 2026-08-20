"""成本煞車。

一次規格產生實測 US$1.68。沒有上限的話，跑一輪 JobRadar Phase 0 就是三十幾塊，
而且是在無人看管的背景執行 —— 這種東西一定要有煞車。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from devloop.db.models import Job


@dataclass(frozen=True)
class Budget:
    spent_today_usd: float
    limit_usd: float

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_today_usd)

    @property
    def exhausted(self) -> bool:
        return self.limit_usd > 0 and self.spent_today_usd >= self.limit_usd

    @property
    def summary(self) -> str:
        return f"今日已用 US${self.spent_today_usd:.2f} / 上限 US${self.limit_usd:.2f}"


def _start_of_today() -> datetime:
    now = datetime.now(UTC).astimezone()
    return (now - timedelta(hours=now.hour, minutes=now.minute, seconds=now.second)).replace(
        microsecond=0
    )


def spent_today(session: Session) -> float:
    """已經花掉的錢 —— 只算真的跑過的（成本欄位有值的）。"""
    total = session.scalar(
        select(func.coalesce(func.sum(Job.cost_usd), 0)).where(Job.queued_at >= _start_of_today())
    )
    return float(total or 0)


def budget_for(session: Session, limit_usd: float) -> Budget:
    return Budget(spent_today_usd=spent_today(session), limit_usd=float(limit_usd))
