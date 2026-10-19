"""의미 검색 (M10): BM25 + 임베딩 후보를 리랭커로 정렬하고 2단 판정으로 거른다.

1. 검색어 임베딩(Voyage voyage-4-large)과 항목 임베딩의 코사인 상위 50건, BM25 상위 50건을
   RRF로 합쳐 후보 50건을 만든다
2. Voyage rerank-3로 후보마다 "검색어에 답이 되는 정도"를 0~1 점수로 매긴다
3. 1위 점수가 GATE 이상이면 결과 있음, 그때 점수가 ITEM_MIN 이상인 항목을 최대 10건 돌려준다

설정값과 근거는 docs/기획.md "검색", 실험은 docs/milestones/M10.md(eval/search_gate.py).
API 키(VOYAGE_API_KEY)가 없거나 호출이 실패하면 SemanticUnavailable을 던지고,
호출한 쪽(query.search)이 단어 일치 검색으로 대체한다.
"""

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Protocol, TypeVar

import httpx
import numpy as np

API_KEY_ENV = "VOYAGE_API_KEY"
API_URL = "https://api.voyageai.com/v1"
EMBEDDING_MODEL = "voyage-4-large"
RERANK_MODEL = "rerank-3"
CANDIDATES = 50  # 리랭커에 넘기는 후보 수 (BM25·임베딩 각 상위 50건을 RRF로 합친 상위)
RRF_K = 60
# 2단 판정 기준선 (M10 4단계 사용자 결정). 1위 점수가 GATE 미만이면 "결과 없음"이다.
# GATE는 DB에 없는 주제에서 1위로 올라오는 "비슷하지만 다른 글"을 거르는 값이라 높고,
# ITEM_MIN은 결과가 있을 때 무관한 꼬리만 자르는 값이라 낮다 (정답 항목의 48%가 GATE 미만)
GATE = 0.654
ITEM_MIN = 0.50
# 초. 검색 한 번의 API 호출(검색어 임베딩 + 리랭커)이 모두 이 안에 끝나지 않으면 단어 일치
# 검색으로 대체한다. 정상 응답은 합쳐 1~2초인데, 호출 하나가 33초 걸린 적이 있다(M10 R5).
# httpx의 timeout은 연결·읽기 단계별 한도라 합계를 막지 못해 전체 한도를 따로 둔다
DEADLINE = 8.0
# 호출 하나의 httpx 단계별 한도 기본값. 검색은 호출마다 남은 시간을 한도로 넘긴다(_within)
TIMEOUT = DEADLINE
SLOW_CALL = 2.0  # 초. 이보다 오래 걸린 호출은 단계와 시간을 경고 로그로 남긴다
# 검색이 연속 BREAKER_FAILURES번 API 실패로 대체되면 BREAKER_COOLDOWN초 동안 Voyage를 부르지 않고
# 바로 단어 일치 검색으로 간다. 어차피 대체될 검색이 한도까지 기다리지 않게 한다(이슈 #25)
BREAKER_FAILURES = 3
BREAKER_COOLDOWN = 30.0

log = logging.getLogger(__name__)
# Voyage 호출 일꾼 수. 검색 하나는 임베딩·리랭커를 차례로 불러 한 번에 일꾼 하나를 쓴다.
# 4개였을 때 원격 서버에서 동시 검색 16개가 일꾼 앞에 줄을 서 3초 걸렸고, 로컬(CPU 1개)에서
# 32개로 늘리자 동시 32개가 4.75초 → 1.36초로 줄었다(이슈 #27). 호출은 대부분 응답 대기라
# 스레드가 늘어도 CPU 부담이 작다. 한도를 넘겨 버린 호출이 남아 있어도 다음 검색을 막지 않는다
WORKERS_ENV = "SONBAE_VOYAGE_WORKERS"
DEFAULT_WORKERS = 32
_executor = ThreadPoolExecutor(
    max_workers=int(os.environ.get(WORKERS_ENV, DEFAULT_WORKERS)), thread_name_prefix="voyage"
)
T = TypeVar("T")


