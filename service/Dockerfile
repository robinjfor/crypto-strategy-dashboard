FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Injected by deploy-trader workflow from the repository single source of truth.
COPY strategy_codes.json /app/strategy_codes.json
# Default: Cloud Run Job
ENTRYPOINT ["python", "-u"]
CMD ["main.py", "run"]
