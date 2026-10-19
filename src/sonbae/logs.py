"""원격 서버(M9)의 구조화 로그: 한 줄에 JSON 객체 하나를 표준 출력으로 쓴다.

Fly.io는 표준 출력을 그대로 수집해 `fly logs`와 대시보드에서 보여 준다. 줄마다 JSON이면 필드로
거르고 셀 수 있다. 같은 HTTP 요청에서 나온 로그(요청 로그, 도구 호출 로그, Voyage 경고 등)는
`request_id`로 묶는다.

로그에 남길 값은 `logger.info(메시지, extra={"fields": {...}})`로 넘긴다. stdio 서버는 이
설정을 쓰지 않는다(표준 출력이 MCP 통신 통로라 로그를 쓰면 안 된다).
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

# 요청 로그 미들웨어가 정하고, 같은 요청 안의 로그가 함께 쓴다. 동기 도구는 작업 스레드에서 돌지만
# anyio가 contextvar를 스레드로 넘겨 준다
request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if rid := request_id.get():
            data["request_id"] = rid
        data.update(getattr(record, "fields", {}))
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def configure(level: int = logging.INFO) -> None:
    """루트 로거가 JSON 한 줄씩 표준 출력에 쓰게 한다. uvicorn 로그도 루트로 모인다."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx는 Voyage 호출마다 INFO 로그를 남긴다. 검색 시간은 tool_call에, 느리거나 한도를 넘긴
    # 호출은 semantic의 경고에 이미 있어 경고 이상만 남긴다
    logging.getLogger("httpx").setLevel(logging.WARNING)
