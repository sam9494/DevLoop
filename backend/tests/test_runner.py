import json

from devloop.runner.claude import ClaudeCliRunner, extract_json_object

RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "sess-1",
    "total_cost_usd": 0.0921985,
    "num_turns": 2,
    "permission_denials": [],
    "result": '{"sections": [], "questions": []}',
}


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def test_parses_the_final_result_event() -> None:
    out = ClaudeCliRunner._parse(
        _stream({"type": "system", "subtype": "init"}, {"type": "assistant"}, RESULT), "", 0
    )
    assert out.ok
    assert out.session_id == "sess-1"
    assert out.cost_usd == 0.0921985
    assert out.num_turns == 2


def test_non_json_lines_are_ignored() -> None:
    stdout = "起始雜訊\n" + _stream(RESULT)
    assert ClaudeCliRunner._parse(stdout, "", 0).ok


def test_missing_result_event_is_a_failure_with_the_stderr() -> None:
    out = ClaudeCliRunner._parse("", "boom", 1)
    assert not out.ok
    assert "boom" in (out.error or "")


def test_permission_denials_are_carried_through() -> None:
    denied = {**RESULT, "permission_denials": [{"tool_name": "Write"}]}
    out = ClaudeCliRunner._parse(_stream(denied), "", 0)
    assert out.permission_denials[0]["tool_name"] == "Write"


def test_error_result_is_not_ok() -> None:
    out = ClaudeCliRunner._parse(_stream({**RESULT, "is_error": True, "subtype": "error"}), "", 0)
    assert not out.ok


def test_stream_json_requires_verbose_flag() -> None:
    # CLI 實測：--output-format stream-json 少了 --verbose 會直接被拒絕
    assert "--verbose" in ClaudeCliRunner()._command("x", "plan")


def test_json_is_extracted_from_surrounding_prose() -> None:
    assert extract_json_object('這是報告：\n```json\n{"a": 1}\n```\n以上') == {"a": 1}
