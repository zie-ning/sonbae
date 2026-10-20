"""응답 형식 평가 채점·집계 (이슈 #36): before·after 서버 답변을 인용·충실도·품질로 비교한다.

    uv run python eval/agent_format_eval.py --dry-run     # 판정 수와 예상 비용, 지금까지의 집계
    uv run python eval/agent_format_eval.py               # 남은 판정 후 집계 (캐시는 건너뜀)
    uv run python eval/agent_format_eval.py --report      # 집계를 eval/reports/issue36-format.md로

실행 기록은 eval/agent_format_run.py가 만든다. 지표:
① 열지 않고 인용: 답변에 링크로 인용한 글 중 get_details로 연 항목이 하나도 없는 글의 비율
② 결과에 없는 출처: 답변의 링크 중 sonbae 도구 결과 어디에도 없는 블로그 글 링크 수
③ 충실도: 답변이 인용한 글에 대해 서술한 내용을 그 글 항목의 전체 데이터(요약 + 근거 발췌)와
   대조해 뒷받침됨·일부·근거 없음으로 나눈다 (LLM 판정). 버전 이름은 판정기에 주지 않는다
④ 답변 품질: 같은 과제·회차의 after와 before 답변을 M7 판정기(eval/agent_judge.py)로 비교한다
⑤ 컨텍스트(진단): 실행마다 sonbae 도구 결과 글자 수
판정은 eval/runs/agent/<name>/judge.jsonl(④)과 faithfulness.jsonl(③)에 캐시한다.
"""

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 스크립트로 실행할 때도 eval·pipeline을 import할 수 있게 한다
    sys.path.insert(0, str(ROOT))

from eval.agent_judge import (  # noqa: E402
    JUDGE_EFFORT,
    JUDGE_FILE,
    JUDGE_MODEL,
    JudgeRecord,
    Pair,
    judge_one,
    load_judgements,
    pair_result,
    pending,
)
from eval.agent_run import RUNS_DIR, RunRecord  # noqa: E402
from eval.agent_tasks import Task, load_tasks  # noqa: E402
from eval.inspect_run import cost, load_pricing  # noqa: E402
from pipeline.extract.schema import Usage  # noqa: E402

ENTRIES = ROOT / "data" / "entries.jsonl"
REPORT = ROOT / "eval" / "reports" / "issue36-format.md"
FAITH_FILE = "faithfulness.jsonl"
INTERPRETATION = "## 해석"
SONBAE_TOOLS = {"search", "get_details", "aggregate"}
LEFT, RIGHT = "after", "before"  # 쌍 판정의 left/right

_URL = re.compile(r"https?://[^\s)\]>\"'`]+")
_CASE = re.compile(r"^\[(case_\d+)\]", re.M)
_SOURCE = re.compile(r"^원문: (\S+)", re.M)


def _norm(url: str) -> str:
    return url.rstrip(".,;:").rstrip("/")


# ---------- ①② 인용 지표 (코드) ----------


@dataclass
class Citations:
    cited: set[str]  # 답변에 링크로 인용한 글 (sonbae 결과에 있던 것)
    unopened: set[str]  # 그중 get_details로 연 항목이 하나도 없는 글
    unknown: set[str]  # 답변의 링크 중 sonbae 결과에 없는 블로그 글 링크
    result_chars: int  # sonbae 도구 결과 글자 수 합
    shown: dict[str, set[str]]  # sonbae 결과에 나온 글 링크 → 나온 항목 ID


def sonbae_calls(record: RunRecord):
    return [c for c in record.tool_calls if c.name in SONBAE_TOOLS and not c.is_error]


def citations(record: RunRecord, blog_hosts: set[str]) -> Citations:
    calls = sonbae_calls(record)
    # 결과에 나온 글 링크 → 그 글의 항목 ID. 카드·상세 모두 "[case_…]"로 시작해 "원문:" 줄을 가진다
    shown: dict[str, set[str]] = {}
    for call in calls:
        for block in re.split(r"\n(?=\[case_\d+\])", call.result or ""):
            case, source = _CASE.search(block), _SOURCE.search(block)
            if case and source:
                shown.setdefault(_norm(source.group(1)), set()).add(case.group(1))
    opened = {i for c in calls if c.name == "get_details" for i in c.input.get("ids", [])}
    links = {_norm(u) for u in _URL.findall(record.answer)}
    cited = {u for u in links if u in shown}
    unknown = {u for u in links - cited if urlsplit(u).netloc in blog_hosts}
    return Citations(
        cited=cited,
        unopened={u for u in cited if not shown[u] & opened},
        unknown=unknown,
        result_chars=sum(len(c.result or "") for c in calls),
        shown=shown,
    )


