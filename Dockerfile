# agentic-reporting: one image for the CLI, the CronJob and the approver pod.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first (better layer cache), then the code.
COPY pyproject.toml README.md LICENSE ./
COPY agentic_reporting ./agentic_reporting
RUN pip install ".[llm]"

COPY specs ./specs
COPY samples ./samples
RUN python samples/make_sales_db.py && python samples/make_ops_db.py

# Non-root. The run state and reports live on volumes owned by this user.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin reporter \
    && mkdir -p /app/reports /app/.agentic_reporting \
    && chown -R reporter:reporter /app/reports /app/.agentic_reporting
USER 10001

ENV REPORT_CHECKPOINT_DB=/app/.agentic_reporting/checkpoints.sqlite \
    REPORT_APPROVER=container-user

ENTRYPOINT ["python", "-m", "agentic_reporting.run"]
CMD ["--help"]
