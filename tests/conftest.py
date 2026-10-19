from pathlib import Path

import httpx
import pytest

from pipeline.build_index import build
from pipeline.extract.schema import Entry, Evidenced, RejectedAlternative

FIXTURES = Path(__file__).parent / "fixtures"


def make_entry(entry_id: str, **overrides) -> Entry:
    base = dict(
        id=entry_id,
        source="oliveyoung",
        company="올리브영",
        post_url=f"https://example.com/{entry_id}",
        post_title="제목",
        published_at="2025-01-01",
        problem_types=["동시성·락"],
        domains=["커머스·주문·재고"],
        technologies=[],
        technologies_raw=[],
        tags=[],
    )
    base.update(overrides)
    return Entry(**base)


def ev(text: str, evidence: str | None = None) -> Evidenced:
    return Evidenced(text=text, evidence=evidence or text)


SAMPLE_ENTRIES = [
    make_entry(
        "case_0001",
        post_title="선착순 쿠폰 발급 개선기",
        post_url="https://example.com/coupon",
        published_at="2025-03-12",
        problem_types=["동시성·락", "트래픽 급증 대응"],
        technologies=["Redis", "Spring Boot"],
        technologies_raw=["Redis", "Spring Boot"],
        tags=["선착순", "쿠폰"],
        problem_situation=[ev("선착순 쿠폰 발급 시 한도 초과 발급 발생 (초당 최대 3만 요청)")],
        solution=[ev("Redis 원자 연산으로 발급 수량 관리")],
        performance_ops=[ev("초과 발급이 0건으로 줄었다")],
        rejected_alternatives=[
            RejectedAlternative(
                name="DB 비관적 락",
                name_raw="DB 비관적 락",
                kind="설계 방식",
                reason="처리량 부족",
                evidence="락 대기",
            )
        ],
    ),
    make_entry(
        "case_0002",
        post_title="선착순 쿠폰 발급 개선기",
        post_url="https://example.com/coupon",
        published_at="2025-03-12",
        problem_types=["메시징·비동기 처리"],
        technologies=["RabbitMQ"],
        technologies_raw=["RabbitMQ"],
        problem_situation=[ev("대량 쿠폰 발급 요청을 동기로 처리해 지연 발생")],
        solution=[ev("RabbitMQ로 비동기 발급 처리")],
        rejected_alternatives=[
            RejectedAlternative(
                name="Kafka",
                name_raw="카프카",
                kind="기술",
                reason="파티션 축소 불가",
                evidence="파티션",
            )
        ],
    ),
    make_entry(
        "case_0003",
        company="토스",
        source="toss",
        post_title="Kafka 기반 정산 파이프라인",
        post_url="https://example.com/settle",
        published_at="2024-06-01",
        problem_types=["데이터 파이프라인"],
        domains=["결제·금융"],
        technologies=["Kafka"],
        technologies_raw=["Kafka"],
        problem_situation=[ev("정산 데이터 적재 지연")],
        solution=[ev("Kafka 스트림으로 실시간 적재")],
    ),
    make_entry(
        "case_0004",
        post_title="AI 코딩 에이전트 활용 팁",
        post_url="https://example.com/ai",
        published_at="2026-05-02",
        problem_types=["개발 생산성"],
        # 두 영역에 걸치는 항목
        domains=["사내 플랫폼·개발 도구", "LLM·AI"],
        technologies=["Claude Code"],
        technologies_raw=["Claude Code"],
        # 팁·활용 경험 항목: 문제 상황과 성능·운영 포인트가 없다
        solution=[
            ev("AI 코딩 에이전트의 완료 조건을 측정 가능하게 설계"),
            ev("테스트를 완료 조건으로 삼는다"),
        ],
    ),
]


@pytest.fixture(autouse=True)
def no_voyage_key(monkeypatch):
    # 개발자 환경에 키가 있어도 테스트가 실제 API를 부르지 않게 한다
    # (의미 검색은 가짜 클라이언트로 시험)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def fresh_breaker(monkeypatch):
    # 대체 경로를 시험하는 테스트가 쌓은 API 실패가 다른 테스트의 의미 검색을 막지 않게 한다
    from sonbae.search import semantic

    monkeypatch.setattr(semantic, "_breaker", semantic._Breaker())


@pytest.fixture(scope="session")
def sample_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "techblog.sqlite"
    build(SAMPLE_ENTRIES, path)
    return path


class FakeClient:
    """URL별로 정해 둔 응답을 돌려주는 PoliteClient 대역. 요청한 URL을 기록한다."""

    def __init__(self, responses: dict[str, bytes | httpx.Response | Exception]):
        self.responses = responses
        self.requested: list[str] = []

    def get(self, url: str) -> httpx.Response:
        self.requested.append(url)
        body = self.responses[url]
        if isinstance(body, Exception):
            raise body
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, content=body, request=httpx.Request("GET", url))


@pytest.fixture
def fake_client():
    return FakeClient
