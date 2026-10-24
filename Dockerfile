FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY hris ./hris
COPY ethena ./ethena
COPY hrsync ./hrsync
COPY reconcile ./reconcile
COPY mcp_server ./mcp_server
COPY webapp ./webapp
RUN pip install --no-cache-dir .
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x entrypoint.sh && mkdir -p /app/data
ENV HRIS_DB=/app/data/hris.db ETHENA_DB=/app/data/ethena.db SYNC_DB=/app/data/sync.db \
    RULES_PATH=/app/hrsync/rules.default.json WEB_HOST=0.0.0.0 WEB_PORT=8003
EXPOSE 8003
ENTRYPOINT ["/app/entrypoint.sh"]
