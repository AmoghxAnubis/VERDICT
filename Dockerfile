FROM ghcr.io/astral-sh/uv:0.11.2 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
COPY alembic.ini ./
COPY migrations ./migrations
COPY simulations ./simulations
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home verdict && mkdir -p /app/data && chown verdict /app/data
USER verdict
EXPOSE 8000
CMD ["verdict", "serve", "--host", "0.0.0.0"]
