# The MCP server of a read-only replica of a phdcode library (README.md in this folder).
# The dependencies: the pins of pyproject.toml without torch, torchvision, transformers and marker-pdf, which only
# the parse needs; the replica never parses.
FROM python:3.12-slim
RUN pip install --no-cache-dir python-dotenv asyncpg==0.31.0 pgvector==0.4.2 openai==1.109.1 requests pydantic \
    mcp==2.3.0 fastapi==0.141.1 jinja2==3.1.6 markdown-it-py==4.2.0 uvicorn==0.52.4
WORKDIR /app
COPY llm_engine llm_engine
ENV PHDCODE_DIR=/app LOG_DIR=/tmp/phdcode PYTHONUNBUFFERED=1
USER nobody
CMD ["python", "-m", "llm_engine.mcp_server"]
