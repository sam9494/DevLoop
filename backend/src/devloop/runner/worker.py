"""背景工作佇列。

刻意不用 Celery / Redis —— 併發量是「一次一張卡」，一支 thread 加一張 jobs 表就夠。
償還觸發條件寫在 DEBT.md：同時 >3 個 job 或失敗率 >10%。
"""

import threading
import time
from collections.abc import Callable

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from devloop.db.models import Job

log = structlog.get_logger()


def claim_next_job(session: Session) -> Job | None:
    """SKIP LOCKED：多個 worker 同時撈也不會撿到同一個。"""
    job = session.scalar(
        select(Job)
        .where(Job.status == "queued")
        .order_by(Job.queued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = "running"
    session.flush()
    return job


class Worker:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        handler: Callable[[Session, Job], None],
        poll_seconds: float = 2.0,
    ) -> None:
        self._session_factory = session_factory
        self._handler = handler
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="devloop-worker", daemon=True)
        self._thread.start()
        log.info("worker.started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        log.info("worker.stopped")

    def tick(self) -> bool:
        """跑一輪。回傳有沒有處理到工作 —— 測試直接呼叫這個，不用等 thread。"""
        session = self._session_factory()
        try:
            job = claim_next_job(session)
            if job is None:
                return False
            try:
                self._handler(session, job)
            except Exception as exc:  # worker 不能被單一 job 弄死
                log.exception("worker.job_failed", job_id=str(job.id))
                job.status = "failed"
                job.error = f"worker 例外：{exc}"
            session.commit()
            return True
        finally:
            session.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.tick():
                    self._stop.wait(self._poll)
            except Exception:
                log.exception("worker.tick_failed")
                self._stop.wait(self._poll)
            else:
                time.sleep(0)
