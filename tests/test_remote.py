"""원격 서버(Streamable HTTP) 테스트. 실제 uvicorn을 띄우고 MCP 클라이언트로 URL에 연결한다."""

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from mcp import Client

from sonbae import db, remote, server


@pytest.fixture(autouse=True)
def use_sample_db(sample_db, monkeypatch):
    monkeypatch.setenv(db.DB_PATH_ENV, str(sample_db))
    server._database_path.cache_clear()
    yield
    server._database_path.cache_clear()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def url() -> Iterator[str]:
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
    remote.main()
    assert calls == {"host": "0.0.0.0", "port": 8080}
