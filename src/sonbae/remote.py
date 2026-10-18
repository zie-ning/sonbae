"""원격 MCP 서버 진입점: stdio 서버와 같은 도구를 Streamable HTTP로 제공한다 (M9).

도구·prompt는 `sonbae.server`의 것을 그대로 쓰고 전송 방식과 HTTP 계층만 다룬다. 결정 사항은
docs/기획.md "원격 HTTP 서버 (M9)".

- 상태 없는 세션(`stateless_http`): 세션이 특정 프로세스 메모리에 묶이지 않아 여러 대로 늘려도
  아무 머신이 요청을 받는다. 도구가 모두 읽기 전용이라 세션 상태가 필요 없다.
- JSON 응답(`json_response`): 도구가 진행 알림을 보내지 않아 SSE 스트림 대신 응답 하나로 돌려준다.
- 토큰 인증: 서버가 Voyage 키를 보관하므로, 환경 변수 `SONBAE_TOKEN`의 토큰을
  `Authorization: Bearer` 헤더로 확인해 없거나 틀리면 401로 거절한다. 토큰 없이 외부에 열려고 하면
  기동하지 않는다(로컬 주소에서만 토큰 없이 실행할 수 있다).
- 요청 제한: 인증을 통과한 MCP 요청을 토큰 버킷으로 세어(기본 분당 60회, 몰림 20회) 넘으면 429로
  거절한다. 토큰이 새거나 에이전트가 검색을 반복해도 Voyage 비용이 한없이 늘지 않게 하는
  안전장치다. 토큰 하나·서버 한 대라 카운터는 프로세스 메모리에 하나만 둔다.
- 헬스 체크 `GET /health`: 기동 준비(DB·항목 임베딩, 형태소 분석기)가 끝나면 200, 준비 중이거나
  실패했으면 503. Fly.io가 준비된 머신에만 요청을 보내는 데 쓴다. 플랫폼이 토큰 없이 부르므로
  인증·요청 제한에서 빼고, 응답에는 단계별 상태만 담는다.
- 구조화 로그: 요청마다 JSON 한 줄(`http_request`: 경로·상태 코드·소요 시간)을 남기고, 같은 요청의
  도구 호출 로그(`server.py`의 `tool_call`)와 `request_id`로 묶는다 (`sonbae.logs`).
"""

import hmac
import logging
import math
import os
import time
import uuid
from collections.abc import Callable

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from sonbae import logs, server

HOST_ENV = "SONBAE_HOST"
PORT_ENV = "PORT"
TOKEN_ENV = "SONBAE_TOKEN"
RATE_ENV = "SONBAE_RATE_PER_MINUTE"
BURST_ENV = "SONBAE_RATE_BURST"
# 로컬에서 실행하면 이 PC에서만 접속하게 한다. 컨테이너에서는 0.0.0.0으로 연다
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
MCP_PATH = "/mcp"
HEALTH_PATH = "/health"
# 에이전트 한 명의 검색(한 번에 2~5개, 분당 수십 회 이하)은 넉넉히 통과하고, 최악의 Voyage 비용은
# 검색당 약 $0.0015 × 분당 60회 = 시간당 약 $5.4로 묶인다
DEFAULT_RATE_PER_MINUTE = 60
DEFAULT_BURST = 20

log = logging.getLogger(__name__)


