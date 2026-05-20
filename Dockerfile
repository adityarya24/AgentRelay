FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTRELAY_HOST=0.0.0.0 \
    AGENTRELAY_PORT=8899 \
    AGENTRELAY_DB_PATH=/data/relay.db

WORKDIR /app

RUN groupadd --system agentrelay \
    && useradd --system --gid agentrelay --home-dir /nonexistent --no-create-home agentrelay \
    && mkdir -p /data \
    && chown agentrelay:agentrelay /data

COPY pyproject.toml README.md LICENSE ./
COPY agentrelay ./agentrelay

RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir ".[all]"

USER agentrelay
VOLUME ["/data"]
EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8899/', timeout=3))['status'])"

CMD ["agentrelay", "start"]
