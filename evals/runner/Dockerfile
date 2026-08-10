# Eval-runner для задачи 3.4.
#
# Лёгкий самодостаточный контейнер: внутри только MCP SDK и наш код.
# Никаких qdrant-client / sentence-transformers — всю тяжёлую работу
# делают сервисы, в которые runner ходит по сети.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код runner'а. Датасеты и reports монтируются bind-volume'ом.
COPY run_eval.py    /app/run_eval.py
COPY mcp_client.py  /app/mcp_client.py
COPY predicates.py  /app/predicates.py
COPY metrics.py     /app/metrics.py
COPY report.py      /app/report.py

# endpoint по умолчанию — к platform-help по docker-DNS.
# scripts/eval.py с хоста переопределяет на localhost:8003.
CMD ["python", "/app/run_eval.py", \
     "--dataset", "/app/evals/datasets/platform_help.jsonl", \
     "--out", "/app/evals/reports", \
     "--endpoint", "http://mcp-platform-help:8003/sse"]