def blog_hosts(entries: dict[str, list[dict]]) -> set[str]:
    return {urlsplit(u).netloc for u in entries}


# ---------- ③ 충실도 (LLM) ----------

FAITH_INSTRUCTIONS = """\
너는 기술 문서의 사실 확인 담당자다.
코딩 에이전트가 국내 기업 기술 블로그 사례를 인용해 답변을 썼다.
아래 "사례 데이터"는 그 블로그 글에서 뽑은 요약과 원문 발췌다(원문에 실제로 있는 문장).

사례 데이터는 에이전트가 이번 답변을 쓰면서 도구로 받은 사례 전부다.
답변에서 국내 기업 사례(회사명이나 링크로 가리킨 것)를 두고 "그 회사가 무엇을 했다, 어떤 문제가
있었다, 어떤 수치가 나왔다, 무엇을 버렸다"처럼 서술한 내용을 하나씩 뽑아 판정한다.
링크 없이 회사명만 쓴 서술도 판정한다. 사례 데이터에 없는 회사·글의 사례를 서술했으면 unsupported.
- supported: 사례 데이터(요약 또는 발췌)로 뒷받침된다. 다른 말로 바꿔 쓴 것도 뜻이 같으면 supported.
- partial: 핵심은 맞지만 사례 데이터에 없는 세부(수치, 구현 방법, 이유)가 덧붙었거나
  범위가 넓어졌다.
- unsupported: 사례 데이터에 없거나 어긋난다 (지어낸 수치·방법, 다른 글의 내용을 섞음, 뜻이 바뀜).

판정하지 않는 것: 사례에 대한 서술이 아닌 일반 설명·에이전트 자신의 제안·사용자 상황과의 비교,
링크만 나열한 참고 목록. 같은 내용을 반복한 서술은 한 번만 센다.
claim에는 답변의 해당 서술을 짧게 옮기고, note에는 판정 이유를 한 문장으로 쓴다.
"""


class Claim(BaseModel):
    source: str = Field(description="서술 대상 글의 링크 (사례 데이터에 없으면 회사명)")
    claim: str
    verdict: Literal["supported", "partial", "unsupported"]
    note: str


class FaithOutput(BaseModel):
    claims: list[Claim]


class FaithRecord(BaseModel):
    key: str
    output: FaithOutput
    judge_model: str
    judge_effort: str
    prompt_version: str
    input_hash: str
    usage: Usage


