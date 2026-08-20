"""用量煞車。

**`total_cost_usd` 不是帳單。** 走本機 Claude Code 時吃的是訂閱，那個數字是
「同樣的 token 換算成 API 費率會是多少」。它有用，因為跟 token 消耗成正比，
可以當用量的代理指標 —— 但它不是錢。

真正會擋住人的是訂閱的用量視窗（`rate_limit_info`，目前是五小時一輪）。
用完不只 DevLoop 停擺，**你自己開的 Claude Code 互動 session 也會一起被鎖**。
所以這裡的上限是一道自律閘：避免背景工作把整個視窗吃光。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from devloop.db.models import Job


@dataclass(frozen=True)
class RateLimit:
    """訂閱的用量視窗 —— 從 claude 的 rate_limit_event 撈到的真實狀態。"""

    status: str
    resets_at: datetime | None

    @property
    def ok(self) -> bool:
        return self.status == "allowed"

    @property
    def summary(self) -> str:
        when = f"，{self.resets_at.astimezone():%H:%M} 重置" if self.resets_at else ""
        label = "額度正常" if self.ok else f"額度狀態 {self.status}"
        return f"訂閱用量：{label}{when}"


@dataclass(frozen=True)
class Budget:
    spent_today_usd: float
    limit_usd: float
    rate_limit: RateLimit | None = None

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_today_usd)

    @property
    def exhausted(self) -> bool:
        return self.limit_usd > 0 and self.spent_today_usd >= self.limit_usd

    @property
    def summary(self) -> str:
        return (
            f"今日用量 US${self.spent_today_usd:.2f} / 自訂上限 US${self.limit_usd:.2f}"
            "（API 費率換算，訂閱不收這筆）"
        )


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


def latest_rate_limit(session: Session) -> RateLimit | None:
    """最近一次真的跑過的工作回報的訂閱狀態。"""
    row = session.scalars(
        select(Job).where(Job.rate_limit_status.isnot(None)).order_by(Job.queued_at.desc()).limit(1)
    ).first()
    if row is None or row.rate_limit_status is None:
        return None
    return RateLimit(status=row.rate_limit_status, resets_at=row.rate_limit_resets_at)


def budget_for(session: Session, limit_usd: float) -> Budget:
    return Budget(
        spent_today_usd=spent_today(session),
        limit_usd=float(limit_usd),
        rate_limit=latest_rate_limit(session),
    )
