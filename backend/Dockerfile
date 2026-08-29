# syntax=docker/dockerfile:1.8
FROM python:3.14.7-slim-bookworm AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

FROM python-base AS dependencies
COPY requirements/base.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt

FROM python-base AS runtime
ARG BUILD_SHA=development
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /nonexistent app
COPY --from=dependencies /opt/venv /opt/venv
COPY app /app/app
COPY migrations /app/migrations
COPY alembic.mysql.ini alembic.postgres.ini /app/
ENV PATH="/opt/venv/bin:$PATH" \
    ECOM_BUILD_SHA="$BUILD_SHA"
LABEL org.opencontainers.image.revision="$BUILD_SHA"
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
