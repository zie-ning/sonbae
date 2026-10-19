"""호출률 평가 리포트 (이슈 #30): 환경·서버 버전별 호출 판단 지표를 정리한다.

    uv run python eval/agent_call_report.py   # → eval/reports/issue30-call-rate.md

답변 품질은 채점하지 않고 실행 기록의 도구 호출만 센다. 실행은 eval/agent_call_run.py.
"""

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 스크립트로 실행할 때도 eval 모듈을 import할 수 있게 한다
    sys.path.insert(0, str(ROOT))

from eval.agent_call_run import (  # noqa: E402
    RUNS_DIR,
    SONBAE_PREFIX,
    VERSIONS,
    CallRecord,
    CallTask,
    load_call_tasks,
)
from eval.agent_decoy import SERVERS as DECOY_SERVERS  # noqa: E402

REPORTS = ROOT / "eval" / "reports"
DECOY_PREFIXES = tuple(f"mcp__{n}__" for n in DECOY_SERVERS)
WEB_TOOLS = {"WebSearch", "WebFetch"}
EDIT_TOOLS = {"Edit", "Write"}
FALLBACK_MARK = "의미 검색을 쓰지 못해"
SERVER_IN_QUERY = "sonbae"  # ToolSearch 검색어에 서버 이름이 있으면 sonbae를 불러온 검색으로 본다
ENV_NAMES = {"e1": "E1 경쟁 (빈 폴더 + 미끼 MCP + 웹 검색)", "e2": "E2 작업 도중 (예제 저장소)"}
EXPECT_NAMES = {"required": "불러야 함", "forbidden": "부르면 안 됨", "optional": "관찰"}


def is_sonbae(name: str) -> bool:
    return name.startswith(SONBAE_PREFIX)


class RunStats:
    """실행 하나에서 뽑은 호출 판단 지표."""

    def __init__(self, record: CallRecord) -> None:
        names = [c.name for c in record.tool_calls]
        first = next((i for i, n in enumerate(names) if is_sonbae(n)), None)
        self.called = first is not None
        # 처음 sonbae를 부르기 전에 쓴 도구 수 (ToolSearch 제외: sonbae를 불러오는 단계라서)
        before = names[:first] if first is not None else names
        self.tools_before = sum(n != "ToolSearch" for n in before)
        # 코드를 고친 뒤에야 sonbae를 부르면 설계 판단이 이미 끝난 늦은 호출로 본다
        self.late = self.called and any(n in EDIT_TOOLS for n in before)
        self.decoy_calls = sum(n.startswith(DECOY_PREFIXES) for n in names)
        self.web_calls = sum(n in WEB_TOOLS for n in names)
        self.searches = sum(n == f"{SONBAE_PREFIX}search" for n in names)
        self.fallbacks = sum(
            1
            for c in record.tool_calls
            if c.name == f"{SONBAE_PREFIX}search" and FALLBACK_MARK in (c.result or "")
        )
        self.decoy_only = not self.called and self.decoy_calls > 0
        self.loaded_by = [
            str(c.input.get("query", ""))
            for c in record.tool_calls
            if c.name == "ToolSearch" and SERVER_IN_QUERY in str(c.input.get("query", ""))
        ]
        self.error = record.is_error
        self.cost = record.cost_usd or 0.0


def load_records(out_dir: Path) -> list[CallRecord]:
    path = out_dir / "records.jsonl"
    return [CallRecord.model_validate_json(line) for line in path.read_text("utf-8").splitlines()]


def _rate(hit: int, total: int) -> str:
    return f"{hit}/{total} ({hit / total:.0%})" if total else "-"


