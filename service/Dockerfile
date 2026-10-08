FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Injected by deploy-trader workflow from the repository single source of truth.
COPY strategy_codes.json /app/strategy_codes.json
# Git commit of this build (deploy-trader passes --build-arg RUNNER_VERSION=<sha>); exposed in /status.
ARG RUNNER_VERSION=unknown
ENV RUNNER_VERSION=${RUNNER_VERSION}
# Default: Cloud Run Job
ENTRYPOINT ["python", "-u"]
CMD ["main.py", "run"]
