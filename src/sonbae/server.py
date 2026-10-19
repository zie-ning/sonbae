"""MCP 서버 진입점: 도구 search, get_details, aggregate와 prompt sonbae.

도구 입력 스키마의 문제 유형·도메인 enum은 taxonomy에서 만들어 추출에 쓴 값과 항상 같다.
"""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from sonbae import db, render, taxonomy
from sonbae.search import analyzer, semantic
from sonbae.search import query as q

ProblemType = Literal[taxonomy.problem_type_names()]  # type: ignore[valid-type]
Domain = Literal[taxonomy.domain_names()]  # type: ignore[valid-type]

# 상세는 길어서 한 번에 많이 열면 에이전트 맥락을 많이 차지한다. 소개할 사례만 골라 열게 한다
# (M5 사용자 결정: search가 최대 10건을 돌려줘도 5건 유지)
MAX_DETAIL_IDS = 5

log = logging.getLogger(__name__)

# 세 도구 모두 검색 DB를 읽기만 한다
# (첫 실행 때 고정된 DB 파일을 내려받는 것은 서버 준비 과정이라 도구 동작으로 보지 않는다)
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# search는 검색어를 외부 API(Voyage 임베딩·리랭커)로 보낸다 (M10 의미 검색)
SEARCH_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

COMPANIES = "토스, 우아한형제들, 카카오, 카카오페이, 네이버, LY, 컬리, 올리브영"

# 클라이언트가 도구 정의를 지연 로드하면(Claude Code의 tool search) 처음에는 도구 이름과 이 안내만
# 보인다. 그래서 부를지 판단하는 데 필요한 것(무엇인지, 언제 부르는지, 언제 부르지 않는지)만 담고,
# 인용 규칙·사례 활용 원칙은 호출 전에 불러오는 도구 설명에 둔다 (사용자 결정, 이슈 #30)
INSTRUCTIONS = f"""\
국내 IT 기업({COMPANIES}) 기술 블로그의 2023-09 이후 글에서 뽑은 해결 사례와 실무 경험을
검색·집계한다.

다음 경우에 search로 국내 기업의 비슷한 사례를 먼저 찾는다.
- 선택지가 있는 설계를 정할 때. 작업을 시작할 때뿐 아니라 구현 중에 방식을 골라야 할 때도
  (예: 동시성 제어, 캐시 무효화, 메시지 중복 처리, 재시도·서킷브레이커, DB 전환, 장애 대응,
  LLM 활용)
- 기술을 고르거나 비교할 때 (예: Kafka와 RabbitMQ)
- 사용자가 다른 회사나 국내 기업의 사례·경험을 물을 때
- 장애 회고나 운영·성능 개선 방향을 정할 때
찾은 사례의 근거는 get_details로, 여러 회사가 글로 다룬 기술·문제의 건수는 aggregate로 본다.

단순 CRUD, 버그 수정, 리팩터링, 개념·원리 설명처럼 선택지가 없거나 사례가 필요 없는 작업에는
부르지 않는다."""

SEARCH_DESCRIPTION = f"""\
국내 IT 기업 기술 블로그({COMPANIES})에서
해결 사례와 실무 경험을 검색한다. (2023-09 이후 글)

동시성, 캐싱, 메시징, DB 마이그레이션, 장애 대응, LLM 활용처럼 선택지가 있는 설계를
정할 때(작업 시작 전이든 구현 중이든), 기술을 고르거나 비교할 때, 다른 회사의 사례를 물을 때
호출해 국내 기업이 비슷한 문제를 어떻게 풀었는지 확인할 것.
단순 CRUD, 버그 수정, 리팩터링, 개념·원리 설명에는 호출하지 않는다.

검색어는 뜻으로 찾는다(같은 뜻의 다른 표현, 한국어/영어 표기도 찾음).
검색어는 외부 검색 API로 보내므로 사내 코드, 서비스·고객 이름, 비밀 값은 넣지 말고
일반적인 주제로 쓸 것.
한 가지 주제를 담은 짧은 명사구로 쓰고,
여러 하위 주제를 묶은 요구는 하위 주제별로 나눠 여러 번 검색할 것
(예: "Kafka 주문 이벤트 중복 유실 멱등성" → "Kafka 메시지 중복 처리", "주문 이벤트 유실 방지").
필터로 지정한 조건은 검색어에 다시 쓰지 말고, 그 안에서 찾을 구체적인 주제만 쓸 것
(예: problem_type="트래픽 급증 대응"이면 query="이벤트 대기열").
결과가 없으면 검색어를 나누거나 다른 표현으로 한 번 더 찾아볼 것.

결과는 관련도 기준을 넘은 항목만 관련도 순으로 최대 {q.MAX_RESULTS}건이고,
근거 발췌는 get_details로 본다.
카드의 성능·운영 줄은 적용 결과나 운영 경험이 원문에 있을 때만 나온다.

결과를 답변에 쓸 때:
답변에 소개할 사례는 모두 get_details로 열어 확인한 뒤 인용하고,
카드에 없는 내용을 추측으로 채우지 말 것.
인용할 때는 회사명과 원문 링크를 함께 밝히고, 결과에 없는 사례를 지어내지 말 것.
결과가 없거나 적으면 그렇다고 말할 것.
같은 문제에 다른 선택을 한 사례가 있으면 비교 자료로 함께 소개할 가치가 있다.
{render.REFERENCE_PRINCIPLE}"""

