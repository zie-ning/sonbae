"""원격 MCP 서버 진입점: stdio 서버와 같은 도구를 Streamable HTTP로 제공한다 (M9).

도구·prompt는 `sonbae.server`의 것을 그대로 쓰고 전송 방식과 HTTP 계층만 다룬다. 결정 사항은
docs/기획.md "원격 HTTP 서버 (M9)".

- 상태 없는 세션(`stateless_http`): 세션이 특정 프로세스 메모리에 묶이지 않아 여러 대로 늘려도
  아무 머신이 요청을 받는다. 도구가 모두 읽기 전용이라 세션 상태가 필요 없다.
- JSON 응답(`json_response`): 도구가 진행 알림을 보내지 않아 SSE 스트림 대신 응답 하나로 돌려준다.
- 토큰 인증: 서버가 Voyage 키를 보관하므로, 환경 변수 `SONBAE_TOKEN`의 토큰을
  `Authorization: Bearer` 헤더로 확인해 없거나 틀리면 401로 거절한다. 토큰 없이 외부에 열려고 하면
  기동하지 않는다(로컬 주소에서만 토큰 없이 실행할 수 있다).
"""

import hmac
import os

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from sonbae import server

HOST_ENV = "SONBAE_HOST"
PORT_ENV = "PORT"
TOKEN_ENV = "SONBAE_TOKEN"
# 로컬에서 실행하면 이 PC에서만 접속하게 한다. 컨테이너에서는 0.0.0.0으로 연다
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
MCP_PATH = "/mcp"


class TokenAuth:
    """`Authorization: Bearer <토큰>`이 맞는 HTTP 요청만 통과시키는 ASGI 미들웨어.

    응답을 스트림으로 보낼 수 있게 BaseHTTPMiddleware가 아니라 ASGI로 직접 감싼다.
    lifespan 같은 HTTP가 아닌 이벤트는 그대로 넘긴다.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self._authorized(scope):
            response = JSONResponse(
                {"error": "unauthorized", "message": "Authorization: Bearer 토큰이 필요합니다."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        header = dict(scope["headers"]).get(b"authorization", b"")
        # 비교 시간으로 토큰을 한 글자씩 알아내지 못하게 상수 시간 비교를 쓴다
        return hmac.compare_digest(header, self.expected)


def _host() -> str:
    return os.environ.get(HOST_ENV, DEFAULT_HOST)


def app() -> Starlette:
    host = _host()
    token = os.environ.get(TOKEN_ENV)
    if not token and host not in LOCAL_HOSTS:
        # 토큰 없이 외부에 열면 누구나 서버의 Voyage 키로 검색할 수 있다
        raise SystemExit(f"{TOKEN_ENV} 환경 변수 없이 {host}로 열 수 없습니다.")
    # host는 SDK가 localhost일 때 DNS 리바인딩 보호를 켜는 데 쓴다
    starlette_app = server.server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        host=host,
    )
    if token:
        starlette_app.add_middleware(TokenAuth, token=token)
    return starlette_app


def main() -> None:
    # 설정이 잘못됐으면 DB를 준비하기 전에 멈춘다
    starlette_app = app()
    # stdio 서버와 같이 DB·형태소 분석기 준비는 백그라운드에서 한다
    server._prepare()
    uvicorn.run(starlette_app, host=_host(), port=int(os.environ.get(PORT_ENV, DEFAULT_PORT)))
