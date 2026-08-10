# Харденинг-патч поверх 4.6.1

Что меняется:

1. **Громкое логирование Neo4j-ошибок** — в `server.py` (вручную) и в
   `graph_writer.py` (готовый файл). Цель: чтобы ошибки уровня Cypher
   (SyntaxError, undefined variable, missing constraint) сразу попадали
   в логи MCP-сервера с текстом запроса, а не маскировались под
   "пустой ответ".

2. **Интеграционные тесты writer'а** — `tests_graph_writer.py`.
   Прогоняют те же Cypher-запросы, что делают v3-tools, против настоящего
   Neo4j. **Был бы такой тест в 4.6.1 — баг с `Variable t not defined`
   отловился бы автоматически.**

## Установка

Файлы для копирования (заменить существующие):

| Из архива                                          | Куда                                                                          |
|----------------------------------------------------|-------------------------------------------------------------------------------|
| `mcp-metadata-graph-neo4j/graph_writer.py`         | `1c-mcp-suite/mcp-metadata-graph-neo4j/graph_writer.py`                       |
| `mcp-metadata-graph-neo4j/tests_graph_writer.py`   | `1c-mcp-suite/mcp-metadata-graph-neo4j/tests_graph_writer.py`                 |
| `mcp-metadata-graph/server_v3_tools.py`            | `1c-mcp-suite/mcp-metadata-graph/server_v3_tools.py` (уже стоял после 4.6.1 fix; одинаковый) |

Файл `patches/server.py.patch2` — это **инструкция руками**: открой `server.py`,
найди функции `_neo4j_query`, `_neo4j_available`, `_neo4j_rows`, `_neo4j_count`
(строки ~95–152), замени блок на текст из патча.

## Проверка после установки

### 1. Юнит-тесты парсера (как и раньше)
```bash
cd 1c-mcp-suite/mcp-metadata-graph-neo4j
python3 tests_metadata_xml.py
# Ожидание: 40 tests, all OK
```

### 2. Интеграционные тесты writer'а (новое)

Используем уже запущенный Neo4j из compose:

```powershell
# Пароль читается из .env — в репозиторий он не попадает.
# ВАЖНО: прежний пароль лежал в этом файле и остался в истории git,
# поэтому его недостаточно удалить — он должен быть сменён в Neo4j.
$env:NEO4J_TEST_URL  = "http://localhost:7474"
$env:NEO4J_TEST_USER = "neo4j"
$env:NEO4J_TEST_PASS = $env:NEO4J_PASSWORD   # SEC-1: значение берётся из .env, в README его быть не должно
cd 1c-mcp-suite\mcp-metadata-graph-neo4j
python tests_graph_writer.py
```

**ВНИМАНИЕ.** Эти тесты пишут в **рабочую** Neo4j. Они изолируют свои данные
по namespace (`_test_<uuid>` в именах), но если хочешь полную чистоту —
запускай против отдельной Neo4j. Тест помечен env-flag'ом
`NEO4J_TEST_ALLOW_WIPE` — если задан в "1", после прогона стирает **весь
слой метаданных** (потребуется переиндексация). По умолчанию — НЕ стирает.

Ожидаемый результат:

```
test_changes_on_file_added ... ok
test_empty_workspace ... ok
test_stable_on_reread ... ok
test_meta_nodes_written ... ok
test_attribute_nodes_written ... ok
test_role_persisted_on_edge ... ok
test_resolves_to_built ... ok
test_subsystem_contains ... ok
test_clear_layer_idempotent ... ok
test_attribute_type_query ... ok
test_find_link_path_query ... ok
test_referrers_query_DOES_NOT_RAISE ... ok      ← главный регрессионный
test_referrers_count_query ... ok
test_object_attributes_query ... ok
test_object_attributes_with_role_filter ... ok
test_subsystem_tree_query ... ok
test_dead_metadata_query ... ok
test_v3_stats_aggregations ... ok
test_syntax_error_raises_runtime ... ok
test_undefined_variable_in_with_raises ... ok
test_fingerprint_roundtrip ... ok

Ran 21 tests in <5s
OK
```

### 3. Smoke (как обычно после правки сервера)

После применения патча `server.py`:

```powershell
docker compose build mcp-metadata-graph
docker compose up -d --force-recreate mcp-metadata-graph
docker run --rm `
  --network 1c-suite-net `
  -e MCP_SHARED_SECRET=... `
  -v "...\1c-mcp-suite\scripts:/scripts:ro" `
  python:3.12-slim `
  sh -c "pip install --quiet 'mcp[cli]' && python /scripts/smoke_metadata_v3.py"
# Ожидание: 6/6 passed
```

## Что бы поймали эти тесты, если бы существовали в 4.6.1

`test_referrers_query_DOES_NOT_RAISE` напрямую исполняет тот Cypher, в
котором был баг. На сломанном запросе Neo4j вернул бы 
`Neo.ClientError.Statement.SyntaxError: Variable t not defined`,
`self.neo.rows(...)` бросил `RuntimeError`, тест упал.

То есть — фикс попал бы в CI, не в продакшен.

В 4.6.2 (BSL-парсер) Cypher станет сложнее: переменной длины пути для
`:CALLS`, опциональные паттерны для конкатенации модулей. Этот testsuite
заточен на проверку **каждого нового tool'а** через прямой Cypher-вызов
до того, как его обернут в MCP-tool. Это правильная защитная сетка.

## Что НЕ войдёт в этот патч (намеренно)

- Структура `evals/` не трогается — там harness 3.4 с jsonl-датасетом,
  это поверх MCP, отдельный слой.
- Smoke (`scripts/smoke_metadata_v3.py`) не трогается — он живёт.
- Нет правки `Dockerfile.python` — тесты writer'а локальные, в образ
  не пакуются.