class SemanticUnavailable(Exception):
    """의미 검색을 쓸 수 없다. 메시지는 결과에 붙이는 대체 이유다."""


BAD_RESPONSE = "Voyage API 응답을 해석하지 못했습니다"


class Client(Protocol):
    """timeout: 이번 호출의 httpx 단계별 한도(초). None이면 클라이언트 기본값."""

    def embed_query(self, text: str, timeout: float | None = None) -> np.ndarray: ...
    def rerank(
        self, query: str, documents: list[str], timeout: float | None = None
    ) -> list[float]: ...


def entry_text(entry: dict) -> str:
    """항목 임베딩·리랭커 입력: 제목 + 요약 필드 + 기술 (M5·M10 실험과 같은 텍스트).

    발췌(evidence)는 요약과 뜻이 같아 넣지 않는다. 파이프라인(pipeline.embed)과 서버가 이 함수
    하나를 같이 써야 항목 임베딩과 리랭커가 같은 텍스트를 본다.
    """

    def texts(points: list[dict]) -> str:
        return " ".join(p["text"] for p in points)

    parts = [entry["post_title"]]
    if entry["problem_situation"]:
        parts.append(f"문제: {texts(entry['problem_situation'])}")
    parts.append(f"해결: {texts(entry['solution'])}")
    if entry["performance_ops"]:
        parts.append(f"성능·운영: {texts(entry['performance_ops'])}")
    if entry["technologies"]:
        parts.append(f"기술: {', '.join(entry['technologies'])}")
    return "\n".join(parts)


def normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


