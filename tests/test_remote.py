"""원격 서버(Streamable HTTP) 테스트. 실제 uvicorn을 띄우고 MCP 클라이언트로 URL에 연결한다."""

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from sonbae import db, remote, server


@pytest.fixture(autouse=True)
def use_sample_db(sample_db, monkeypatch):
    monkeypatch.setenv(db.DB_PATH_ENV, str(sample_db))
    # 개발 PC에 설정된 값이 테스트에 섞이지 않게 한다
    for name in (remote.HOST_ENV, remote.PORT_ENV, remote.TOKEN_ENV, remote.RATE_ENV):
        monkeypatch.delenv(name, raising=False)
    # 요청 제한 테스트가 아니면 한도에 걸리지 않게 몰림 허용을 크게 둔다
    monkeypatch.setenv(remote.BURST_ENV, "1000")
    server._database_path.cache_clear()
    yield
    server._database_path.cache_clear()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serving() -> Iterator[str]:
    port = free_port()
    http = uvicorn.Server(
        uvicorn.Config(remote.app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=http.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not http.started:
        assert time.monotonic() < deadline, "서버가 뜨지 않았다"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}{remote.MCP_PATH}"
    http.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def url() -> Iterator[str]:
    with serving() as u:
        yield u


def test_tools_over_http(url):
    async def run():
        async with Client(url) as client:
            tools = await client.list_tools()
            result = await client.call_tool("search", {"query": "선착순 쿠폰"})
            return {t.name for t in tools.tools}, result

    names, result = asyncio.run(run())
    assert names == {"search", "get_details", "aggregate"}
    assert not result.is_error
    # stdio와 같은 도구라 결과도 같다
    direct = asyncio.run(server.server.call_tool("search", {"query": "선착순 쿠폰"}))
    assert result.content[0].text == direct.content[0].text


def test_stateless_clients_run_concurrently(url):
    # 상태 없는 세션이라 클라이언트마다 세션을 따로 유지하지 않고, 동시에 연결해도 섞이지 않는다
    queries = ["선착순 쿠폰 동시성", "정산 배치"] * 4

    async def one(query: str) -> str:
        async with Client(url) as client:
            result = await client.call_tool("search", {"query": query})
            return result.content[0].text

    async def run():
        return await asyncio.gather(*(one(q) for q in queries))

    results = asyncio.run(run())
    assert results[0::2] == [results[0]] * 4
    assert results[1::2] == [results[1]] * 4
    assert results[0] != results[1]


def test_host_and_port_from_env(monkeypatch):
    calls = {}
    monkeypatch.setattr(remote.server, "_prepare", lambda: [])
    monkeypatch.setattr(remote.uvicorn, "run", lambda app, **kw: calls.update(kw))
    remote.main()
    assert calls == {"host": remote.DEFAULT_HOST, "port": remote.DEFAULT_PORT}

    monkeypatch.setenv(remote.HOST_ENV, "0.0.0.0")
    monkeypatch.setenv(remote.PORT_ENV, "8080")
    monkeypatch.setenv(remote.TOKEN_ENV, "secret")
    remote.main()
    assert calls == {"host": "0.0.0.0", "port": 8080}


# ---- 토큰 인증 ----

TOKEN = "test-token"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


@pytest.fixture
def secured_url(monkeypatch) -> Iterator[str]:
    monkeypatch.setenv(remote.TOKEN_ENV, TOKEN)
    with serving() as u:
        yield u


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": "Basic x"}],
    ids=["none", "wrong", "no-scheme", "basic"],
)
def test_rejects_missing_or_wrong_token(secured_url, headers):
    headers = {"Accept": "application/json, text/event-stream", **headers}
    response = httpx.post(secured_url, json=INITIALIZE, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_accepts_correct_token(secured_url):
    async def run():
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
        async with Client(streamable_http_client(secured_url, http_client=http)) as client:
            return await client.call_tool("search", {"query": "선착순 쿠폰"})

    result = asyncio.run(run())
    assert not result.is_error
    assert "선착순" in result.content[0].text


def test_refuses_public_host_without_token(monkeypatch):
    # 토큰 없이 외부에 열면 누구나 서버의 Voyage 키를 쓸 수 있어 기동하지 않는다
    prepared = []
    monkeypatch.setattr(remote.server, "_prepare", lambda: prepared.append(True))
    monkeypatch.setenv(remote.HOST_ENV, "0.0.0.0")
    with pytest.raises(SystemExit, match=remote.TOKEN_ENV):
        remote.main()
    assert not prepared


# ---- 요청 제한 ----


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_bucket_allows_burst_then_refills():
    clock = FakeClock()
    bucket = remote.TokenBucket(rate=1.0, capacity=3, clock=clock)
    assert [bucket.take() for _ in range(3)] == [0, 0, 0]
    assert bucket.take() == pytest.approx(1.0)  # 비었으면 하나가 찰 때까지 기다릴 시간
    clock.now += 0.5
    assert bucket.take() == pytest.approx(0.5)
    clock.now += 0.5
    assert bucket.take() == 0


def test_bucket_does_not_exceed_capacity_after_idle():
    clock = FakeClock()
    bucket = remote.TokenBucket(rate=1.0, capacity=3, clock=clock)
    clock.now += 3600  # 오래 쉬어도 몰림 허용 이상으로 쌓이지 않는다
    assert [bucket.take() for _ in range(4)][-1] > 0


def post(url: str, token: str | None = None) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.post(url, json=INITIALIZE, headers=headers)


def test_rejects_over_limit_with_retry_after(monkeypatch):
    monkeypatch.setenv(remote.BURST_ENV, "3")
    monkeypatch.setenv(remote.RATE_ENV, "6")  # 10초에 하나씩 찬다
    with serving() as url:
        codes = [post(url).status_code for _ in range(4)]
        limited = post(url)
    assert codes == [200, 200, 200, 429]
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["retry-after"]) <= 10