class RequestLog:
    """HTTP 요청마다 경로·상태 코드·소요 시간을 JSON 로그로 남기는 ASGI 미들웨어. 가장 바깥에 둔다.

    Fly.io 프록시가 붙이는 `Fly-Request-Id`가 있으면 요청 ID로 쓰고, 없으면 새로 만든다.
    플랫폼이 몇 초마다 부르는 헬스 체크는 정상(200)이면 남기지 않는다.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        rid = headers.get(b"fly-request-id", b"").decode() or uuid.uuid4().hex[:16]
        token = logs.request_id.set(rid)
        status = 500  # 응답을 시작하기 전에 예외가 나면 uvicorn이 500으로 답한다
        start = time.perf_counter()

        async def send_with_status(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            if not (scope["path"] == HEALTH_PATH and status == 200):
                log.info(
                    "http_request",
                    extra={
                        "fields": {
                            "event": "http_request",
                            "method": scope["method"],
                            "path": scope["path"],
                            "status": status,
                            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                        }
                    },
                )
            logs.request_id.reset(token)


class TokenBucket:
    """최대 `capacity`개가 차 있고 초당 `rate`개씩 다시 차는 양동이. 요청마다 하나씩 쓴다.

    매 분 0초에 초기화하는 고정 구간은 경계 앞뒤로 한도의 두 배가 통과해 쓰지 않는다.
    이벤트 루프 안에서 await 없이 불려 동시에 실행되지 않으므로 잠금이 필요 없다.
    """

    def __init__(
        self, rate: float, capacity: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.rate = rate
        self.capacity = capacity
        self.clock = clock
        self.tokens = float(capacity)
        self.updated = clock()

    def take(self) -> float:
        """하나를 쓸 수 있으면 쓰고 0을, 비었으면 다시 시도할 때까지의 초를 돌려준다."""
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate


class RateLimit:
    """MCP 경로 요청을 토큰 버킷으로 세어 넘으면 429와 `Retry-After`로 거절하는 ASGI 미들웨어."""

    def __init__(self, app: ASGIApp, bucket: TokenBucket) -> None:
        self.app = app
        self.bucket = bucket

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(MCP_PATH):
            wait = self.bucket.take()
            if wait:
                response = JSONResponse(
                    {
                        "error": "rate_limited",
                        "message": "요청이 너무 많습니다. 잠시 뒤 다시 시도하세요.",
                    },
                    status_code=429,
                    headers={"Retry-After": str(math.ceil(wait))},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class TokenAuth:
    """`Authorization: Bearer <토큰>`이 맞는 HTTP 요청만 통과시키는 ASGI 미들웨어.

    응답을 스트림으로 보낼 수 있게 BaseHTTPMiddleware가 아니라 ASGI로 직접 감싼다.
    lifespan 같은 HTTP가 아닌 이벤트는 그대로 넘긴다.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] != HEALTH_PATH and not self._authorized(scope):
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


@server.server.custom_route(HEALTH_PATH, methods=["GET"])
async def health(request: Request) -> JSONResponse:
    status, steps = server.readiness.status()
    return JSONResponse(
        {"status": status, "steps": steps}, status_code=200 if status == "ok" else 503
    )


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
    # 나중에 더한 미들웨어가 바깥에 놓인다. 요청 로그 → 인증 → 요청 제한 → MCP 순서로 거쳐
    # 틀린 토큰은 세지 않고, 401·429도 요청 로그에 남는다
    rate = float(os.environ.get(RATE_ENV, DEFAULT_RATE_PER_MINUTE)) / 60
    burst = int(os.environ.get(BURST_ENV, DEFAULT_BURST))
    starlette_app.add_middleware(RateLimit, bucket=TokenBucket(rate, burst))
    if token:
        starlette_app.add_middleware(TokenAuth, token=token)
    starlette_app.add_middleware(RequestLog)
    return starlette_app


def main() -> None:
    # 설정이 잘못됐으면 DB를 준비하기 전에 멈춘다
    starlette_app = app()
    logs.configure()  # 기동 준비 실패 경고도 JSON으로 남게 먼저 설정한다
    # stdio 서버와 같이 DB·형태소 분석기 준비는 백그라운드에서 한다
    server._prepare()
    # uvicorn 자체 로그 설정을 끄고 루트(JSON)로 모은다. 접근 로그는 RequestLog가 대신 남긴다
    uvicorn.run(
        starlette_app,
        host=_host(),
        port=int(os.environ.get(PORT_ENV, DEFAULT_PORT)),
        log_config=None,
        access_log=False,
    )