GET_DETAILS_DESCRIPTION = f"""\
search나 aggregate 결과의 항목 ID(case_0001 형식)로 전체 내용을 조회한다.
한 번에 최대 {MAX_DETAIL_IDS}건.

문제 상황·해결 방법·성능/운영 포인트·버린 대안을 돌려주고, 원문 제목·링크와
같은 글의 다른 항목 ID도 알려 준다. 각 필드의 본문 줄은 요약이고, "근거"는 원문에서 그대로
옮긴 문장이다(원문에 있는지 검증함). 원문 표현을 인용할 때는 근거 문장을 쓸 것.

원문 전체가 아니라 요약과 짧은 발췌만 있다. 결과에 없는 세부 내용은 추측으로 채우지 말고
원문 링크를 안내할 것. 답변에 소개할 사례는 모두 이 도구로 열어 확인하고, 소개하지 않을
사례까지 열 필요는 없다. 같은 글의 다른 항목은 질문과 관련 있어 보일 때 이어서 열 것."""

AGGREGATE_DESCRIPTION = f"""\
조건에 맞는 항목을 기술, 문제 유형, 도메인, 회사, 버린 대안별로 센다.
"메시징 문제에 어떤 기술을 많이 다뤘나?", "Kafka 사례를 쓴 회사는 몇 곳?"처럼
여러 회사의 선택을 비교할 때 호출한다.

건수는 {COMPANIES}의 기술 블로그(2023-09 이후)에 글로 쓴 사례의 수다.
회사의 실제 도입 현황이나 국내 업계 전체를 대표하는 값이 아니므로, 답변에서 그렇게 밝힐 것.

범위는 문제 유형·도메인·기술 필터로만 좁힌다(검색어로는 집계할 수 없음).
필터로 표현되지 않는 주제는 search를 쓸 것.

모수(전체 건수·회사 수)와 항목별 건수(성능·운영 포인트에 수치가 있는 건수 포함),
회사 이름, 예시 ID를 돌려준다. 기술은 같은 계열 제품을 상위 기술로 합친다
(예: Amazon MSK는 Kafka 계열). 기술 필터도 같은 기준이라 "Kafka"로 거르면 계열 전체가 포함된다.
예시 ID는 get_details로 이어서 볼 수 있다. 결과에 없는 수치를 지어내지 말 것."""

server = MCPServer(name="sonbae", instructions=INSTRUCTIONS)

QueryArg = Annotated[
    str,
    Field(
        description="찾는 문제 상황이나 주제. 한 주제를 담은 짧은 명사구."
        " 예: 선착순 쿠폰 발급 동시성"
    ),
]
ProblemTypeArg = Annotated[
    ProblemType | None,
    Field(description="문제 유형 필터. 항목의 문제 유형(여러 개) 중 하나라도 맞으면 찾는다"),
]
DomainArg = Annotated[
    Domain | None,
    Field(description="도메인 필터. 항목의 도메인(여러 개) 중 하나라도 맞으면 찾는다"),
]
TechnologiesArg = Annotated[
    list[str] | None,
    Field(
        description="기술 필터 (자유 입력, 예: 카프카 → Kafka). 하나라도 쓴 항목을 찾는다."
        " 상위 기술은 같은 계열의 하위 기술까지 포함한다 (Kafka → Amazon MSK 등)"
    ),
]


_path_lock = threading.Lock()


@cache
def _database_path() -> Path:
    # 쓸 DB는 처음 한 번 정하고 계속 쓴다. 새로 받은 DB는 다음 기동부터 쓴다
    return db.db_path()