def test_wrong_token_is_not_counted(monkeypatch):
    # 인증이 먼저라, 틀린 토큰 요청이 쌓여도 맞는 토큰의 한도를 쓰지 않는다
    monkeypatch.setenv(remote.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(remote.BURST_ENV, "2")
    monkeypatch.setenv(remote.RATE_ENV, "1")
    with serving() as url:
        assert {post(url, "wrong").status_code for _ in range(5)} == {401}
        assert [post(url, TOKEN).status_code for _ in range(3)] == [200, 200, 429]


# ---- 헬스 체크 ----


def health(url: str) -> httpx.Response:
    return httpx.get(url.removesuffix(remote.MCP_PATH) + remote.HEALTH_PATH)


def test_health_is_starting_until_prepared(url):
    server.readiness.reset()
    response = health(url)
    assert response.status_code == 503
    assert response.json() == {
        "status": "starting",
        "steps": {"db": "starting", "analyzer": "starting"},
    }

    for thread in server._prepare():
        thread.join()
    response = health(url)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "steps": {"db": "ok", "analyzer": "ok"}}


def test_health_reports_failed_step_without_details(tmp_path, monkeypatch, url):
    # 실패 이유(경로 등)는 로그에만 남기고 응답에는 상태만 담는다
    missing = tmp_path / "none.sqlite"
    monkeypatch.setenv(db.DB_PATH_ENV, str(missing))
    server._database_path.cache_clear()
    for thread in server._prepare():
        thread.join()
    response = health(url)
    assert response.status_code == 503
    assert response.json() == {"status": "error", "steps": {"db": "error", "analyzer": "ok"}}
    assert str(tmp_path) not in response.text


def test_health_needs_no_token_and_is_not_rate_limited(monkeypatch):
    # 플랫폼이 토큰 없이 자주 부르므로 인증·요청 제한에서 뺀다
    monkeypatch.setenv(remote.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(remote.BURST_ENV, "1")
    monkeypatch.setenv(remote.RATE_ENV, "1")
    with serving() as url:
        assert {health(url).status_code for _ in range(5)} <= {200, 503}
        assert post(url).status_code == 401  # MCP 경로는 그대로 토큰이 필요하다
        assert [post(url, TOKEN).status_code for _ in range(2)] == [200, 429]
