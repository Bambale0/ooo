# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --prefix=/install .

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install --no-cache-dir --prefix=/install --no-deps .

# Stage 2: Runtime
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    PYTHONPATH=/app

WORKDIR /app

RUN groupadd -r neironych && \
    useradd -r -g neironych -d /app -s /sbin/nologin neironych && \
    apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY --from=builder /build/alembic ./alembic
COPY --from=builder /build/alembic.ini ./

RUN mkdir -p /app/var/media && \
    chown -R neironych:neironych /app

USER neironych

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/health/readiness || exit 1

ENTRYPOINT ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--log-level", "info", "--no-access-log"]

