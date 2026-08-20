"""呼叫本機的 Claude Code。

刻意用 subprocess 而不是 Agent SDK —— 這樣吃的是這台機器上的登入狀態，
不需要另外的 API 金鑰（TECH-002 / llm-runtime）。

作用範圍的唯一防線是 spawn 時的 cwd：CLI 沒有 --cwd 旗標，但實測確認
子行程寫不到 cwd 以外的地方，被擋的動作會出現在 permission_denials 裡。

用 Popen 而不是 subprocess.run，是為了讓執行中的工作可以被中止 —— 一次
呼叫要跑好幾分鐘、花掉 US$1.68，沒有煞車不行。
"""

import json
import os
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import structlog

log = structlog.get_logger()


@dataclass
class RunResult:
    ok: bool
    result_text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    permission_denials: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    transcript: str = ""
    cancelled: bool = False


class LlmRunner(Protocol):
    def run(
        self,
        prompt: str,
        cwd: Path,
        permission_mode: str,
        timeout_s: int,
        job_id: uuid.UUID | None = None,
    ) -> RunResult: ...


def extract_json_object(text: str) -> dict[str, Any]:
    """模型可能把 JSON 包在說明文字或 ``` 圍欄裡，取最外層的大括號區段。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("回應裡找不到 JSON 物件")
    parsed: dict[str, Any] = json.loads(text[start : end + 1])
    return parsed


def _terminate_tree(proc: "subprocess.Popen[str]", grace_s: float = 5.0) -> None:
    """殺掉整個行程群組，不只是直屬子行程。

    `claude` 底下還會開別的行程。只 kill 直屬的那個，孫行程會繼續握著 stdout/stderr
    的 pipe，`communicate()` 就永遠等不到 EOF —— 逾時等於沒有生效。所以子行程用
    `start_new_session=True` 自成一個群組，這裡整組一起送訊號。
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return

    # 安全閥：子行程若不在自己的群組裡，killpg 會把「我們自己」也殺掉。
    # 開發時真的踩過 —— pytest 整個被 SIGTERM 掉。這種情況只殺那一個行程。
    if pgid == os.getpgid(0):
        log.warning("runner.terminate_single", reason="子行程與服務同一個行程群組")
        proc.terminate()
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            proc.kill()
        return

    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


class ProcessRegistry:
    """執行中的子行程。

    worker 是同一個行程裡的 thread，所以 HTTP 請求可以直接在這裡找到它要殺的行程。
    真的要跨行程時再換成寫 PID 進 jobs 表。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: dict[uuid.UUID, subprocess.Popen[str]] = {}
        self._cancelled: set[uuid.UUID] = set()

    def register(self, job_id: uuid.UUID, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._live[job_id] = proc

    def release(self, job_id: uuid.UUID) -> None:
        with self._lock:
            self._live.pop(job_id, None)

    def was_cancelled(self, job_id: uuid.UUID) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def forget_cancel(self, job_id: uuid.UUID) -> None:
        with self._lock:
            self._cancelled.discard(job_id)

    def cancel(self, job_id: uuid.UUID) -> bool:
        """終止子行程。回傳有沒有真的殺到東西。"""
        with self._lock:
            proc = self._live.get(job_id)
            self._cancelled.add(job_id)
        if proc is None:
            return False
        _terminate_tree(proc)
        log.warning("runner.cancelled", job_id=str(job_id))
        return True


registry = ProcessRegistry()


class ClaudeCliRunner:
    def __init__(self, binary: str = "claude", processes: ProcessRegistry | None = None) -> None:
        self._binary = binary
        self._registry = processes or registry

    def _command(self, prompt: str, permission_mode: str) -> list[str]:
        return [
            self._binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",  # stream-json 沒有 --verbose 會直接被 CLI 拒絕
            "--permission-mode",
            permission_mode,
        ]

    def run(
        self,
        prompt: str,
        cwd: Path,
        permission_mode: str,
        timeout_s: int,
        job_id: uuid.UUID | None = None,
    ) -> RunResult:
        try:
            proc = subprocess.Popen(
                self._command(prompt, permission_mode),
                cwd=cwd,  # ← 作用範圍的防線就是這一行
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # 自成行程群組，才殺得乾淨
            )
        except FileNotFoundError:
            return RunResult(
                ok=False, error=f"找不到 {self._binary} —— Claude Code 沒安裝或不在 PATH"
            )

        if job_id is not None:
            self._registry.register(job_id, proc)
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _terminate_tree(proc)
            return RunResult(ok=False, error=f"超過 {timeout_s} 秒沒跑完，已中止")
        finally:
            if job_id is not None:
                self._registry.release(job_id)

        if job_id is not None and self._registry.was_cancelled(job_id):
            self._registry.forget_cancel(job_id)
            return RunResult(ok=False, cancelled=True, error="你按了中止")

        return self._parse(stdout, stderr, proc.returncode)

    @staticmethod
    def _parse(stdout: str, stderr: str, returncode: int) -> RunResult:
        events = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        final = next((e for e in reversed(events) if e.get("type") == "result"), None)
        transcript = stdout

        if final is None:
            detail = (stderr or stdout or "").strip()[:500]
            return RunResult(
                ok=False,
                error=f"Claude Code 沒有回傳結果（離開碼 {returncode}）：{detail}",
                transcript=transcript,
            )

        return RunResult(
            ok=not final.get("is_error", False) and final.get("subtype") == "success",
            result_text=final.get("result") or "",
            session_id=final.get("session_id"),
            cost_usd=final.get("total_cost_usd"),
            num_turns=final.get("num_turns"),
            permission_denials=list(final.get("permission_denials") or []),
            error=None if final.get("subtype") == "success" else str(final.get("subtype")),
            transcript=transcript,
        )


class FakeRunner:
    """測試用。想模擬失敗就給 result=None 加 error。"""

    def __init__(self, result: str | None = None, error: str | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, Path, str]] = []

    def run(
        self,
        prompt: str,
        cwd: Path,
        permission_mode: str,
        timeout_s: int,
        job_id: uuid.UUID | None = None,
    ) -> RunResult:
        self.calls.append((prompt, cwd, permission_mode))
        if self._error:
            return RunResult(ok=False, error=self._error)
        return RunResult(
            ok=True,
            result_text=self._result or "",
            session_id="fake-session",
            cost_usd=0.09,
            num_turns=2,
        )