def build_report(records: list[CallRecord], tasks: dict[str, CallTask], name: str) -> str:
    groups: dict[tuple[str, str, str], list[RunStats]] = defaultdict(list)
    per_task: dict[tuple[str, str], list[RunStats]] = defaultdict(list)
    for r in records:
        task = tasks[r.task_id]
        stats = RunStats(r)
        groups[(task.env, task.expect_call, r.version)].append(stats)
        per_task[(r.task_id, r.version)].append(stats)
    versions = [v for v in VERSIONS if any(r.version == v for r in records)]

    lines = [
        "# 호출률 평가 (이슈 #30)",
        "",
        f"실행 묶음 `{name}`, 기록 {len(records)}건."
        " 생성: `uv run python eval/agent_call_report.py`.",
        "서버 버전 `before`는 이슈 #30 변경 전, `after`는 변경 후. 둘 다 기본 동작(지연 로드)이다.",
        "",
        "## 호출 판단",
        "",
        "| 환경 | 기대 | " + " | ".join(versions) + " |",
        "| --- | --- | " + " | ".join("---" for _ in versions) + " |",
    ]
    for env in ENV_NAMES:
        for expect in EXPECT_NAMES:
            cells = []
            for v in versions:
                runs = groups.get((env, expect, v), [])
                cells.append(_rate(sum(s.called for s in runs), len(runs)))
            if any(c != "-" for c in cells):
                lines.append(f"| {env} | {EXPECT_NAMES[expect]} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "칸의 값은 sonbae 도구를 한 번이라도 부른 실행 수다. 부르면 안 됨 행은 낮을수록 좋다.",
        "",
        "## 진단 (불러야 함·관찰 과제)",
        "",
        "| 환경 | 버전 | 늦은 호출 | sonbae 전 도구 수 중앙값 | 미끼만 부름 | 미끼 호출 | 웹 호출 "
        "| 검색 | 대체 검색 | 오류 | 비용 표시 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for env in ENV_NAMES:
        for v in versions:
            runs = groups.get((env, "required", v), []) + groups.get((env, "optional", v), [])
            if not runs:
                continue
            called = [s for s in runs if s.called]
            median = statistics.median(s.tools_before for s in called) if called else "-"
            lines.append(
                f"| {env} | {v} | {_rate(sum(s.late for s in called), len(called))} | {median} "
                f"| {sum(s.decoy_only for s in runs)} | {sum(s.decoy_calls for s in runs)} "
                f"| {sum(s.web_calls for s in runs)} | {sum(s.searches for s in runs)} "
                f"| {sum(s.fallbacks for s in runs)} | {sum(s.error for s in runs)} "
                f"| ${sum(s.cost for s in runs):.2f} |"
            )
    lines += [
        "",
        "- 늦은 호출: 코드를 고친(Edit·Write) 뒤에야 sonbae를 처음 부른 실행 (부른 실행 중)",
        "- sonbae 전 도구 수: 처음 sonbae를 부르기 전에 쓴 도구 수 (ToolSearch 제외)",
        "- 대체 검색: 의미 검색을 못 써 단어 일치 검색으로 바뀐 검색 수 (0이어야 의미 검색 조건)",
        "",
        "## 과제별",
        "",
        "| 과제 | 환경 | 기대 | " + " | ".join(versions) + " | 내용 |",
        "| --- | --- | --- | " + " | ".join("---" for _ in versions) + " | --- |",
    ]
    for task in tasks.values():
        cells = [
            _rate(
                sum(s.called for s in per_task.get((task.id, v), [])),
                len(per_task.get((task.id, v), [])),
            )
            for v in versions
        ]
        if all(c == "-" for c in cells):
            continue
        lines.append(
            f"| {task.id} | {task.env} | {EXPECT_NAMES[task.expect_call]} | "
            + " | ".join(cells)
            + f" | {task.prompt[:40]} |"
        )
    queries = Counter(q for stats in per_task.values() for s in stats for q in s.loaded_by)
    lines += ["", "## sonbae를 불러온 ToolSearch 검색어", ""]
    lines += [f"- `{q}` {n}회" for q, n in queries.most_common(10)] or ["- 없음"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="issue30-call-sonnet")
    parser.add_argument("--out", type=Path, default=REPORTS / "issue30-call-rate.md")
    args = parser.parse_args()
    records = load_records(RUNS_DIR / args.name)
    tasks = {t.id: t for t in load_call_tasks()}
    args.out.write_text(build_report(records, tasks, args.name), encoding="utf-8")
    print(f"→ {args.out}")


if __name__ == "__main__":
    main()
