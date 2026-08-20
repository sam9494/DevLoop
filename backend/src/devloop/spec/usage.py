"""用量。

這裡只放**真的量得到的東西**：跑過幾次、花了多少時間、Claude 自己回報的
訂閱視窗狀態。

刻意不做「每日美金上限」——`total_cost_usd` 是 API 費率換算，走訂閱時不是帳單；
拿它設每日門檻等於用代理指標的代理指標，而且週期還對不上（日 vs 五小時視窗）。
真正的煞車就是視窗狀態本身：不是 allowed 就不排新工作。
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
        label = "額度正常" if self.ok else f"額度用完（{self.status}）"
        return f"訂閱用量：{label}{when}"


@dataclass(frozen=True)
class Usage:
    runs_today: int
    minutes_today: float
    rate_limit: RateLimit | None = None

    @property
    def blocked(self) -> bool:
        """只有 Claude 明講額度不行時才擋 —— 沒跑過就不知道，不知道就不擋。"""
        return self.rate_limit is not None and not self.rate_limit.ok

    @property
    def summary(self) -> str:
        if not self.runs_today:
            return "今天還沒跑過"
        return f"今天跑了 {self.runs_today} 次，共 {self.minutes_today:.0f} 分鐘"


def _start_of_today() -> datetime:
    now = datetime.now(UTC).astimezone()
    return (now - timedelta(hours=now.hour, minutes=now.minute, seconds=now.second)).replace(
        microsecond=0
    )


def latest_rate_limit(session: Session) -> RateLimit | None:
    row = session.scalars(
        select(Job).where(Job.rate_limit_status.isnot(None)).order_by(Job.queued_at.desc()).limit(1)
    ).first()
    if row is None or row.rate_limit_status is None:
        return None
    return RateLimit(status=row.rate_limit_status, resets_at=row.rate_limit_resets_at)


def usage_today(session: Session) -> Usage:
    since = _start_of_today()
    runs = (
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.queued_at >= since, Job.started_at.isnot(None))
        )
        or 0
    )
    seconds = (
        session.scalar(
            select(
                func.coalesce(func.sum(func.extract("epoch", Job.finished_at - Job.started_at)), 0)
            ).where(Job.queued_at >= since, Job.finished_at.isnot(None))
        )
        or 0
    )
    return Usage(
        runs_today=int(runs),
        minutes_today=float(seconds) / 60,
        rate_limit=latest_rate_limit(session),
    )