def load_entries(path: Path = ENTRIES) -> dict[str, list[dict]]:
    """글 링크 → 그 글의 항목 목록."""
    by_post: dict[str, list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            e = json.loads(line)
            by_post.setdefault(_norm(e["post_url"]), []).append(e)
    return by_post


def case_data(url: str, entries: list[dict]) -> str:
    """판정 기준 데이터: 항목을 요약 + 발췌로 (서버가 어떤 형식으로 보냈는지와 무관)."""
    first = entries[0]
    lines = [f"### {first['company']} · {first['post_title']}", f"링크: {url}"]
    fields = [
        ("문제 상황", "problem_situation"),
        ("해결 방법", "solution"),
        ("성능·운영", "performance_ops"),
    ]
    for e in entries:
        lines.append(f"\n[{e['id']}]")
        for title, key in fields:
            for p in e[key]:
                lines.append(f'- {title}: {p["text"]}\n  발췌: "{p["evidence"]}"')
        for r in e.get("rejected_alternatives", []):
            lines.append(f'- 버린 대안 {r["name"]}: {r["reason"]}\n  발췌: "{r["evidence"]}"')
    return "\n".join(lines)


def faith_message(
    record: RunRecord, shown: dict[str, set[str]], entries: dict[str, list[dict]]
) -> str:
    # 도구 결과에 나온 항목만 넣는다. 링크 없이 회사명으로 서술한 사례도 대조할 수 있게 한다
    data = "\n\n".join(
        case_data(u, [e for e in entries[u] if e["id"] in ids])
        for u, ids in sorted(shown.items())
        if u in entries
    )
    return f"## 답변\n{record.answer}\n\n## 사례 데이터\n{data}"


def faith_version() -> str:
    schema = json.dumps(FaithOutput.model_json_schema(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((FAITH_INSTRUCTIONS + schema).encode("utf-8")).hexdigest()[:12]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_faith(path: Path) -> dict[str, FaithRecord]:
    if not path.exists():
        return {}
    records = [
        FaithRecord.model_validate_json(x) for x in path.read_text("utf-8").splitlines() if x
    ]
    return {r.key: r for r in records}


def faith_one(llm, key: str, message: str) -> FaithRecord:
    usage = Usage()
    output = llm.parse(
        FAITH_INSTRUCTIONS, [{"role": "user", "content": message}], FaithOutput, usage, "faith"
    )
    return FaithRecord(
        key=key,
        output=output,
        judge_model=llm.model,
        judge_effort=llm.reasoning_effort,
        prompt_version=faith_version(),
        input_hash=_hash(message),
        usage=usage,
    )


# ---------- ④ 쌍 판정 ----------


def build_pairs(tasks: list[Task], records: list[RunRecord]) -> list[Pair]:
    by_key = {(r.task_id, r.condition, r.rep): r for r in records}
    pairs = []
    for task in tasks:
        for rep in sorted({r for (t, c, r) in by_key if t == task.id and c == LEFT}):
            a, b = by_key.get((task.id, LEFT, rep)), by_key.get((task.id, RIGHT, rep))
            if a and b and not a.is_error and not b.is_error:
                pairs.append(Pair(task, a, b))
    return pairs


# ---------- 집계 ----------


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d * 100:.0f}%)" if d else "0/0"


def summarize(
    records: list[RunRecord],
    entries: dict[str, list[dict]],
    faith: dict[str, FaithRecord],
    pairs: list[Pair],
    judged: dict[tuple[str, str], JudgeRecord],
) -> list[str]:
    hosts = blog_hosts(entries)
    lines = ["| 지표 | before | after |", "| --- | --- | --- |"]
    rows: dict[str, dict[str, str]] = {}
    for version in (RIGHT, LEFT):
        runs = [r for r in records if r.condition == version and not r.is_error]
        cs = [citations(r, hosts) for r in runs]
        cited = sum(len(c.cited) for c in cs)
        verdicts = Counter(
            claim.verdict for r in runs if r.key in faith for claim in faith[r.key].output.claims
        )
        total = sum(verdicts.values())
        searches = [sum(c.name == "search" for c in r.tool_calls) for r in runs]
        details = [sum(c.name == "get_details" for c in r.tool_calls) for r in runs]
        chars = [c.result_chars for c in cs]
        rows[version] = {
            "실행 (오류 제외)": str(len(runs)),
            "① 열지 않고 인용한 글": _pct(sum(len(c.unopened) for c in cs), cited),
            "② 결과에 없는 블로그 링크": str(sum(len(c.unknown) for c in cs)),
            "③ 서술 판정 수": str(total),
            "③ 근거 없음": _pct(verdicts["unsupported"], total),
            "③ 일부": _pct(verdicts["partial"], total),
            "⑤ search 호출 (실행당 평균)": f"{statistics.mean(searches):.2f}" if runs else "-",
            "⑤ get_details 호출 (실행당 평균)": f"{statistics.mean(details):.2f}" if runs else "-",
            "⑤ sonbae 결과 글자 (실행 중앙값)": f"{statistics.median(chars):,.0f}" if runs else "-",
        }
    for name in rows[RIGHT]:
        lines.append(f"| {name} | {rows[RIGHT][name]} | {rows[LEFT][name]} |")

    tally = Counter()
    for pair in pairs:
        result = pair_result(
            [
                judged[(pair.key, o)]
                for o in ("left_first", "right_first")
                if (pair.key, o) in judged
            ]
        )
        if result:
            tally[result["overall"]] += 1
    n = sum(tally.values())
    lines.append("")
    lines.append(
        f"④ 답변 품질 (after 기준, 쌍 {n}): "
        f"승 {tally['left']} · 무 {tally['tie']} · 패 {tally['right']}"
    )
    return lines


def load_records(run_dir: Path) -> list[RunRecord]:
    path = run_dir / "records.jsonl"
    return [RunRecord.model_validate_json(x) for x in path.read_text("utf-8").splitlines() if x]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--name", default="issue36-format-sonnet")
    parser.add_argument("--model", default=JUDGE_MODEL)
    parser.add_argument("--effort", default=JUDGE_EFFORT)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--env-file", type=Path, default=ROOT / ".env", help="OPENAI_API_KEY를 읽을 .env"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true", help=f"집계를 {REPORT.name}에 쓴다")
    args = parser.parse_args()

    run_dir = RUNS_DIR / args.name
    records = load_records(run_dir)
    tasks = load_tasks()
    entries = load_entries()
    hosts = blog_hosts(entries)

    # ③ 판정 대상: 인용한 글이 있는 실행
    faith_path = run_dir / FAITH_FILE
    faith = load_faith(faith_path)
    version = faith_version()
    faith_todo = []
    for r in records:
        if r.is_error:
            continue
        shown = citations(r, hosts).shown
        if not shown.keys() & entries.keys():
            continue
        message = faith_message(r, shown, entries)
        cached = faith.get(r.key)
        if not (
            cached
            and cached.judge_model == args.model
            and cached.judge_effort == args.effort
            and cached.prompt_version == version
            and cached.input_hash == _hash(message)
        ):
            faith_todo.append((r.key, message))

    # ④ 판정 대상
    judge_path = run_dir / JUDGE_FILE
    judged = load_judgements(judge_path)
    pairs = build_pairs(tasks, records)
    pair_todo = pending(pairs, judged, args.model, args.effort)

    price = load_pricing().get(args.model)
    if price:
        # 한국어는 대략 2자당 1토큰. 출력은 reasoning 포함 대략치
        tokens_in = sum(len(m) // 2 for _, m in faith_todo) + sum(
            (len(p.left.answer) + len(p.right.answer)) // 2 for p, _ in pair_todo
        )
        out = 3000 * len(faith_todo) + 2000 * len(pair_todo)
        estimate = f"${cost(Usage(input_tokens=tokens_in, output_tokens=out), price):.2f}"
    else:
        estimate = "단가 없음"
    print(
        f"충실도 판정 {len(faith_todo)}회, 쌍 판정 {len(pair_todo)}회 "
        f"({args.model} {args.effort}), 예상 비용 {estimate}"
    )

    if not args.dry_run and (faith_todo or pair_todo):
        from dotenv import load_dotenv

        from pipeline.extract.extractor import OpenAILLM

        load_dotenv(args.env_file)
        llm = OpenAILLM(args.model, args.effort)
        total = Usage()
        with (
            ThreadPoolExecutor(max_workers=args.workers) as pool,
            faith_path.open("a", encoding="utf-8") as ff,
            judge_path.open("a", encoding="utf-8") as jf,
        ):
            futures = {pool.submit(faith_one, llm, k, m): ("faith", k) for k, m in faith_todo}
            futures |= {
                pool.submit(judge_one, llm, p, o): ("pair", f"{p.key} {o}") for p, o in pair_todo
            }
            for i, future in enumerate(as_completed(futures), 1):
                kind, label = futures[future]
                try:
                    record = future.result()
                except Exception as e:  # 하나가 실패해도 나머지는 계속한다. 다시 돌리면 이어서 판정
                    print(f"[{i}/{len(futures)}] {kind} {label} 실패: {e}", flush=True)
                    continue
                if kind == "faith":
                    ff.write(record.model_dump_json() + "\n")
                    ff.flush()
                    faith[record.key] = record
                else:
                    jf.write(record.model_dump_json() + "\n")
                    jf.flush()
                    judged[(record.pair, record.order)] = record
                total.add(record.usage)
                print(f"[{i}/{len(futures)}] {kind} {label} 완료", flush=True)
        if price:
            print(f"실제 비용 ${cost(total, price):.2f}")

    lines = summarize(records, entries, faith, pairs, judged)
    print("\n".join(lines))
    if args.report:
        # 사람이 쓴 해석(INTERPRETATION 제목 아래)은 다시 생성해도 남긴다
        old = REPORT.read_text(encoding="utf-8") if REPORT.exists() else ""
        kept = old[old.index(INTERPRETATION) :] if INTERPRETATION in old else ""
        REPORT.write_text(
            "# get_details 응답 형식 평가 (이슈 #36)\n\n"
            "생성: `uv run python eval/agent_format_eval.py --report`."
            " before는 근거 발췌·분류·기술·태그를 상세에 넣은 서버, after는 뺀 서버.\n\n"
            + "\n".join(lines)
            + "\n"
            + (f"\n{kept}" if kept else ""),
            encoding="utf-8",
        )
        print(f"→ {REPORT}")


if __name__ == "__main__":
    main()
