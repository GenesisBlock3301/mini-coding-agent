FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    RUNS_DIR=/state/journals EVAL_WORKSPACES_DIR=/state/workspaces \
    MINI_AGENT_MEMORY_DIR=/state/memory WORKSPACE_DIR=/workspace
WORKDIR /opt/mini-agent
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 agent \
    && mkdir -p /state /workspace && chown agent:agent /state /workspace
COPY agent ./agent
COPY scripts ./scripts
COPY fixtures ./fixtures
COPY evals ./evals
COPY tests ./tests
COPY pytest.ini README.md ./
RUN chown -R agent:agent /opt/mini-agent
USER agent
ENTRYPOINT ["python", "-m", "agent.cli"]