class VoyageClient:
    """Voyage API 클라이언트. HTTP 클라이언트 하나를 만들어 모든 호출에 재사용한다.

    httpx 클라이언트를 새로 만들 때마다 SSL 설정(인증서 묶음 파일 읽기)에 약 0.4초가 들어,
    호출마다 만들면 검색 한 번이 0.7~0.8초 느려진다(이슈 #19). httpx.Client는 여러 스레드가
    함께 써도 안전해, 검색 한도를 넘겨 버린 호출이 일꾼 스레드에 남아 있어도 된다.
    transport는 테스트에서 가짜 응답을 넣을 때 쓴다.
    """

    def __init__(
        self,
        api_key: str,
        timeout: float = TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ):
        self._http = httpx.Client(
            base_url=API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def _post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        # httpx에서 timeout=None은 한도 없음이라, 주지 않았으면 클라이언트 기본값을 쓴다
        limit = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
        try:
            response = self._http.post(path, json=body, timeout=limit)
        except httpx.HTTPError as e:
            reason = f"Voyage API에 연결하지 못했습니다({type(e).__name__})"
            raise SemanticUnavailable(reason) from e
        if response.status_code != 200:
            reason = f"Voyage API가 오류를 돌려줬습니다(HTTP {response.status_code})"
            raise SemanticUnavailable(reason)
        try:
            return response.json()
        except ValueError as e:
            raise SemanticUnavailable(BAD_RESPONSE) from e

    def embed(self, texts: list[str], input_type: str, timeout: float | None = None) -> np.ndarray:
        """input_type: 검색어는 query, 항목은 document. 길이 1로 맞춘 float32 벡터."""
        data = self._post(
            "embeddings",
            {"model": EMBEDDING_MODEL, "input": texts, "input_type": input_type},
            timeout,
        )
        try:
            rows = sorted(data["data"], key=lambda d: d["index"])
            return normalize(np.asarray([r["embedding"] for r in rows], dtype=np.float32))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise SemanticUnavailable(BAD_RESPONSE) from e

    def embed_query(self, text: str, timeout: float | None = None) -> np.ndarray:
        return self.embed([text], "query", timeout)[0]

    def rerank(self, query: str, documents: list[str], timeout: float | None = None) -> list[float]:
        body = {"model": RERANK_MODEL, "query": query, "documents": documents}
        data = self._post("rerank", body, timeout)
        scores = [0.0] * len(documents)
        try:
            for r in data["data"]:
                scores[r["index"]] = r["relevance_score"]
        except (KeyError, IndexError, TypeError) as e:
            raise SemanticUnavailable(BAD_RESPONSE) from e
        return scores


_clients: dict[str, VoyageClient] = {}
_clients_lock = threading.Lock()


def client_from_env() -> VoyageClient | None:
    """VOYAGE_API_KEY의 클라이언트. 검색마다 부르므로 키별로 하나만 만들어 계속 쓴다."""
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        return None
    with _clients_lock:
        if key not in _clients:
            _clients[key] = VoyageClient(key)
        return _clients[key]


@dataclass(frozen=True)
class EntryVectors:
    rowids: np.ndarray  # entries.rowid
    matrix: np.ndarray  # (항목 수, 차원) float32, 길이 1


_vectors: dict[tuple[str, str], EntryVectors | None] = {}
_vectors_lock = threading.Lock()


def load_vectors(conn: sqlite3.Connection) -> EntryVectors | None:
    """DB의 항목 임베딩. 임베딩이 없거나 모델이 다르면 None (의미 검색을 쓸 수 없음).

    DB 파일과 빌드 시각별로 한 번만 읽는다(항목 1,315개 × 1024차원 약 5MB).
    """
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    key = (path, meta.get("built_at", ""))
    with _vectors_lock:
        if key not in _vectors:
            _vectors[key] = _read_vectors(conn, meta)
        return _vectors[key]


def _read_vectors(conn: sqlite3.Connection, meta: dict[str, str]) -> EntryVectors | None:
    if meta.get("embedding_model") != EMBEDDING_MODEL:
        return None
    rows = conn.execute("SELECT rowid, vector FROM entry_embeddings ORDER BY rowid").fetchall()
    if not rows:
        return None
    rowids = np.array([r[0] for r in rows])
    matrix = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    return EntryVectors(rowids, matrix)


def _rrf(lists: list[list[int]]) -> list[int]:
    """순위 목록들을 RRF로 합친다. 점수가 같으면 먼저 나온 목록(BM25)의 순서를 따른다."""
    score: dict[int, float] = {}
    for ranked in lists:
        for rank, rowid in enumerate(ranked, start=1):
            score[rowid] = score.get(rowid, 0.0) + 1 / (RRF_K + rank)
    return sorted(score, key=lambda r: -score[r])


@dataclass
class Ranked:
    rowids: list[int]  # 판정을 넘은 항목, 리랭커 점수 순 (ITEM_MIN 이상 전부)
    top_score: float  # 1위 리랭커 점수 (결과 없음 판정 근거)


class _Breaker:
    """API 실패가 이어지면 잠시 호출을 막는다(circuit breaker).

    차단 시간이 지나면 검색 하나만 시험 삼아 보내고, 그 결과로 풀거나 다시 막는다.
    시험 중에 들어온 검색은 막는다. allow()가 돌려준 세대를 결과와 함께 받아, 막기 전에
    들어온 검색의 늦은 결과는 지금 상태를 바꾸지 않게 한다(막을 때마다 세대가 바뀐다).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._probing = False
        self._generation = 0

    def allow(self) -> int | None:
        """허용하면 세대, 막으면 None."""
        with self._lock:
            if self._failures < BREAKER_FAILURES:
                return self._generation
            if self._probing or time.monotonic() < self._open_until:
                return None
            self._probing = True
            return self._generation

    def succeeded(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if self._failures >= BREAKER_FAILURES:
                log.warning("Voyage API 호출이 다시 성공해 의미 검색을 재개한다")
            self._failures = 0
            self._probing = False

    def failed(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._failures += 1
            self._probing = False
            if self._failures >= BREAKER_FAILURES:
                self._open_until = time.monotonic() + BREAKER_COOLDOWN
                self._generation += 1
                log.warning(
                    "Voyage API 호출이 %d번 잇달아 실패해 %.0f초 동안 쓰지 않는다",
                    self._failures,
                    BREAKER_COOLDOWN,
                )


_breaker = _Breaker()


def _within(step: str, call: Callable[[float], T], deadline: float) -> T:
    """API 호출을 남은 시간 안에서만 기다린다. 넘으면 결과를 버리고 대체 경로로 보낸다.

    DB 조회는 호출한 스레드에 남기고 API 호출만 일꾼 스레드에서 돌린다. call은 httpx 한도로
    쓸 남은 시간(초)을 받는다.
    """

    def run() -> T:
        # 실행 중인 호출은 취소할 수 없어, 검색이 포기할 즈음 httpx도 끝나도록 남은 시간을 한도로
        # 준다(이슈 #25). 줄 선 시간을 빼야 하므로 일꾼에서 시작할 때 잰다
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SemanticUnavailable("검색 한도가 지나 호출하지 않았습니다")
        return call(remaining)

    start = time.monotonic()
    future = _executor.submit(run)
    try:
        result = future.result(timeout=max(0.0, deadline - start))
    except FutureTimeout as e:
        # 일꾼을 아직 못 받은 호출은 대기열에서 뺀다. 남겨 두면 끝난 검색을 위해 나중에 실행돼
        # 뒤이은 검색을 밀어낸다(이슈 #25). 이미 실행 중인 호출은 취소되지 않는다
        future.cancel()
        log.warning("Voyage %s 호출이 검색 한도 %.0f초를 넘겨 대체했다", step, DEADLINE)
        raise SemanticUnavailable(f"Voyage API 응답이 {DEADLINE:g}초 안에 오지 않았습니다") from e
    elapsed = time.monotonic() - start
    if elapsed > SLOW_CALL:
        log.warning("느린 Voyage %s 호출: %.2f초", step, elapsed)
    return result


def rank(
    query: str,
    vectors: EntryVectors,
    lexical: list[int],
    allowed: set[int] | None,
    texts_of: Callable[[list[int]], list[str]],
    client: Client,
) -> Ranked:
    """lexical: 필터를 건 BM25 상위 rowid. allowed: 필터에 맞는 rowid (필터가 없으면 None)."""
    generation = _breaker.allow()
    if generation is None:
        raise SemanticUnavailable("Voyage API 호출이 잇달아 실패해 잠시 쓰지 않습니다")
    try:
        ranked = _rank(query, vectors, lexical, allowed, texts_of, client)
    except Exception:
        # 예상 밖의 예외도 실패로 세야 시험 중 상태가 남아 계속 막히지 않는다
        _breaker.failed(generation)
        raise
    _breaker.succeeded(generation)
    return ranked


def _rank(
    query: str,
    vectors: EntryVectors,
    lexical: list[int],
    allowed: set[int] | None,
    texts_of: Callable[[list[int]], list[str]],
    client: Client,
) -> Ranked:
    deadline = time.monotonic() + DEADLINE
    scores = vectors.matrix @ _within("임베딩", lambda t: client.embed_query(query, t), deadline)
    order = np.argsort(-scores, kind="stable")
    semantic = []
    for n in order:
        rowid = int(vectors.rowids[n])
        if allowed is None or rowid in allowed:
            semantic.append(rowid)
            if len(semantic) == CANDIDATES:
                break
    candidates = _rrf([lexical[:CANDIDATES], semantic])[:CANDIDATES]
    if not candidates:
        return Ranked([], 0.0)
    documents = texts_of(candidates)
    relevance = _within("리랭커", lambda t: client.rerank(query, documents, t), deadline)
    ranked = sorted(zip(candidates, relevance, strict=True), key=lambda p: -p[1])
    top = ranked[0][1]
    if top < GATE:
        return Ranked([], top)
    return Ranked([rowid for rowid, score in ranked if score >= ITEM_MIN], top)
