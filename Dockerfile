# Single-stage: Python + Node.js in one image (avoids symlink breakage from multi-stage copy)
FROM python:3.11-slim

# Install Node.js 20 via NodeSource + MCP server packages
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g \
         servicenow-mcp-server \
         @modelcontextprotocol/server-github \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

# Run as non-root
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Default: API server. Worker overrides via ACA container command.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