def _resolve_path() -> Path:
    # 기동 때 시작한 DB 준비(첫 실행이면 다운로드)와 도구 호출이 겹치면 도구 호출이 끝나길 기다린다
    with _path_lock:
        path = _database_path()
        if not path.exists():
            # 다른 sonbae 프로세스가 옛 버전을 정리했거나 캐시를 비운 경우. 실패하는 대신 경로를
            # 다시 찾아 그때 쓸 수 있는 DB로 넘어간다
            _database_path.cache_clear()
            path = _database_path()
        return path


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    """도구 호출 하나가 혼자 쓰는 연결. 호출마다 열고 닫는다 (이슈 #22).

    MCP SDK는 동기 도구를 작업 스레드에서 실행해 호출이 동시에 돌 수 있다. 연결 하나를 나눠
    쓰면 질의 결과가 섞여, 연결은 호출 범위 밖으로 내보내지 않는다. 여는 비용은 약 1ms다.
    """
    try:
        conn = db.connect(_resolve_path())
    except db.DatabaseNotFound as e:
        # 예상한 실패라 ToolError로 바꿔 이유와 해결 방법만 도구 결과로 돌려준다
        raise ToolError(str(e)) from e
    try:
        yield conn
    finally:
        conn.close()


class _Readiness:
    """기동 준비(DB·항목 임베딩, 형태소 분석기)가 끝났는지. 원격 서버의 `/health`가 본다 (M9)."""

    STEPS = ("db", "analyzer")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.done = {step: threading.Event() for step in self.STEPS}
        self.errors: dict[str, str] = {}

    def status(self) -> tuple[str, dict[str, str]]:
        """("ok" | "starting" | "error", 단계별 상태). 실패한 단계는 재시작 전까지 그대로다."""
        steps = {}
        for step, event in self.done.items():
            if event.is_set():
                steps[step] = "ok"
            elif step in self.errors:
                steps[step] = (
                    "error"  # 이유는 로그에만 남긴다(경로 등이 담길 수 있어 응답에는 안 넣음)
                )
            else:
                steps[step] = "starting"
        if all(v == "ok" for v in steps.values()):
            return "ok", steps
        if "error" in steps.values():
            return "error", steps
        return "starting", steps


readiness = _Readiness()


def _quietly(name: str, step: Callable[[], object]) -> None:
    try:
        step()
    except Exception as e:
        # 실패하면 도구를 호출할 때 다시 시도하며 이유를 결과로 알린다
        readiness.errors[name] = str(e) or type(e).__name__
        log.warning("기동 준비 실패(%s): %s", name, readiness.errors[name])


def _connect_then_check_update() -> None:
    # 지금 쓸 DB와 항목 임베딩(의미 검색)을 먼저 준비한 뒤 새 버전을 받아 둔다.
    # 새 DB는 다음 기동부터 쓰므로 새 버전 확인은 준비 완료를 늦추지 않는다
    with _connection() as conn:
        semantic.load_vectors(conn)
    readiness.done["db"].set()
    db.check_for_update()


def _warm_up_analyzer() -> None:
    analyzer.warm_up()
    readiness.done["analyzer"].set()


def _prepare() -> list[threading.Thread]:
    """첫 도구 호출을 빠르게 하려고 DB(첫 실행이면 다운로드)·항목 임베딩과 형태소 분석기를
    동시에 준비한다.

    DB를 연 뒤에는 하루 한 번 최신 DB가 있는지 확인해 받아 둔다.
    """
    readiness.reset()
    threads = [
        threading.Thread(target=_quietly, args=(name, step), daemon=True)
        for name, step in (("db", _connect_then_check_update), ("analyzer", _warm_up_analyzer))
    ]
    for thread in threads:
        thread.start()
    return threads


