"""用量 —— 只放真的量得到的東西。

刻意沒有「每日美金上限」：total_cost_usd 是 API 費率換算，走訂閱時不是帳單，
拿它設每日門檻等於用代理指標的代理指標，週期還對不上（日 vs 五小時視窗）。
真正的煞車是 Claude 自己回報的視窗狀態。
"""

import uuid
from datetime import UTC, datetime, timedelta

from devloop.db.models import Card, Job
from devloop.spec import usage as usage_service
from tests.conftest import needs_db


def _job(session, *, status="succeeded", started=None, finished=None, limit=None, when=None):  # type: ignore[no-untyped-def]
    card = Card(key=f"DEV-{uuid.uuid4().hex[:6]}", project="DEV", title="t")
    session.add(card)
    session.flush()
    job = Job(
        card_id=card.id,
        kind="generate_spec",
        status=status,
        prompt="p",
        cwd="/tmp",
        permission_mode="plan",
        started_at=started,
        finished_at=finished,
    )
    if when is not None:
        job.queued_at = when
    if limit is not None:
        job.rate_limit_status = limit[0]
        job.rate_limit_resets_at = limit[1]
    session.add(job)
    session.flush()
    return job


@needs_db
def test_a_quiet_day(session) -> None:  # type: ignore[no-untyped-def]
    u = usage_service.usage_today(session)
    assert u.runs_today == 0
    assert u.summary == "今天還沒跑過"
    assert not u.blocked  # 不知道就不擋


@needs_db
def test_runs_and_minutes_are_counted(session) -> None:  # type: ignore[no-untyped-def]
    start = datetime.now(UTC) - timedelta(minutes=7)
    _job(session, started=start, finished=start + timedelta(minutes=7))
    _job(session, started=start, finished=start + timedelta(minutes=3))

    u = usage_service.usage_today(session)
    assert u.runs_today == 2
    assert u.summary == "今天跑了 2 次，共 10 分鐘"


@needs_db
def test_a_job_that_never_started_is_not_a_run(session) -> None:  # type: ignore[no-untyped-def]
    _job(session, status="queued")
    assert usage_service.usage_today(session).runs_today == 0


@needs_db
def test_yesterday_does_not_count(session) -> None:  # type: ignore[no-untyped-def]
    yesterday = datetime.now(UTC) - timedelta(days=1)
    _job(session, started=yesterday, finished=yesterday + timedelta(minutes=5), when=yesterday)
    assert usage_service.usage_today(session).runs_today == 0


@needs_db
def test_an_exhausted_window_blocks_new_work(session) -> None:  # type: ignore[no-untyped-def]
    resets = datetime.now(UTC) + timedelta(hours=2)
    _job(session, limit=("rejected", resets))

    u = usage_service.usage_today(session)
    assert u.blocked
    assert u.rate_limit is not None
    assert "額度用完（rejected）" in u.rate_limit.summary
    assert "重置" in u.rate_limit.summary


@needs_db
def test_an_allowed_window_does_not_block(session) -> None:  # type: ignore[no-untyped-def]
    _job(session, limit=("allowed", datetime.now(UTC) + timedelta(hours=3)))

    u = usage_service.usage_today(session)
    assert not u.blocked
    assert u.rate_limit is not None and u.rate_limit.ok


@needs_db
def test_the_newest_report_wins(session) -> None:  # type: ignore[no-untyped-def]
    old = datetime.now(UTC) - timedelta(hours=6)
    _job(session, limit=("rejected", old), when=old)
    _job(session, limit=("allowed", datetime.now(UTC) + timedelta(hours=1)))

    assert not usage_service.usage_today(session).blocked


@needs_db
def test_there_is_no_money_limit_anywhere(session) -> None:  # type: ignore[no-untyped-def]
    """回歸測試：曾經有個「每日美金上限」，量的是換算值不是帳單，已經拿掉。"""
    from devloop.db.models import Connection

    assert not hasattr(Connection, "daily_cost_limit_usd")
    assert not hasattr(usage_service.usage_today(session), "limit_usd")
