import pytest

from eval.agent_format_eval import build_pairs, case_data, citations
from eval.agent_format_run import RunSpec, estimate_rerank_tokens, select_tasks
from eval.agent_run import RunRecord, ToolCall
from eval.agent_tasks import load_tasks

HOSTS = {"toss.tech", "techblog.woowahan.com"}
CARD_A = "[case_0001] 토스 · 2025-01-01 · 동시성·락 / 커머스\n제목: A\n원문: https://toss.tech/a"
CARD_B = "[case_0002] 토스 · 2025-01-01 · 동시성·락 / 커머스\n제목: B\n원문: https://toss.tech/b"


def _record(answer: str, opened: list[str], version: str = "after", rep: int = 1) -> RunRecord:
    calls = [ToolCall(id="1", name="search", input={"query": "q"}, result=f"{CARD_A}\n\n{CARD_B}")]
    if opened:
        calls.append(ToolCall(id="2", name="get_details", input={"ids": opened}, result="..."))
    return RunRecord(
        key=f"t01__{version}__r{rep}",
        task_id="t01",
        condition=version,
        rep=rep,
        tool_calls=calls,
        answer=answer,
        result_subtype="success",
    )


def test_citations_counts_unopened_and_unknown_links():
    answer = (
        "토스([링크](https://toss.tech/a)), 토스 https://toss.tech/b/ 참고."
        " 우아한형제들 https://techblog.woowahan.com/999 와 https://example.com/x"
    )
    c = citations(_record(answer, ["case_0001"]), HOSTS)
    assert c.cited == {"https://toss.tech/a", "https://toss.tech/b"}
    assert c.unopened == {"https://toss.tech/b"}  # 열지 않은 글
    assert c.unknown == {"https://techblog.woowahan.com/999"}  # 결과에 없는 블로그 글만 센다


def test_case_data_includes_evidence():
    entry = {
        "id": "case_0001",
        "company": "토스",
        "post_title": "A",
        "problem_situation": [],
        "solution": [{"text": "요약", "evidence": "원문 문장"}],
        "performance_ops": [],
        "rejected_alternatives": [],
    }
    text = case_data("https://toss.tech/a", [entry])
    assert "요약" in text and '발췌: "원문 문장"' in text


def test_pairs_match_same_task_and_rep():
    tasks = [t for t in load_tasks() if t.id == "t01"]
    records = [
        _record("a", [], "after", 1),
        _record("b", [], "before", 1),
        _record("c", [], "after", 2),
    ]
    pairs = build_pairs(tasks, records)
    assert [p.key for p in pairs] == ["t01__after-vs-before__r1"]


def test_select_tasks_skips_categories_without_search():
    tasks = select_tasks(load_tasks())
    assert {t.category for t in tasks} == {"design", "open", "absent", "noise", "few"}
    with pytest.raises(ValueError):
        select_tasks(load_tasks(), ["t25"])  # 호출 금지 과제


def test_estimate_uses_category_search_counts():
    task = next(t for t in load_tasks() if t.id == "t21")  # absent
    assert estimate_rerank_tokens([RunSpec(task, "after", 1)]) == round(3.58 * 21_700)
