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
# `make lock` ещё не прогоняли. Предупреждение в логе сборки видно сразу.
RUN if grep -qE '^[a-zA-Z]' requirements-bsl.lock.txt; then \
        echo "TR-2: ставим из requirements-bsl.lock.txt"; \
        pip install --no-cache-dir -r requirements-bsl.lock.txt; \
    else \
        echo "TR-2: requirements-bsl.lock.txt пуст — ставим по границам из requirements-bsl.txt."; \
        echo "      Пересборка может дать другие версии. Запустите: make lock"; \
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
COPY mcp_auth.py /app/mcp_auth.py
# TR-1: у bsl-checker свой образ, но точка входа общая
COPY mcp_http.py /app/mcp_http.py

EXPOSE 8002

CMD ["python3", "/app/server.py"]
