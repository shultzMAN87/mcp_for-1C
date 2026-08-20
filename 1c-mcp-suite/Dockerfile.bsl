FROM eclipse-temurin:17-jre-jammy

WORKDIR /app

# Зависимости Python для MCP-обёртки
RUN apt-get update && apt-get install -y python3 python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
COPY requirements-bsl.txt requirements-bsl.lock.txt ./
# TR-2: ставим из лок-файла, если он заполнен, иначе — по границам.
# Пустой лок-файл не должен ломать сборку: он всего лишь означает, что
# лок ещё не собирали (`make lock`, на Windows — `.\scripts\gen_lockfiles.ps1`).
# Предупреждение в логе сборки видно сразу.
RUN if grep -qE '^[a-zA-Z]' requirements-bsl.lock.txt; then \
        echo "TR-2: ставим из requirements-bsl.lock.txt"; \
        pip install --no-cache-dir -r requirements-bsl.lock.txt; \
    else \
        echo "TR-2: requirements-bsl.lock.txt пуст — ставим по границам из requirements-bsl.txt."; \
        echo "      Пересборка может дать другие версии. Запустите:"; \
        echo "        Linux/WSL:   make lock"; \
        echo "        PowerShell:  .\\scripts\\gen_lockfiles.ps1"; \
        pip install --no-cache-dir -r requirements-bsl.txt; \
    fi

# Скачиваем BSL Language Server
ARG BSL_LS_VERSION=0.28.5
RUN mkdir -p /opt/bsl-language-server && \
    apt-get update && apt-get install -y curl && \
    curl -L -o /opt/bsl-language-server/bsl-ls.jar \
      "https://github.com/1c-syntax/bsl-language-server/releases/download/v${BSL_LS_VERSION}/bsl-language-server-${BSL_LS_VERSION}-exec.jar" && \
    apt-get remove -y curl && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

ENV BSL_LS_JAR=/opt/bsl-language-server/bsl-ls.jar

COPY mcp-bsl-checker/server.py /app/server.py
# B-7: состояние анализатора (bsl_stats). Без этой строки сервер падает на
# импорте при старте; отсутствие ловит tests_bsl_health.py.
COPY mcp-bsl-checker/bsl_health.py /app/bsl_health.py
COPY mcp_auth.py /app/mcp_auth.py
# TR-1: у bsl-checker свой образ, но точка входа общая
COPY mcp_http.py /app/mcp_http.py
# OBS-1: единый словарь отказа (answerable / degraded / meaning).
# Без этой строки сервер падает на импорте при старте. Копируется в ТРИ
# образа; расхождение между ними ловит tests_refusal.py.
COPY refusal.py /app/refusal.py
# TOOL-1: счётчик вызовов инструментов (поле usage в *_stats).
# Импортируется серверами metadata-graph, platform-help и bsl-checker;
# копируется в ТРИ образа — расхождение ловит tests_delivery.py.
COPY tool_usage.py /app/tool_usage.py
# B-4: единый словарь постраничности. Строку не пришлось вспоминать —
# tests_delivery.py (B-6) назвал её сам, как только сервер начал
# импортировать модуль.
COPY mcp_pagination.py /app/mcp_pagination.py
# PERF-7: клиент BSL LS в режиме LSP. Третья подряд строка COPY, которую
# назвал tests_delivery.py, а не память.
COPY mcp-bsl-checker/bsl_lsp.py /app/bsl_lsp.py
# PERF-9: прогрев JVM при старте контейнера. Четвёртая подряд строка,
# которую назвал tests_delivery.py. Без неё сервер падает на импорте — то
# есть образ собирается, а контейнер не поднимается вовсе.
COPY mcp-bsl-checker/bsl_warmup.py /app/bsl_warmup.py
# CFG-4: разбор файла настроек диагностик. Пятая подряд строка COPY в этом
# образе, и первая, которую назвали заранее, а не после падения контейнера:
# tests_delivery.py читает импорты server.py и требует строку сам.
COPY mcp-bsl-checker/bsl_config.py /app/bsl_config.py

EXPOSE 8002

CMD ["python3", "/app/server.py"]
