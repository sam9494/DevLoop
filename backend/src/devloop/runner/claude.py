"""呼叫本機的 Claude Code。

刻意用 subprocess 而不是 Agent SDK —— 這樣吃的是這台機器上的登入狀態，
不需要另外的 API 金鑰（TECH-002 / llm-runtime）。

作用範圍的唯一防線是 spawn 時的 cwd：CLI 沒有 --cwd 旗標，但實測確認
子行程寫不到 cwd 以外的地方，被擋的動作會出現在 permission_denials 裡。
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


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


class LlmRunner(Protocol):
    def run(self, prompt: str, cwd: Path, permission_mode: str, timeout_s: int) -> RunResult: ...


def extract_json_object(text: str) -> dict[str, Any]:
    """模型可能把 JSON 包在說明文字或 ``` 圍欄裡，取最外層的大括號區段。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("回應裡找不到 JSON 物件")
    parsed: dict[str, Any] = json.loads(text[start : end + 1])
    return parsed


class ClaudeCliRunner:
    def __init__(self, binary: str = "claude") -> None:
        self._binary = binary

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

    def run(self, prompt: str, cwd: Path, permission_mode: str, timeout_s: int) -> RunResult:
        try:
            proc = subprocess.run(
                self._command(prompt, permission_mode),
                cwd=cwd,  # ← 作用範圍的防線就是這一行
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            return RunResult(
                ok=False, error=f"找不到 {self._binary} —— Claude Code 沒安裝或不在 PATH"
            )
        except subprocess.TimeoutExpired:
            return RunResult(ok=False, error=f"超過 {timeout_s} 秒沒跑完，已中止")

        return self._parse(proc.stdout, proc.stderr, proc.returncode)

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

    def run(self, prompt: str, cwd: Path, permission_mode: str, timeout_s: int) -> RunResult:
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
