# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python package management tools
RUN pip install --no-cache-dir uv

# Copy dependency definitions
COPY pyproject.toml uv.lock* ./

# Install project dependencies
ENV UV_SYSTEM_PYTHON=1
RUN uv pip install --system -r pyproject.toml

# Copy the rest of the application code
COPY . .

# Install the package itself
RUN uv pip install --system --no-deps -e .

# Default command to run the MCP server
ENTRYPOINT ["python", "-m", "pbi_mcp_server.main"]
