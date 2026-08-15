# Makefile для 1C MCP Suite.
#
# Основной способ запуска — docker compose, но часть повседневных операций
# удобнее собрать в короткие команды. На Windows использовать через WSL или
# напрямую запускать python3/py.

.PHONY: help check-prereqs up down build restart logs clean lock v8std v8std-check \
        test test-full test-strict baseline-update eval-all eval-summary

help:
	@echo "1C MCP Suite — доступные команды:"
	@echo ""
	@echo "  make check-prereqs   Проверить готовность окружения к запуску"
	@echo "  make up              Поднять весь стек (docker compose up -d)"
	@echo "  make down            Остановить стек"
	@echo "  make build           Пересобрать образы"
	@echo "  make lock            Сгенерировать лок-файлы зависимостей (нужна сеть)"
	@echo "  make v8std           Забрать/обновить корпус стандартов v8std (нужна сеть)"
	@echo "  make v8std-check     Показать версию и возраст корпуса стандартов"
	@echo "  make restart         Перезапустить (down + up)"
	@echo "  make logs            Следить за логами всех сервисов"
	@echo "  make clean           Остановить и удалить volume'ы (ОСТОРОЖНО: удалит данные)"
	@echo ""
	@echo "  make test            — все тесты одной командой (CI-1)"
	@echo "  make test-strict     — то же, но не запущенный набор = провал (A-5)"
	@echo "  make test-full       — тесты + сверка графа с базой (BASE-1)"
	@echo "  make baseline-update — перезаписать эталонные числа"
	@echo "  make eval-all        — прогнать ВСЕ датасеты и показать сводку (EVAL-3)"
	@echo "  make eval-summary    — сводка по последним отчётам, без прогона"

test:
	@echo "CI-1: прогон всех наборов тестов"
	python3 scripts/run_all_tests.py

# A-5. Пропущенный набор — не успех: в CI это провал, а не примечание.
test-strict:
	@echo "CI-1 + A-5: не запущенный набор считается провалом"
	python3 scripts/run_all_tests.py --strict

test-full:
	@echo "CI-1 + BASE-1: тесты и сверка графа с базой (нужен живой Neo4j)"
	python3 scripts/run_all_tests.py --baseline

# EVAL-3. Три команды и ручное сравнение с прошлыми отчётами — было.
# Одна команда и таблица с дельтой — стало.
eval-all:
	@echo "EVAL-3: прогон всех датасетов (нужен поднятый стек)"
	python3 scripts/eval_all.py

eval-summary:
	@python3 scripts/eval_all.py --summary-only

baseline-update:
	@echo "BASE-1: перезапись эталонных чисел ТЕКУЩИМ состоянием графа."
	@echo "Делайте это только после того, как убедились, что числа верны."
	python3 scripts/check_baseline.py --update

check-prereqs:
	@python3 scripts/check_prereqs.py

up:
	docker compose up -d --build

down:
	docker compose down

build:
	docker compose build

# TR-2. Резолв идёт внутри python:3.12-slim, а не локальным pip:
# набор версий зависит от версии Python и платформы, и лок-файл,
# собранный на хосте, описывал бы не тот образ, который собирается.
lock:
	@bash scripts/gen_lockfiles.sh

# STD-1. Отдельная команда, а не шаг сборки: сеть нужна один раз при
# обновлении, а контейнер v8std-mcp должен подниматься офлайн.
v8std:
	@python3 scripts/fetch_v8std.py

v8std-check:
	@python3 scripts/fetch_v8std.py --check

restart:
	docker compose down
	docker compose up -d

logs:
	docker compose logs -f

clean:
	docker compose down -v
