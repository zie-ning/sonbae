# 원격 MCP 서버(M9) 이미지. Fly.io가 이 파일로 빌드한다 (fly.toml)
# 서버 의존성만 설치하고(dev·pipeline 그룹 제외), 검색 DB는 기동 때 GitHub Release에서 받는다

FROM python:3.13-slim

# uv는 저장소와 같은 버전을 쓴다 (uv.lock을 만든 버전)
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 의존성 층을 먼저 만들어 코드만 바뀌면 다시 설치하지 않는다.
# uv 다운로드 캐시는 빌드 캐시에만 두고 이미지에는 넣지 않는다(넣으면 의존성 층이 약 3배로 커진다)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv     uv sync --locked --no-default-groups --no-install-project

COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv     uv sync --locked --no-default-groups --no-editable

# 루트가 아닌 사용자로 실행한다. 검색 DB는 이 사용자의 캐시 폴더에 받는다
RUN useradd --create-home --uid 1000 sonbae
USER sonbae
ENV PATH="/app/.venv/bin:$PATH" \
    SONBAE_CACHE_DIR=/home/sonbae/.cache/sonbae \
    SONBAE_HOST=0.0.0.0 \
    PORT=8080

EXPOSE 8080
CMD ["sonbae-http"]
