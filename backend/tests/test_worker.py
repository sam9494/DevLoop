"""worker 的責任：撿到工作、跑完、不被單一 job 弄死。"""

from pathlib import Path

from devloop.db.models import Card, Job
from devloop.runner.worker import Worker, claim_next_job
from devloop.spec import service
from tests.conftest import needs_db


def _queued(session, prompt: str = "x") -> Job:  # type: ignore[no-untyped-def]
    card = Card(key="KAN-99", title="t")
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
