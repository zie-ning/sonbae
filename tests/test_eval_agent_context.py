import json

from eval.agent_context_report import build_report, parse_context


def _assistant(mid: str, context: int, *tools: tuple[str, str, dict]) -> dict:
    usage = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": context - 2,
    }
    content = [
        {"type": "tool_use", "id": tid, "name": name, "input": inp} for tid, name, inp in tools
    ]
    return {"type": "assistant", "message": {"id": mid, "usage": usage, "content": content}}


def _result(tid: str, text: str) -> dict:
    block = {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}]}
    return {"type": "user", "message": {"content": [block]}}


def _lines(*events: dict) -> list[str]:
    return [json.dumps(e, ensure_ascii=False) for e in events]


STREAM = _lines(
    _assistant("m1", 20_000, ("a", "Read", {})),
    _result("a", "코드"),
    _assistant("m2", 21_000, ("b", "ToolSearch", {"query": "select:mcp__sonbae__search"})),
    _result("b", ""),
    _assistant("m3", 25_000, ("c", "mcp__sonbae__search", {}), ("d", "mcp__sonbae__search", {})),
    _result("c", "결과" * 10),
    _result("d", "결과" * 5),
    _assistant("m4", 31_000, ("e", "mcp__sonbae__get_details", {})),
    _result("e", "상세" * 20),
    _assistant("m5", 35_000),
)


def test_parse_context_attributes_growth_to_steps():
    run = parse_context(STREAM, "c01__after__r1")
    assert (run.first, run.last) == (20_000, 35_000)
    assert run.definition_tokens == [4_000]  # ToolSearch 단계: 21k → 25k
    assert run.sonbae_tokens == 6_000 + 4_000  # search 단계 + get_details 단계
    assert run.sonbae_chars == 30 + 40
    assert run.per_call("search") == [3_000]  # 병렬 2회로 6k
    assert run.per_call("get_details") == [4_000]
    assert run.called


def test_build_report_without_sizes():
    report = build_report([parse_context(STREAM, "c01__after__r1")], {"c01": "e2"}, {})
    assert "| E2 작업 도중 | after | 1 | 20,000 | 35,000 | 4,000 | 10,000 | 29% |" in report
