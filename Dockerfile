FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MINI_AGENT_STATE_DIR=/state WORKSPACE_DIR=/workspace
WORKDIR /opt/mini-agent
COPY pyproject.toml README.md ./
COPY agent ./agent
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 agent \
    && mkdir -p /state /workspace && chown agent:agent /state /workspace \
    && rm -rf build mini_coding_agent.egg-info
USER agent
ENTRYPOINT ["mini-agent"]
