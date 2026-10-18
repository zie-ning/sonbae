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
    for name in (remote.HOST_ENV, remote.PORT_ENV, remote.TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
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
