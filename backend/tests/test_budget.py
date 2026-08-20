"""成本煞車：上限、中止、逾時。"""

import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from devloop.db.models import Card, Job
from devloop.runner.claude import ClaudeCliRunner, ProcessRegistry
from devloop.spec import budget as budget_service
from tests.conftest import needs_db


def _job(session, cost: float | None, when=None, status="succeeded") -> Job:  # type: ignore[no-untyped-def]
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
        cost_usd=cost,
    )
    if when is not None:
        job.queued_at = when
    session.add(job)
    session.flush()
    return job


# ---------- 上限 ----------


@needs_db
def test_nothing_spent_yet(session) -> None:  # type: ignore[no-untyped-def]
    b = budget_service.budget_for(session, 10)
    assert b.spent_today_usd == 0
    assert not b.exhausted
    assert b.remaining_usd == 10


@needs_db
def test_todays_costs_add_up(session) -> None:  # type: ignore[no-untyped-def]
    _job(session, 1.68)
    _job(session, 0.09)

    b = budget_service.budget_for(session, 10)
    assert round(b.spent_today_usd, 2) == 1.77
    assert b.summary == "今日已用 US$1.77 / 上限 US$10.00"


@needs_db
def test_yesterdays_spending_does_not_count(session) -> None:  # type: ignore[no-untyped-def]
    _job(session, 5.00, when=datetime.now(UTC) - timedelta(days=1))
    _job(session, 1.00)

    assert budget_service.budget_for(session, 10).spent_today_usd == 1.00


@needs_db
def test_reaching_the_limit_exhausts_the_budget(session) -> None:  # type: ignore[no-untyped-def]
    _job(session, 10.00)

    b = budget_service.budget_for(session, 10)
    assert b.exhausted
    assert b.remaining_usd == 0


@needs_db
def test_a_job_that_never_ran_costs_nothing(session) -> None:  # type: ignore[no-untyped-def]
    _job(session, None, status="queued")
    assert budget_service.budget_for(session, 10).spent_today_usd == 0


@needs_db
def test_a_zero_limit_never_blocks(session) -> None:  # type: ignore[no-untyped-def]
    """上限設 0 = 不限制，不是「一毛都不能花」。"""
    _job(session, 99.0)
    assert not budget_service.budget_for(session, 0).exhausted


# ---------- 中止 ----------


def test_cancelling_actually_kills_the_child_process() -> None:
    """只改狀態不算中止 —— 一次呼叫跑七分鐘、花 US$1.68，行程要真的死掉。"""
    processes = ProcessRegistry()
    job_id = uuid.uuid4()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        text=True,
        start_new_session=True,  # runner 就是這樣開的
    )
    processes.register(job_id, proc)

    assert processes.cancel(job_id) is True

    deadline = time.time() + 5
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None  # 真的死了
    assert processes.was_cancelled(job_id)


def test_cancelling_something_that_is_not_running_says_so() -> None:
    assert ProcessRegistry().cancel(uuid.uuid4()) is False


def test_a_child_sharing_our_process_group_is_killed_alone() -> None:
    """安全閥：沒有獨立群組時不能 killpg，否則連服務自己都會被殺掉。"""
    processes = ProcessRegistry()
    job_id = uuid.uuid4()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], text=True
    )  # 故意不給 start_new_session
    processes.register(job_id, proc)

    assert processes.cancel(job_id) is True

    deadline = time.time() + 5
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None
    # 還活著就是安全閥有效（這行跑得到就代表沒被自己殺掉）


def test_a_cancelled_run_is_reported_as_cancelled_not_failed(tmp_path: Path) -> None:
    processes = ProcessRegistry()
    job_id = uuid.uuid4()
    processes.cancel(job_id)  # 先標記，模擬使用者在執行期間按下中止

    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n")
    fake_claude.chmod(0o755)

    out = ClaudeCliRunner(str(fake_claude), processes).run("p", tmp_path, "plan", 10, job_id=job_id)

    assert out.cancelled
    assert not out.ok
    assert out.error == "你按了中止"


# ---------- 逾時 ----------


def test_a_run_that_overruns_is_killed_and_reported(tmp_path: Path) -> None:
    slow = tmp_path / "claude"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)

    started = time.time()
    out = ClaudeCliRunner(str(slow)).run("p", tmp_path, "plan", timeout_s=1)

    assert not out.ok
    assert "超過 1 秒" in (out.error or "")
    assert time.time() - started < 10  # 真的被砍掉，不是等它跑完


def test_a_missing_binary_is_a_clear_message(tmp_path: Path) -> None:
    out = ClaudeCliRunner("/does/not/exist").run("p", tmp_path, "plan", 5)
    assert "沒安裝或不在 PATH" in (out.error or "")
