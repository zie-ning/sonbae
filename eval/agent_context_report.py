"""MCP 컨텍스트 리포트 (이슈 #30): sonbae 정의·응답이 세션 컨텍스트를 얼마나 쓰는지 정리한다.

    uv run python eval/agent_context_report.py   # → eval/reports/issue30-context.md

입력은 두 가지다.
- 호출률 평가(eval/agent_call_run.py)의 원본 로그. 요청마다 남은 토큰 사용량(입력 + 캐시 생성 +
  캐시 읽기 = 그 요청의 컨텍스트)으로, 단계마다 컨텍스트가 얼마나 늘었는지 본다
- 응답 크기 측정(eval/response_size.py)의 버전별 JSON

한 단계의 증가분은 다음 요청의 컨텍스트 − 이번 요청의 컨텍스트다. 도구 결과뿐 아니라 그 단계의
에이전트 출력(생각, 도구 호출 블록)도 들어가므로, 도구별 토큰은 위쪽으로 잡힌 값이다.
sonbae 도구만 부른 단계를 sonbae 응답으로, sonbae 도구만 불러온 ToolSearch 단계를 정의 로드로 센다.
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 스크립트로 실행할 때도 eval 모듈을 import할 수 있게 한다
    sys.path.insert(0, str(ROOT))

from eval.agent_call_run import RUNS_DIR, SONBAE_PREFIX, VERSIONS, load_call_tasks  # noqa: E402
from eval.agent_run import _event, _text  # noqa: E402

REPORTS = ROOT / "eval" / "reports"
SIZE_DIR = ROOT / "eval" / "runs" / "context"
ENV_NAMES = {"e1": "E1 경쟁", "e2": "E2 작업 도중"}


@dataclass
class Step:
    context: int  # 이 요청의 컨텍스트 토큰
    tools: list[str] = field(default_factory=list)
    tool_inputs: list[dict] = field(default_factory=list)
    result_chars: int = 0  # 이 단계 도구 결과의 글자 수


@dataclass
class RunContext:
    key: str
    steps: list[Step]

    @property
    def first(self) -> int:
        return self.steps[0].context if self.steps else 0

    @property
    def last(self) -> int:
        return self.steps[-1].context if self.steps else 0

    def _growth(self, i: int) -> int:
        return self.steps[i + 1].context - self.steps[i].context if i + 1 < len(self.steps) else 0

    def _sonbae_only(self, step: Step) -> bool:
        return bool(step.tools) and all(t.startswith(SONBAE_PREFIX) for t in step.tools)

    def _loads_sonbae(self, step: Step) -> bool:
        if step.tools != ["ToolSearch"]:
            return False
        query = str(step.tool_inputs[0].get("query", ""))
        names = query.removeprefix("select:").split(",")
        return query.startswith("select:") and all(n.startswith(SONBAE_PREFIX) for n in names)

    @property
    def sonbae_tokens(self) -> int:
        return sum(self._growth(i) for i, s in enumerate(self.steps) if self._sonbae_only(s))

    @property
    def sonbae_chars(self) -> int:
        return sum(s.result_chars for s in self.steps if self._sonbae_only(s))

    @property
    def definition_tokens(self) -> list[int]:
        return [self._growth(i) for i, s in enumerate(self.steps) if self._loads_sonbae(s)]

    def per_call(self, tool: str) -> list[float]:
        """그 도구 하나만 부른 단계(병렬 호출이면 같은 도구 여러 번)의 호출 1회당 증가 토큰."""
        name = f"{SONBAE_PREFIX}{tool}"
        return [
            self._growth(i) / len(s.tools)
            for i, s in enumerate(self.steps)
            if s.tools and all(t == name for t in s.tools) and self._growth(i) > 0
        ]

    @property
    def called(self) -> bool:
        return any(self._sonbae_only(s) for s in self.steps)


def parse_context(lines: list[str], key: str) -> RunContext:
    """요청(메시지 id)별 컨텍스트와 그 요청이 부른 도구, 돌아온 결과 글자 수를 모은다."""
    steps: list[Step] = []
    by_id: dict[str, Step] = {}
    owner: dict[str, Step] = {}  # tool_use id → 그 도구를 부른 단계
    for line in lines:
        event = _event(line)
        if event.get("type") == "assistant":
            message = event.get("message", {})
            usage = message.get("usage") or {}
            context = (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
            step = by_id.get(message.get("id"))
            if step is None:
                step = Step(context=context)
                by_id[message.get("id")] = step
                steps.append(step)
            for block in message.get("content", []):
                if block.get("type") == "tool_use":
                    step.tools.append(block["name"])
                    step.tool_inputs.append(block.get("input") or {})
                    owner[block["id"]] = step
        elif event.get("type") == "user":
            content = event.get("message", {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in owner:
                    owner[block["tool_use_id"]].result_chars += len(_text(block.get("content")))
    return RunContext(key=key, steps=steps)


def load_runs(out_dir: Path) -> list[RunContext]:
    return [
        parse_context(raw.read_text(encoding="utf-8").splitlines(), raw.stem)
        for raw in sorted((out_dir / "raw").glob("*.jsonl"))
    ]


def _median(values: list[int]) -> str:
    return f"{statistics.median(values):,.0f}" if values else "-"


def _sizes(size_dir: Path) -> dict[str, dict]:
    return {
        v: json.loads((size_dir / f"{v}.json").read_text(encoding="utf-8"))
        for v in VERSIONS
        if (size_dir / f"{v}.json").exists()
    }


def build_report(runs: list[RunContext], env_of: dict[str, str], sizes: dict[str, dict]) -> str:
    lines = [
        "# MCP 컨텍스트 (이슈 #30)",
        "",
        "생성: `uv run python eval/agent_context_report.py`. 서버 버전 `before`는 이슈 #30 변경 전,"
        " `after`는 변경 후.",
        "",
        "## 정의·응답 크기 (글자 수)",
        "",
        "평가셋 62개, 단어 일치 검색(`eval/response_size.py`). `get_details`는 search 상위 5건.",
        "",
    ]
    versions = [v for v in VERSIONS if v in sizes]
    if versions:
        lines += [
            "| 항목 | " + " | ".join(versions) + " |",
            "| --- | " + " | ".join("---" for _ in versions) + " |",
        ]
        rows = [
            ("서버 안내", lambda d: d["definitions"]["instructions"]),
            ("search 설명", lambda d: d["definitions"]["tools"]["search"]["description"]),
            ("search 스키마", lambda d: d["definitions"]["tools"]["search"]["schema"]),
            (
                "get_details·aggregate 설명+스키마",
                lambda d: sum(
                    d["definitions"]["tools"][t][k]
                    for t in ("get_details", "aggregate")
                    for k in ("description", "schema")
                ),
            ),
            ("search 응답 중앙값", lambda d: d["search_chars"]["median"]),
            ("search 응답 최대", lambda d: d["search_chars"]["max"]),
            ("get_details 5건 중앙값", lambda d: d["details_chars"]["median"]),
            ("get_details 5건 최대", lambda d: d["details_chars"]["max"]),
        ]
        for label, get in rows:
            lines.append(
                f"| {label} | " + " | ".join(f"{get(sizes[v]):,.0f}" for v in versions) + " |"
            )
        lines.append("")

    groups: dict[tuple[str, str], list[RunContext]] = defaultdict(list)
    for r in runs:
        task_id, version, _ = r.key.split("__")
        groups[(env_of[task_id], version)].append(r)
    lines += [
        "## 세션 컨텍스트 (토큰)",
        "",
        "호출률 평가 실행 로그. sonbae를 부른 실행만 센다."
        " 비율은 실행마다 sonbae 응답 토큰 ÷ 마지막 요청 컨텍스트의 중앙값.",
        "",
        "| 환경 | 버전 | 실행 | 첫 요청 | 마지막 요청 | 정의 로드 | sonbae 응답 | 비율 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for env in ENV_NAMES:
        for v in VERSIONS:
            called = [r for r in groups.get((env, v), []) if r.called]
            if not called:
                continue
            shares = [r.sonbae_tokens / r.last for r in called if r.last]
            definitions = [t for r in called for t in r.definition_tokens]
            lines.append(
                f"| {ENV_NAMES[env]} | {v} | {len(called)} | {_median([r.first for r in called])} "
                f"| {_median([r.last for r in called])} | {_median(definitions)} "
                f"| {_median([r.sonbae_tokens for r in called])} "
                f"| {statistics.median(shares):.0%} |"
            )
    lines += [
        "",
        "호출 1회당 (그 도구만 부른 단계, 버전별 전체 실행의 중앙값):",
        "",
        "| 버전 | search 1회 | get_details 1회 | search 호출 수 | get_details 호출 수 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for v in VERSIONS:
        in_version = [r for r in runs if r.key.split("__")[1] == v]
        if not in_version:
            continue
        searches = [t for r in in_version for t in r.per_call("search")]
        details = [t for r in in_version for t in r.per_call("get_details")]
        count = {
            tool: sum(
                t == f"{SONBAE_PREFIX}{tool}" for r in in_version for s in r.steps for t in s.tools
            )
            for tool in ("search", "get_details")
        }
        lines.append(
            f"| {v} | {_median(searches)} | {_median(details)} | {count['search']} "
            f"| {count['get_details']} |"
        )
    tokens = sum(r.sonbae_tokens for r in runs)
    chars = sum(r.sonbae_chars for r in runs)
    lines += [
        "",
        "- 첫 요청: 시스템 프롬프트, 도구 이름 목록, 서버 안내가 든 시작 컨텍스트",
        "- 정의 로드: sonbae 도구만 불러온 ToolSearch 단계에서 늘어난 토큰 (중앙값)",
        "- sonbae 응답: sonbae 도구만 부른 단계에서 늘어난 토큰의 합 (실행별 중앙값)",
        f"- sonbae 응답 단계 전체: {tokens:,} 토큰 / 결과 {chars:,}자"
        + (f" (1자당 약 {tokens / chars:.2f} 토큰, 에이전트 출력 포함)" if chars else ""),
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="issue30-call-sonnet")
    parser.add_argument("--sizes", type=Path, default=SIZE_DIR)
    parser.add_argument("--out", type=Path, default=REPORTS / "issue30-context.md")
    args = parser.parse_args()
    env_of = {t.id: t.env for t in load_call_tasks()}
    report = build_report(load_runs(RUNS_DIR / args.name), env_of, _sizes(args.sizes))
    args.out.write_text(report, encoding="utf-8")
    print(f"→ {args.out}")


if __name__ == "__main__":
    main()
