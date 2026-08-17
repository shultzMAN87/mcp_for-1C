#!/usr/bin/env bash
# Генерация лок-файлов для трёх образов (TR-2).
#
# Запускать на машине с сетью — здесь резолвятся транзитивные зависимости.
# Результат коммитится: критерий приёмки TR-2 в том, что пересборка образа
# с нуля даёт тот же набор версий.
#
#     scripts/gen_lockfiles.sh          # через docker, ничего ставить не надо
#     scripts/gen_lockfiles.sh --local  # локальным pip-tools
#
# Почему по умолчанию через docker: резолв зависит от версии Python и от
# платформы. Локальный pip-compile на Windows выдаст не тот набор, который
# получится внутри образа, и лок-файл будет описывать не тот образ, который
# собирается.
#
# ⚠️ И по той же причине КАЖДЫЙ лок-файл резолвится в СВОЁМ базовом образе.
# Наступили на это ровно один раз: все три прогнали в python:3.12-slim, а
# Dockerfile.bsl собирается на eclipse-temurin:17-jre-jammy, где системный
# python — 3.10. Сборка упала на `rpds-py==2026.6.3`: под 3.10 такого
# дистрибутива нет. Меняете базовый образ в Dockerfile — меняйте и здесь.

set -euo pipefail

cd "$(dirname "$0")/../1c-mcp-suite"

# src : dst : базовый образ (совпадает с FROM соответствующего Dockerfile)
PAIRS=(
  "requirements.txt:requirements.lock.txt:python:3.12-slim"
  # python:3.10-slim, а не eclipse-temurin: значение имеет версия Python
  # (в jammy системный python — 3.10), а apt/venv/чужие репозитории только
  # добавляют отказов. Меняете python в Dockerfile.bsl — меняйте и тут.
  "requirements-bsl.txt:requirements-bsl.lock.txt:python:3.10-slim"
  "requirements-embeddings.txt:requirements-embeddings.lock.txt:python:3.12-slim"
  # STD-4: образ v8std-mcp. База — python:3.12-slim, как в Dockerfile.v8std.
  "requirements-v8std.txt:requirements-v8std.lock.txt:python:3.12-slim"
)

# PERF-11: `--emit-index-url` переносит в лок строку `--extra-index-url` из
# requirements-embeddings.txt (индекс CPU-сборок torch). Без неё лок
# получится «правильным по версиям, но без адреса, откуда их брать», и
# сборка образа снова притащит CUDA-колёса из PyPI. Флаг стоит у всех
# четырёх пар, а не у одной: разные флаги у разных пар — второй способ
# завести молча расходящиеся списки (LOCK-1).
compile_local() {
  command -v pip-compile >/dev/null 2>&1 || {
    echo "pip-compile не найден. Установите: pip install pip-tools" >&2
    exit 1
  }
  echo "ВНИМАНИЕ: локальный резолв даёт набор версий вашего Python, а не"
  echo "образов. Годится для проверки, но не для коммита." >&2
  for pair in "${PAIRS[@]}"; do
    src="${pair%%%%:*}"; rest="${pair#*:}"; dst="${rest%%%%:*}"
    echo "→ $src → $dst"
    pip-compile --quiet --strip-extras --emit-index-url --output-file "$dst" "$src"
  done
}

compile_docker() {
  for pair in "${PAIRS[@]}"; do
    src="${pair%%%%:*}"; rest="${pair#*:}"
    dst="${rest%%%%:*}"; image="${rest#*:}"
    echo "→ $src → $dst (в $image)"
    docker run --rm -v "$PWD:/w" -w /w "$image" sh -c "
      pip install --quiet pip-tools typing_extensions &&
      pip-compile --quiet --strip-extras --emit-index-url --output-file '$dst' '$src'
    "
  done
}

if [[ "${1:-}" == "--local" ]]; then
  compile_local
else
  compile_docker
fi

echo
echo "Готово. Проверьте diff и закоммитьте лок-файлы."
echo "Затем пересоберите образы: docker compose build"
