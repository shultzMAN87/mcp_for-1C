#!/bin/bash
set -e

# ── Программная регистрация провайдера ────────────────────────────
# Вместо интерактивного `opencode providers login`
# генерируем auth.json из переменных окружения.
#
# Поддерживаемые переменные:
#   OPENROUTER_API_KEY  — ключ OpenRouter
#   ANTHROPIC_API_KEY   — ключ Anthropic
#   OPENAI_API_KEY_AUTH — ключ OpenAI (для auth.json; не путать с OPENAI_API_KEY)
#
# Можно задать любую комбинацию — в auth.json попадут все указанные.

AUTH_DIR="/root/.local/share/opencode"
AUTH_FILE="${AUTH_DIR}/auth.json"

mkdir -p "${AUTH_DIR}"

# Если auth.json уже примонтирован (read-only) — не трогаем
if [ -f "${AUTH_FILE}" ] && [ ! -w "${AUTH_FILE}" ]; then
    echo "[entrypoint] auth.json уже примонтирован (ro), пропускаем генерацию"
else
    # Собираем JSON динамически
    echo "[entrypoint] Генерация auth.json из переменных окружения..."
    
    python3 -c "
import json, os

auth = {}

if os.environ.get('OPENROUTER_API_KEY'):
    auth['openrouter'] = {'type': 'api', 'key': os.environ['OPENROUTER_API_KEY']}
    print('  + openrouter')

if os.environ.get('ANTHROPIC_API_KEY'):
    auth['anthropic'] = {'type': 'api', 'key': os.environ['ANTHROPIC_API_KEY']}
    print('  + anthropic')

if os.environ.get('OPENAI_API_KEY_AUTH'):
    auth['openai'] = {'type': 'api', 'key': os.environ['OPENAI_API_KEY_AUTH']}
    print('  + openai')

if auth:
    with open('${AUTH_FILE}', 'w') as f:
        json.dump(auth, f, indent=2)
    print(f'  Записано провайдеров: {len(auth)}')
else:
    print('  Нет API-ключей в окружении, auth.json не создан')
    print('  Используйте /connect в веб-интерфейсе для добавления провайдера')
"
fi

# ── Запуск nginx + opencode ───────────────────────────────────────
echo "[entrypoint] Запуск nginx..."
nginx

echo "[entrypoint] Запуск opencode web..."
exec opencode web --port 3000 --hostname 0.0.0.0
