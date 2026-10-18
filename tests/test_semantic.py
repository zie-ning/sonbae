"""의미 검색(M10) 테스트: 가짜 임베딩·리랭커 클라이언트로 2단 판정, 필터, 대체 경로를 본다."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import numpy as np
import pytest
from conftest import SAMPLE_ENTRIES

from pipeline.build_index import build
from sonbae import db, render
from sonbae.search import query as q
from sonbae.search import semantic

# 항목 임베딩: 0001·0002는 쿠폰 쪽, 0003은 정산, 0004는 AI
VECTORS = {
    "case_0001": [1.0, 0.0, 0.0, 0.0],
    "case_0002": [0.8, 0.6, 0.0, 0.0],
    "case_0003": [0.0, 1.0, 0.0, 0.0],
    "case_0004": [0.0, 0.0, 1.0, 0.0],
}


class FakeClient:
    """검색어 임베딩은 고정 벡터, 리랭커 점수는 항목 제목·해결 방법의 단어로 정한다."""

    def __init__(self, scores: dict[str, float], query_vector=(1.0, 0.0, 0.0, 0.0)):
        self.scores = scores
        self.query_vector = np.asarray(query_vector, dtype=np.float32)
        self.reranked: list[list[str]] = []

    def embed_query(self, text: str, timeout: float | None = None) -> np.ndarray:
        return semantic.normalize(self.query_vector)

    def rerank(self, query: str, documents: list[str], timeout: float | None = None) -> list[float]:
        self.reranked.append(documents)
        return [next((v for k, v in self.scores.items() if k in doc), 0.1) for doc in documents]


class BrokenClient(FakeClient):
    def rerank(self, query: str, documents: list[str], timeout: float | None = None) -> list[float]:
        raise semantic.SemanticUnavailable("Voyage API가 오류를 돌려줬습니다(HTTP 500)")


@pytest.fixture(scope="module")
def vector_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("semantic") / "techblog.sqlite"
    build(SAMPLE_ENTRIES, path, embeddings={k: np.array(v) for k, v in VECTORS.items()})
    return path


@pytest.fixture
def vconn(vector_db):
    conn = db.connect(vector_db)
    yield conn
    conn.close()


@pytest.fixture
def conn(sample_db):
    conn = db.connect(sample_db)
    yield conn
    conn.close()


def ids(result: q.SearchResult) -> list[str]:
    return [e["id"] for e in result.entries]


def test_entry_text_is_title_summary_and_technologies():
    text = semantic.entry_text(SAMPLE_ENTRIES[0].model_dump())
    assert text.splitlines() == [
        "선착순 쿠폰 발급 개선기",
        "문제: 선착순 쿠폰 발급 시 한도 초과 발급 발생 (초당 최대 3만 요청)",
        "해결: Redis 원자 연산으로 발급 수량 관리",
        "성능·운영: 초과 발급이 0건으로 줄었다",
        "기술: Redis, Spring Boot",
    ]


def test_results_ordered_by_reranker_above_item_threshold(vconn):
    client = FakeClient({"원자 연산": 0.9, "RabbitMQ": 0.7, "Kafka": 0.4})
    result = q.search(vconn, "선착순 쿠폰 초과 발급", q.Filters(), client)
    assert result.semantic and result.fallback is None
    # 0.50 미만(Kafka 정산 0.4, AI 0.1)은 보여 주지 않는다
    assert ids(result) == ["case_0001", "case_0002"]
    assert result.total == 2


def test_no_result_when_top_score_below_gate(vconn):
    # 1위가 0.6이면 기준선(0.654) 미만이라 결과 없음. ITEM_MIN(0.50)을 넘는 항목이 있어도 마찬가지
    client = FakeClient({"원자 연산": 0.6, "RabbitMQ": 0.55})
    result = q.search(vconn, "블록체인 가스비", q.Filters(), client)
    assert result.semantic
    assert result.entries == [] and result.total == 0


def test_gate_and_item_thresholds_are_inclusive(vconn):
    client = FakeClient({"원자 연산": semantic.GATE, "RabbitMQ": semantic.ITEM_MIN})
    result = q.search(vconn, "쿠폰", q.Filters(), client)
    assert ids(result) == ["case_0001", "case_0002"]


def test_filters_limit_candidates(vconn):
    client = FakeClient({"Kafka": 0.9, "원자 연산": 0.95})
    result = q.search(vconn, "실시간 적재", q.Filters(domain="결제·금융"), client)
    assert ids(result) == ["case_0003"]
    # 필터 밖의 항목은 리랭커에 보내지도 않는다
    assert all("Kafka" in doc for doc in client.reranked[0])


def test_semantic_finds_entries_without_shared_words(vconn):
    # 검색어 단어가 하나도 겹치지 않아도 임베딩 후보로 올라 리랭커가 판정한다
    client = FakeClient({"원자 연산": 0.8})
    result = q.search(vconn, "한정 수량 혜택 중복 지급", q.Filters(), client)
    assert ids(result) == ["case_0001"]


def test_falls_back_to_lexical_without_key(vconn, monkeypatch):
    monkeypatch.delenv(semantic.API_KEY_ENV, raising=False)
    result = q.search(vconn, "선착순 쿠폰 동시성", q.Filters())
    assert not result.semantic
    assert result.fallback == "VOYAGE_API_KEY가 설정되지 않았습니다"
    assert ids(result)[0] == "case_0001"


def test_falls_back_when_api_fails(vconn):
    result = q.search(vconn, "선착순 쿠폰 동시성", q.Filters(), BrokenClient({}))
    assert not result.semantic
    assert "HTTP 500" in result.fallback
    assert ids(result)[0] == "case_0001"


def test_falls_back_when_db_has_no_embeddings(conn):
    result = q.search(conn, "선착순 쿠폰 동시성", q.Filters(), FakeClient({"원자 연산": 0.9}))
    assert not result.semantic
    assert result.fallback == "검색 DB에 항목 임베딩이 없습니다"


def test_render_mentions_fallback_only_when_it_happened(vconn):
    semantic_text = render.search_result(
        "쿠폰", q.Filters(), q.search(vconn, "쿠폰", q.Filters(), FakeClient({"원자": 0.9})), []
    )
    assert "단어 일치 검색" not in semantic_text
    fallback = q.search(vconn, "쿠폰", q.Filters(), BrokenClient({}))
    fallback_text = render.search_result("쿠폰", q.Filters(), fallback, [])
    assert "단어 일치 검색으로 찾았습니다" in fallback_text


def mock_client(handler) -> semantic.VoyageClient:
    return semantic.VoyageClient("key", transport=httpx.MockTransport(handler))


def test_voyage_client_errors_become_unavailable():
    def refused(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(semantic.SemanticUnavailable, match="연결하지 못했습니다"):
        mock_client(refused).embed_query("쿠폰")

    with pytest.raises(semantic.SemanticUnavailable, match="HTTP 401"):
        mock_client(lambda request: httpx.Response(401)).rerank("쿠폰", ["문서"])


def test_voyage_client_parses_responses():
    replies = {
        "embeddings": {"data": [{"index": 0, "embedding": [3.0, 4.0]}]},
        "rerank": {
            "data": [{"index": 1, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.2}]
        },
    }
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=replies[request.url.path.rsplit("/", 1)[1]])

    client = mock_client(handler)
    assert np.allclose(client.embed_query("쿠폰"), [0.6, 0.8])
    assert client.rerank("쿠폰", ["a", "b"]) == [0.2, 0.8]
    assert [str(r.url) for r in requests] == [
        f"{semantic.API_URL}/embeddings",
        f"{semantic.API_URL}/rerank",
    ]
    assert all(r.headers["Authorization"] == "Bearer key" for r in requests)


def test_voyage_client_passes_call_timeout():
    # 검색이 넘긴 한도는 이번 요청에만 쓰고, 주지 않으면 클라이언트 기본값을 쓴다
    timeouts: list[float] = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"data": [{"index": 0, "relevance_score": 0.5}]})

    client = mock_client(handler)
    client.rerank("쿠폰", ["a"], timeout=1.5)
    client.rerank("쿠폰", ["a"])
    assert timeouts == [1.5, semantic.TIMEOUT]


def test_call_gets_time_left_in_search():
    seen: list[float] = []
    semantic._within("임베딩", seen.append, time.monotonic() + 2.0)
    assert 1.5 < seen[0] <= 2.0


def test_client_from_env_reuses_one_client_per_key(monkeypatch):
    # 검색마다 클라이언트를 새로 만들면 SSL 설정에 호출당 약 0.4초가 든다(이슈 #19)
    monkeypatch.setattr(semantic, "_clients", {})
    assert semantic.client_from_env() is None
    monkeypatch.setenv(semantic.API_KEY_ENV, "key-a")
    first = semantic.client_from_env()
    assert first is not None
    assert semantic.client_from_env() is first
    monkeypatch.setenv(semantic.API_KEY_ENV, "key-b")
    assert semantic.client_from_env() is not first


class SlowClient(FakeClient):
    def __init__(self, scores: dict[str, float], delay: float):
        super().__init__(scores)
        self.delay = delay

    def rerank(self, query: str, documents: list[str], timeout: float | None = None) -> list[float]:
        time.sleep(self.delay)
        return super().rerank(query, documents, timeout)


def test_falls_back_when_api_exceeds_search_deadline(vconn, monkeypatch):
    # httpx 한도는 단계별이라 합계가 길어질 수 있어, 검색 한 번 전체에 한도를 둔다
    monkeypatch.setattr(semantic, "DEADLINE", 0.2)
    start = time.monotonic()
    result = q.search(vconn, "선착순 쿠폰 동시성", q.Filters(), SlowClient({"원자 연산": 0.9}, 1.0))
    assert time.monotonic() - start < 0.9  # 느린 호출이 끝날 때까지 기다리지 않는다
    assert not result.semantic
    assert result.fallback == "Voyage API 응답이 0.2초 안에 오지 않았습니다"
    assert ids(result)[0] == "case_0001"


def test_abandoned_queued_call_is_cancelled(monkeypatch):
    # 일꾼을 못 받은 채 버린 호출은 나중에도 실행되지 않는다(이슈 #25)
    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(semantic, "_executor", pool)
    release = threading.Event()
    pool.submit(release.wait)  # 하나뿐인 일꾼을 막아 둔다
    ran = []
    with pytest.raises(semantic.SemanticUnavailable):
        semantic._within("임베딩", lambda t: ran.append(True), time.monotonic() + 0.05)
    release.set()
    pool.shutdown(wait=True)
    assert ran == []


def test_breaker_skips_api_after_repeated_failures(vconn, monkeypatch):
    # 연속 실패 뒤에는 Voyage를 부르지 않고 바로 단어 일치 검색으로 간다(이슈 #25)
    monkeypatch.setattr(semantic, "BREAKER_FAILURES", 2)
    broken = BrokenClient({})
    for _ in range(2):
        assert q.search(vconn, "쿠폰", q.Filters(), broken).fallback.endswith("(HTTP 500)")
    healthy = FakeClient({"원자 연산": 0.9})
    result = q.search(vconn, "쿠폰", q.Filters(), healthy)
    assert not result.semantic
    assert result.fallback == "Voyage API 호출이 잇달아 실패해 잠시 쓰지 않습니다"
    assert healthy.reranked == []


def test_breaker_probes_once_after_cooldown(vconn, monkeypatch):
    monkeypatch.setattr(semantic, "BREAKER_FAILURES", 1)
    monkeypatch.setattr(semantic, "BREAKER_COOLDOWN", 0.05)
    q.search(vconn, "쿠폰", q.Filters(), BrokenClient({}))
    time.sleep(0.06)
    # 차단 시간이 지나면 한 번 시험하고, 실패하면 다시 막는다
    assert q.search(vconn, "쿠폰", q.Filters(), BrokenClient({})).fallback.endswith("(HTTP 500)")
    assert q.search(vconn, "쿠폰", q.Filters(), FakeClient({})).fallback.startswith(
        "Voyage API 호출이"
    )
    time.sleep(0.06)
    # 시험이 성공하면 풀린다
    assert q.search(vconn, "쿠폰", q.Filters(), FakeClient({"원자 연산": 0.9})).semantic
    assert q.search(vconn, "쿠폰", q.Filters(), FakeClient({"원자 연산": 0.9})).semantic


def test_breaker_blocks_other_searches_while_probing(monkeypatch):
    monkeypatch.setattr(semantic, "BREAKER_FAILURES", 1)
    monkeypatch.setattr(semantic, "BREAKER_COOLDOWN", 0.0)
    breaker = semantic._Breaker()
    breaker.failed()
    assert breaker.allow()  # 시험 하나
    assert not breaker.allow()  # 시험이 끝나기 전 다른 검색
    breaker.succeeded()
    assert breaker.allow()


def test_slow_call_is_logged(vconn, monkeypatch, caplog):
    monkeypatch.setattr(semantic, "SLOW_CALL", 0.05)
    with caplog.at_level(logging.WARNING, logger=semantic.__name__):
        result = q.search(vconn, "쿠폰", q.Filters(), SlowClient({"원자 연산": 0.9}, 0.1))
    assert result.semantic
    assert "느린 Voyage 리랭커 호출" in caplog.text
