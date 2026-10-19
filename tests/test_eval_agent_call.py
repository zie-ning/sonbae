import json
from pathlib import Path

from eval.agent_call_report import RunStats, build_report
from eval.agent_call_run import (
    CallTask,
    RunSpec,
    allowed_tools,
    build_command,
    estimate_rerank_tokens,
    load_call_tasks,
    mcp_config,
    parse_stream,
    plan_runs,
    read_env_file,
)


def _task(tid: str = "c01", env: str = "e2", expect: str = "required") -> CallTask:
    return CallTask(id=tid, env=env, expect_call=expect, prompt="재고 차감 넣어줘")


def _lines(*events: dict) -> list[str]:
    return [json.dumps(e, ensure_ascii=False) for e in events]


def _tool_use(tid: str, name: str, **inputs) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inputs}


def _result(tid: str, text: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}]}


STREAM = _lines(
    {
        "type": "system",
        "subtype": "init",
        "model": "claude-sonnet-5-5",
        "mcp_servers": [
            {"name": "sonbae", "status": "connected"},
            {"name": "wiki", "status": "connected"},
        ],
    },
    {
        "type": "assistant",
        "message": {
            "content": [
                _tool_use("a", "Read", file_path="shop/orders.py"),
                _tool_use("b", "mcp__wiki__search_pages", query="재고"),
                _tool_use("c", "ToolSearch", query="select:mcp__sonbae__search"),
                _tool_use("d", "mcp__sonbae__search", query="재고 차감 동시성"),
                _tool_use("e", "Edit", file_path="shop/orders.py"),
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [_result("b", "검색 결과가 없습니다."), _result("d", "결과: 관련도 순 3건")]
        },
    },
    {"type": "result", "subtype": "success", "result": "끝", "num_turns": 4, "total_cost_usd": 0.2},
)


def test_load_call_tasks_reads_both_envs():
    tasks = load_call_tasks()
    by_env = {env: [t for t in tasks if t.env == env] for env in ("e1", "e2")}
    assert len(by_env["e1"]) == 16 and len(by_env["e2"]) == 10
    # E1은 M7 과제의 기대 호출을 그대로 쓴다
    assert {t.id: t.expect_call for t in by_env["e1"]}["t25"] == "forbidden"
    assert {t.expect_call for t in by_env["e2"]} == {"required", "forbidden"}


def test_plan_and_estimate_skip_forbidden_tasks():
    specs = plan_runs([_task(), _task("n01", expect="forbidden")], ["before", "after"], 3)
    assert len(specs) == 12
    assert specs[0].key == "c01__before__r1"
    # 부르면 안 됨 과제는 검색 토큰 예상에서 뺀다
    assert estimate_rerank_tokens(specs) == 6 * 3 * 21_700


def test_code_tools_only_in_e2():
    assert "Edit" in allowed_tools("e2") and "Edit" not in allowed_tools("e1")
    assert "mcp__sonbae" in allowed_tools("e1") and "mcp__wiki" in allowed_tools("e1")


def test_build_command_uses_strict_config(tmp_path: Path):
    command = build_command("claude", RunSpec(_task(env="e1"), "after", 1), "sonnet", tmp_path)
    assert "--strict-mcp-config" in command and "dontAsk" in command
    assert "Edit" not in command[command.index("--allowedTools") + 1]


def test_mcp_config_pins_db_and_adds_decoys(tmp_path: Path):
    servers = mcp_config(tmp_path / "sonbae.exe", tmp_path / "db.sqlite")["mcpServers"]
    assert servers["sonbae"]["env"] == {"SONBAE_DB": str(tmp_path / "db.sqlite")}
    assert {"wiki", "tracker", "devdocs"} <= servers.keys()
    # 비밀 값은 설정 파일에 쓰지 않는다
    assert "VOYAGE_API_KEY" not in json.dumps(servers)


def test_read_env_file(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text('# 주석\nVOYAGE_API_KEY="abc"\nOTHER=1\n', encoding="utf-8")
    assert read_env_file(path) == {"VOYAGE_API_KEY": "abc", "OTHER": "1"}
    assert read_env_file(None) == {}


def test_parse_stream_and_run_stats():
    record = parse_stream(STREAM, "c01__after__r2")
    assert (record.task_id, record.version, record.rep) == ("c01", "after", 2)
    assert record.mcp_status["sonbae"] == "connected"
    stats = RunStats(record)
    assert stats.called and not stats.late  # Edit보다 먼저 불렀다
    assert stats.tools_before == 2  # Read, 미끼 (ToolSearch 제외)
    assert stats.decoy_calls == 1 and stats.searches == 1 and stats.fallbacks == 0
    assert stats.loaded_by == ["select:mcp__sonbae__search"]


def test_late_call_when_edited_first():
    lines = _lines(
        {
            "type": "assistant",
            "message": {
                "content": [
                    _tool_use("a", "Edit"),
                    _tool_use("b", "mcp__sonbae__search", query="x"),
                ]
            },
        },
        {"type": "result", "subtype": "success", "result": ""},
    )
    assert RunStats(parse_stream(lines, "c01__after__r1")).late


def test_build_report_counts_calls():
    tasks = {"c01": _task(), "n01": _task("n01", expect="forbidden")}
    called = parse_stream(STREAM, "c01__after__r1")
    not_called = parse_stream(
        _lines({"type": "result", "subtype": "success", "result": ""}), "n01__after__r1"
    )
    report = build_report([called, not_called], tasks, "test")
    assert "| e2 | 불러야 함 | 1/1 (100%) |" in report
    assert "| e2 | 부르면 안 됨 | 0/1 (0%) |" in report
