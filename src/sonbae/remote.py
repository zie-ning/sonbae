"""원격 MCP 서버 진입점: stdio 서버와 같은 도구를 Streamable HTTP로 제공한다 (M9).

도구·prompt는 `sonbae.server`의 것을 그대로 쓰고 전송 방식만 다르다. 결정 사항은
docs/기획.md "원격 HTTP 서버 (M9)".

- 상태 없는 세션(`stateless_http`): 세션이 특정 프로세스 메모리에 묶이지 않아 여러 대로 늘려도
  아무 머신이 요청을 받는다. 도구가 모두 읽기 전용이라 세션 상태가 필요 없다.
- JSON 응답(`json_response`): 도구가 진행 알림을 보내지 않아 SSE 스트림 대신 응답 하나로 돌려준다.
"""

import os

import uvicorn
from starlette.applications import Starlette

from sonbae import server

HOST_ENV = "SONBAE_HOST"
PORT_ENV = "PORT"
# 로컬에서 실행하면 이 PC에서만 접속하게 한다. 컨테이너에서는 0.0.0.0으로 연다
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MCP_PATH = "/mcp"


def app() -> Starlette:
    # host는 SDK가 localhost일 때 DNS 리바인딩 보호를 켜는 데 쓴다
    return server.server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        host=os.environ.get(HOST_ENV, DEFAULT_HOST),
    )


def main() -> None:
    # stdio 서버와 같이 DB·형태소 분석기 준비는 백그라운드에서 한다
    server._prepare()
    uvicorn.run(
        app(),
        host=os.environ.get(HOST_ENV, DEFAULT_HOST),
        port=int(os.environ.get(PORT_ENV, DEFAULT_PORT)),
    )
