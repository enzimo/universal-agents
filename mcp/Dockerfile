FROM python:3.13-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

# Install dependencies first for better caching
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt

COPY config_loader.py default_values.py id_generation.py ./
COPY custom_agents/ custom_agents/
COPY storage/__init__.py storage/backends.py storage/
COPY mcp_control/__init__.py mcp_control/sdk_compat.py mcp_control/
COPY mcp/skills_mcp.py .
# Seed a clean image from tracked project templates. The operator-owned
# `.skills/` directory is intentionally ignored by git and may be mounted over
# this path at runtime, so it must never be required as a Docker build input.
COPY custom_agents/skill_templates/ .skills/
RUN python -c "import skills_mcp"

# Standard MCP port if HTTP/SSE transport is used instead of stdio
EXPOSE 8000

# The default transport for MCP servers is stdio
CMD ["python", "skills_mcp.py"]