@contextmanager
def _tool_log(tool: str) -> Iterator[dict]:
    """도구 호출 하나의 소요 시간과 결과를 `tool_call` 로그로 남긴다. 넘겨준 dict에 필드를 더한다.

    검색어 원문은 사용자의 설계 내용이 담길 수 있어 남기지 않고 길이만 남긴다. 원격 서버는 JSON으로
    표준 출력에 쓰고(`sonbae.logs`), stdio 서버는 경고 이상만 표준 오류로 낸다(`main`).
    """
    fields: dict = {"event": "tool_call", "tool": tool}
    start = time.perf_counter()
    try:
        yield fields
        fields["ok"] = True
    except Exception as e:
        fields.update(ok=False, error=type(e).__name__)
        raise
    finally:
        fields["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)
        log.info("tool_call", extra={"fields": fields})


def _filters(
    problem_type: str | None, domain: str | None, technologies: list[str] | None
) -> tuple[q.Filters, list[str]]:
    resolution = q.resolve_technologies(technologies or [])
    filters = q.Filters(
        problem_type=problem_type,
        domain=domain,
        technologies=resolution.normalized,
    )
    return filters, render.unknown_technologies(resolution)


@server.tool(
    description=SEARCH_DESCRIPTION, structured_output=False, annotations=SEARCH_ANNOTATIONS
)
def search(
    query: QueryArg,
    problem_type: ProblemTypeArg = None,
    domain: DomainArg = None,
    technologies: TechnologiesArg = None,
) -> str:
    with _tool_log("search") as fields:
        filters, notes = _filters(problem_type, domain, technologies)
        fields.update(
            query_chars=len(query),
            problem_type=problem_type,
            domain=domain,
            technologies=filters.technologies,
        )
        with _connection() as conn:
            result = q.search(conn, query, filters)
        fields.update(
            mode="semantic" if result.semantic else "lexical",
            fallback=result.fallback,
            results=len(result.entries),
        )
        return render.search_result(query, filters, result, notes)


@server.tool(description=GET_DETAILS_DESCRIPTION, structured_output=False, annotations=READ_ONLY)
def get_details(
    ids: Annotated[
        list[str], Field(min_length=1, description=f"항목 ID 목록. 최대 {MAX_DETAIL_IDS}건")
    ],
) -> str:
    with _tool_log("get_details") as fields:
        ids = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
        wanted, dropped = ids[:MAX_DETAIL_IDS], ids[MAX_DETAIL_IDS:]
        with _connection() as conn:
            details, missing = q.get_details(conn, wanted)
        fields.update(requested=len(ids), found=len(details), missing=len(missing))
        return render.details_result(details, missing, dropped, MAX_DETAIL_IDS)


@server.tool(description=AGGREGATE_DESCRIPTION, structured_output=False, annotations=READ_ONLY)
def aggregate(
    group_by: Annotated[q.GroupBy, Field(description="집계 기준")],
    problem_type: ProblemTypeArg = None,
    domain: DomainArg = None,
    technologies: TechnologiesArg = None,
    top_n: Annotated[int, Field(ge=1, le=50, description="상위 몇 개까지 보여 줄지")] = 10,
) -> str:
    with _tool_log("aggregate") as fields:
        filters, notes = _filters(problem_type, domain, technologies)
        fields.update(
            group_by=group_by,
            problem_type=problem_type,
            domain=domain,
            technologies=filters.technologies,
        )
        with _connection() as conn:
            result = q.aggregate(conn, group_by, filters, top_n)
        return render.aggregate_result(group_by, filters, result, notes)


@server.prompt(
    name="sonbae",
    description="주제에 대한 국내 기업 사례를 참고 자료로 소개하고, 현재 작업에 맞는 선택지를 "
    "따로 제안한다.",
)
def sonbae(topic: Annotated[str, Field(description="찾아볼 설계 주제나 현재 작업")]) -> str:
    return f"""\
다음 주제에 대해 sonbae MCP 도구로 국내 기업 사례를 찾아 참고 자료로 소개해 줘: {topic}

1. search로 관련 사례를 찾는다. 주제가 여러 하위 주제로 나뉘면 하위 주제별로 나눠 검색하고,
   결과가 적으면 검색어를 바꾸거나 필터를 빼서 한 번 더 찾는다.
2. 소개할 사례는 모두 get_details로 열어 근거 발췌를 확인한다. 같은 문제에 다른 선택을 한 사례가
   있으면 비교 자료로 함께 연다.
3. 회사별 접근 방식(문제 상황, 해결 방법, 성능·운영 포인트, 버린 대안)을 출처와 함께 소개한다.
4. 각 사례의 조건(규모, 제약, 기존 인프라)이 지금 작업과 어떻게 다른지 짚는다.
5. 사례에 얽매이지 않고, 지금 작업에 맞는 선택지를 트레이드오프와 함께 제안한다.

답변은 "참고 사례"와 "제안"을 나눠서 쓴다. 사례를 그대로 적용하라고 권하지 않는다.
사례를 인용할 때는 회사명과 원문 링크를 밝히고, 도구 결과에 없는 사례는 지어내지 않는다.
찾은 사례가 없거나 적으면 그렇다고 분명히 말한다."""


def main() -> None:
    # SDK가 INFO 로그를 표준 오류로 내도록 설정하므로, stdio에서는 도구 호출 로그를 내지 않는다
    logging.getLogger("sonbae").setLevel(logging.WARNING)
    # initialize 응답을 막지 않도록 준비는 백그라운드에서 한다
    _prepare()
    server.run()
