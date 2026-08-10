FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Базовые пакеты + подключение репозитория Node 22.x от NodeSource
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    git \
    nginx \
    openssl \
    python3 \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && rm -rf /var/lib/apt/lists/*

# Node + npm из NodeSource отдельным шагом, с явной проверкой
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node -v \
    && npm -v

# Конфиг nginx (TLS + reverse proxy на OpenCode)
RUN rm -f /etc/nginx/sites-enabled/default \
    && printf 'server {\n\
    listen 4096 ssl;\n\
    ssl_certificate     /certs/server.crt;\n\
    ssl_certificate_key /certs/server.key;\n\
    ssl_protocols       TLSv1.2 TLSv1.3;\n\
\n\
    location / {\n\
        proxy_pass         http://127.0.0.1:3000;\n\
        proxy_http_version 1.1;\n\
        proxy_set_header   Upgrade $http_upgrade;\n\
        proxy_set_header   Connection "upgrade";\n\
        proxy_set_header   Host $host;\n\
        proxy_read_timeout 300s;\n\
        proxy_send_timeout 300s;\n\
        proxy_connect_timeout 60s;\n\
    }\n\
}\n' > /etc/nginx/sites-enabled/opencode

# OpenCode CLI (теперь npm точно доступен)
RUN npm i -g opencode-ai

# Self-signed TLS (валиден 10 лет)
RUN mkdir -p /certs \
    && openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
       -keyout /certs/server.key \
       -out /certs/server.crt \
       -subj "/CN=localhost"

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /workspace

EXPOSE 4096

CMD ["/entrypoint.sh"]
