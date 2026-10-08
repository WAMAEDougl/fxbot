FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=15
WORKDIR /app

COPY pyproject.toml README.md ./
COPY . .
RUN pip install ".[ibkr]" \
    && useradd --create-home trader \
    && mkdir -p /app/state /app/audit /app/data_lake \
    && chown -R trader /app/state /app/audit /app/data_lake
USER trader

# Runtime dirs are mounted as volumes (see docker-compose.yml).
CMD ["python", "-m", "scripts.run_backtest", "--help"]
