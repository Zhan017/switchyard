FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir uv==0.11.32

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY chaos ./chaos
COPY config ./config
# Recorded streams that mockllm's replay fault plays back.
COPY tests/fixtures/streams ./tests/fixtures/streams
RUN uv sync --locked --no-dev

RUN useradd --create-home --uid 10001 app
USER app
ENV PATH="/app/.venv/bin:$PATH" \
    SWITCHYARD_CONFIG=/app/config/switchyard.yaml

EXPOSE 8000
CMD ["uvicorn", "--factory", "switchyard.api.app:app_from_env", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
