"""worker 的責任：撿到工作、跑完、不被單一 job 弄死。"""

from pathlib import Path

from sqlalchemy.orm import Session

from devloop.db.models import Card, Job
from devloop.runner.worker import Worker, claim_next_job, reap_orphaned_jobs
from devloop.spec import service
from tests.conftest import needs_db


def _queued(session, prompt: str = "x") -> Job:  # type: ignore[no-untyped-def]
    card = Card(key="KAN-99", project="KAN", title="t")
    session.add(card)
    session.flush()
    return service.enqueue_generation(session, card, workspace=Path("/tmp"), permission_mode="plan")


@needs_db
def test_claim_marks_the_job_running_so_nobody_else_takes_it(session) -> None:  # type: ignore[no-untyped-def]
    _queued(session)
    session.flush()
    job = claim_next_job(session)

    assert job is not None and job.status == "running"
    assert claim_next_job(session) is None


@needs_db
def test_tick_reports_when_there_is_nothing_to_do(session_factory) -> None:  # type: ignore[no-untyped-def]
    worker = Worker(session_factory, lambda s, j: None)
    assert worker.tick() is False


@needs_db
def test_tick_runs_the_handler(session, session_factory) -> None:  # type: ignore[no-untyped-def]
    _queued(session)
    session.commit()
    seen: list[str] = []

    def handler(s, j) -> None:  # type: ignore[no-untyped-def]
        seen.append(j.kind)
        j.status = "succeeded"

    assert Worker(session_factory, handler).tick() is True
    assert seen == ["generate_spec"]


@needs_db
def test_an_exploding_job_is_marked_failed_and_does_not_kill_the_worker(  # type: ignore[no-untyped-def]
    session, session_factory
) -> None:
    job_id = _queued(session).id
    session.commit()

    def boom(s, j) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("炸了")

    worker = Worker(session_factory, boom)
    assert worker.tick() is True

    job = session.get(Job, job_id)
    assert job is not None
    session.refresh(job)
    assert job.status == "failed"
    assert "炸了" in (job.error or "")
    assert worker.tick() is False  # 還活著，只是沒工作了


@needs_db
def test_the_claim_is_committed_before_the_work_starts(session, session_factory) -> None:  # type: ignore[no-untyped-def]
    """真的跑起來才發現的 bug：認領後沒 commit，狀態改成 running 只有自己看得到。

    `claude -p` 一跑就是好幾分鐘，那段期間卡片列表顯示的是「尚未產生規格」，
    而且那一列的鎖被抓著不放。認領完必須立刻 commit，狀態才對外可見。

    共用連線的測試環境驗不出跨連線可見性，所以直接驗機制：
    handler 被呼叫的時候，前面一定已經 commit 過了。
    """
    _queued(session)
    session.commit()

    events: list[str] = []

    def spying_factory() -> Session:
        real = session_factory()
        original_commit = real.commit

        def commit() -> None:
            events.append("commit")
            original_commit()

        real.commit = commit  # type: ignore[method-assign]
        return real

    def handler(s, j) -> None:  # type: ignore[no-untyped-def]
        events.append("handler:" + j.status)
        j.status = "succeeded"

    Worker(spying_factory, handler).tick()

    assert events[0] == "commit"  # 認領之後立刻 commit
    assert events[1] == "handler:running"  # 工作開始時狀態已經是 running
    assert events[-1] == "commit"  # 做完再 commit 一次


@needs_db
def test_a_job_left_running_by_a_dead_process_goes_back_to_the_queue(session) -> None:  # type: ignore[no-untyped-def]
    """只有一個 worker，所以啟動當下還掛著 running 的一定是上個行程死掉留下的。"""
    job = _queued(session)
    job.status = "running"
    session.flush()

    assert reap_orphaned_jobs(session) == 1

    assert job.status == "queued"
    assert job.started_at is None
    assert "沒跑完就結束" in (job.error or "")


@needs_db
def test_reaping_leaves_finished_jobs_alone(session) -> None:  # type: ignore[no-untyped-def]
    job = _queued(session)
    job.status = "succeeded"
    session.flush()

    assert reap_orphaned_jobs(session) == 0
    assert job.status == "succeeded"
